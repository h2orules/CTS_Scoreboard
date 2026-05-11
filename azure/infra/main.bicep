// Bicep template for the CTS Scoreboard Azure relay.
// One file deploys preprod or prod (controlled by `environmentName`).
// Recipient contact info for alerts is supplied via parameters; never default
// these or commit them to source. Pass them at deploy time from GitHub
// secrets.

@description('Short environment label, used as a suffix on every resource.')
@allowed(['preprod', 'prod'])
param environmentName string

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Container image reference (full path with digest or tag).')
param containerImage string

@description('TCP port the container listens on. Defaults to 8000 (the relay app). For the very first bootstrap deploy, set this to whatever your placeholder image listens on (e.g. 80 for mcr.microsoft.com/azuredocs/aci-helloworld).')
param targetPort int = 8000

@description('Min replicas for the Container App. 0 enables scale-to-zero (preprod).')
@minValue(0)
@maxValue(10)
param minReplicas int = environmentName == 'preprod' ? 0 : 1

@description('Max replicas for the Container App.')
@minValue(1)
@maxValue(30)
// Socket.IO fanout across replicas works because the app uses
// socketio.AsyncRedisManager (backed by the same Redis instance defined
// below). Clients connect with websocket-only transport, so we don't
// need sticky sessions on the ingress: each connection lives on
// whichever worker accepts the upgrade for its lifetime, and broadcasts
// to rooms reach clients on every other worker/replica via Redis.
param maxReplicas int = environmentName == 'prod' ? 10 : 2

@description('Entra tenant ID used to validate Pi access tokens.')
param entraTenantId string

@description('Audience expected on Pi access tokens. Set to the relay app registration\'s Application ID URI (e.g. `api://<client-id>`). The validator also accepts the bare GUID form for backward compatibility.')
param entraAudience string

@description('Email address for Azure Monitor alert receivers. Supplied at deploy time; not stored in source.')
@secure()
@minLength(5)
param alertEmail string

@description('Country code (digits only, e.g. 1 for US) for SMS alert receiver.')
@minLength(1)
@maxLength(4)
param alertSmsCountryCode string

@description('Phone number (digits only, no formatting) for SMS alert receiver.')
@secure()
@minLength(7)
param alertSmsPhone string

// ---------- naming ----------
var prefix = 'cts-sb-${environmentName}'
var acrName = replace('${prefix}acr', '-', '')
var laName = '${prefix}-la'
var aiName = '${prefix}-ai'
var caEnvName = '${prefix}-cae'
var caName = '${prefix}-app'
var uamiName = '${prefix}-uami'
var redisName = '${prefix}-redis'
var storageName = take(replace('${prefix}st', '-', ''), 24)
var actionGroupName = '${prefix}-ag'

// ---------- observability ----------
resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: laName
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource ai 'Microsoft.Insights/components@2020-02-02' = {
  name: aiName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: law.id
    IngestionMode: 'LogAnalytics'
  }
}

// ---------- container registry ----------
resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: acrName
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

// ---------- user-assigned managed identity for ACR pull ----------
// We grant AcrPull to this UAMI BEFORE the Container App is created, so the
// first revision can pull the image immediately. (System-assigned identity
// would create a chicken-and-egg: the role assignment can't exist until the
// Container App exists, so the first pull always races role propagation and
// can hang for 10+ minutes.)
resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: uamiName
  location: location
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(acr.id, uami.id, 'AcrPull')
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}

// ---------- storage (Tables + Blob) ----------
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
  }
}

resource storageTables 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource meetsTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = {
  parent: storageTables
  name: 'meets'
}

resource storageBlob 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource snapshotsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: storageBlob
  name: 'meet-snapshots'
}

// ---------- redis ----------
resource redis 'Microsoft.Cache/redis@2023-08-01' = {
  name: redisName
  location: location
  properties: {
    sku: {
      name: 'Basic'
      family: 'C'
      capacity: 0
    }
    enableNonSslPort: false
    minimumTlsVersion: '1.2'
  }
}

// ---------- container apps environment ----------
var customDomain = environmentName == 'prod' ? 'scoreboard.aquagnomeapps.com' : 'scoreboard-pre.aquagnomeapps.com'

resource caEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: caEnvName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: law.properties.customerId
        sharedKey: law.listKeys().primarySharedKey
      }
    }
  }
}

// Managed certificate for the custom domain. Azure handles renewal automatically.
// PREREQUISITE: DNS records must be in place before deploying this resource —
// see azure/docs/CUSTOM_DOMAIN_SETUP.md for the required GoDaddy records.
resource managedCert 'Microsoft.App/managedEnvironments/managedCertificates@2024-03-01' = {
  parent: caEnv
  name: '${prefix}-cert'
  location: location
  properties: {
    subjectName: customDomain
    domainControlValidation: 'CNAME'
  }
}

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: caName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${uami.id}': {} }
  }
  dependsOn: [ acrPull ]
  properties: {
    managedEnvironmentId: caEnv.id
    configuration: {
      activeRevisionsMode: 'Multiple' // enables 'az containerapp revision activate' rollback
      ingress: {
        external: true
        targetPort: targetPort
        transport: 'auto'
        allowInsecure: false
        // No stickySessions: not supported in Multiple-revision mode, and
        // not needed because clients use websocket-only transport (see
        // _rewrite_io_connect in app/routes.py and the Pi client's
        // transports=['websocket'] in azure_relay.py). Cross-worker /
        // cross-replica fanout is handled by socketio.AsyncRedisManager.
        customDomains: [
          {
            name: customDomain
            certificateId: managedCert.id
            bindingType: 'SniEnabled'
          }
        ]
      }
      registries: [
        {
          server: '${acrName}.azurecr.io'
          identity: uami.id
        }
      ]
      secrets: [
        // redis-py expects a URL like rediss://:<key>@<host>:<sslPort>/0.
        // The raw password is URL-encoded (it can contain '+', '/', '=').
        { name: 'redis-conn', value: 'rediss://:${uriComponent(redis.listKeys().primaryKey)}@${redis.properties.hostName}:${redis.properties.sslPort}/0' }
        { name: 'storage-conn', value: 'DefaultEndpointsProtocol=https;AccountName=${storage.name};AccountKey=${storage.listKeys().keys[0].value};EndpointSuffix=${environment().suffixes.storage}' }
        { name: 'appinsights-conn', value: ai.properties.ConnectionString }
      ]
    }
    template: {
      containers: [
        {
          name: 'relay'
          image: containerImage
          resources: {
            cpu: json(environmentName == 'prod' ? '1.0' : '0.5')
            memory: environmentName == 'prod' ? '2Gi' : '1Gi'
          }
          env: [
            { name: 'ENVIRONMENT', value: environmentName }
            { name: 'LOG_LEVEL', value: 'INFO' }
            { name: 'REDIS_URL', secretRef: 'redis-conn' }
            { name: 'STORAGE_CONNECTION_STRING', secretRef: 'storage-conn' }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', secretRef: 'appinsights-conn' }
            { name: 'ENTRA_TENANT_ID', value: entraTenantId }
            { name: 'ENTRA_AUDIENCE', value: entraAudience }
          ]
          // No explicit probes here. Container Apps applies a default TCP
          // socket probe on targetPort, which works for any image (including
          // bootstrap placeholders). The relay app's /healthz and /readyz
          // endpoints are exercised by the deploy workflow's smoke test
          // after each revision flip.
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: [
          {
            name: 'http-rule'
            http: { metadata: { concurrentRequests: '50' } }
          }
        ]
      }
    }
  }
}

// AcrPull role for the user-assigned MI is declared above, before the
// Container App, so the first image pull doesn't race role propagation.

// ---------- alerting ----------
resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: actionGroupName
  location: 'global'
  properties: {
    groupShortName: take('${environmentName}-ag', 12)
    enabled: true
    emailReceivers: [
      {
        name: 'primary-email'
        emailAddress: alertEmail
        useCommonAlertSchema: true
      }
    ]
    smsReceivers: [
      {
        name: 'primary-sms'
        countryCode: alertSmsCountryCode
        phoneNumber: alertSmsPhone
      }
    ]
  }
}

// Alert: client error rate > 5/min for 5 min.
resource alertClientErrors 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: '${prefix}-alert-client-errors'
  location: location
  properties: {
    severity: 2
    enabled: true
    evaluationFrequency: 'PT5M'
    windowSize: 'PT5M'
    scopes: [ai.id]
    criteria: {
      allOf: [
        {
          query: 'customMetrics | where name == "client_errors_total" | summarize n = sum(value) | where n > 25'
          timeAggregation: 'Total'
          metricMeasureColumn: 'n'
          operator: 'GreaterThan'
          threshold: 0
        }
      ]
    }
    actions: { actionGroups: [actionGroup.id] }
  }
}

// Alert: any single meet sees > 5 Pi reconnects in 10 min.
resource alertReconnects 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: '${prefix}-alert-pi-reconnects'
  location: location
  properties: {
    severity: 3
    enabled: true
    evaluationFrequency: 'PT5M'
    windowSize: 'PT10M'
    scopes: [ai.id]
    criteria: {
      allOf: [
        {
          query: 'customMetrics | where name == "pi_reconnects_total" | summarize n = sum(value) by tostring(customDimensions["meet_id"]) | where n > 5'
          timeAggregation: 'Total'
          metricMeasureColumn: 'n'
          operator: 'GreaterThan'
          threshold: 0
        }
      ]
    }
    actions: { actionGroups: [actionGroup.id] }
  }
}

// ---------- outputs ----------
output containerAppFqdn string = containerApp.properties.configuration.ingress.fqdn
output acrLoginServer string = '${acrName}.azurecr.io'
output appInsightsConnectionString string = ai.properties.ConnectionString
output redisHost string = redis.properties.hostName
output storageAccountName string = storage.name
