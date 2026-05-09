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

@description('Min replicas for the Container App. 0 enables scale-to-zero (preprod).')
@minValue(0)
@maxValue(10)
param minReplicas int = environmentName == 'preprod' ? 0 : 1

@description('Max replicas for the Container App.')
@minValue(1)
@maxValue(30)
param maxReplicas int = environmentName == 'prod' ? 10 : 2

@description('Entra tenant ID used to validate Pi access tokens.')
param entraTenantId string

@description('Application (client) ID of the relay app registration. Required audience for Pi access tokens.')
param entraAudience string

@description('Email address for Azure Monitor alert receivers. Supplied at deploy time; not stored in source.')
@secure()
param alertEmail string

@description('Country code (digits only, e.g. 1 for US) for SMS alert receiver.')
param alertSmsCountryCode string

@description('Phone number (digits only, no formatting) for SMS alert receiver.')
@secure()
param alertSmsPhone string

// ---------- naming ----------
var prefix = 'cts-sb-${environmentName}'
var acrName = replace('${prefix}acr', '-', '')
var laName = '${prefix}-la'
var aiName = '${prefix}-ai'
var caEnvName = '${prefix}-cae'
var caName = '${prefix}-app'
var redisName = '${prefix}-redis'
var storageName = take(replace('${prefix}st', '-', ''), 24)
var pubsubName = '${prefix}-wps'
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

// ---------- Web PubSub for Socket.IO ----------
resource webPubSub 'Microsoft.SignalRService/webPubSub@2024-03-01' = {
  name: pubsubName
  location: location
  sku: {
    name: environmentName == 'prod' ? 'Standard_S1' : 'Free_F1'
    capacity: 1
    tier: environmentName == 'prod' ? 'Standard' : 'Free'
  }
  kind: 'SocketIO'
  properties: {
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: false
    socketIO: {
      serviceMode: 'Default'
    }
  }
}

// ---------- container apps environment ----------
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

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: caName
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {
    managedEnvironmentId: caEnv.id
    configuration: {
      activeRevisionsMode: 'Multiple' // enables 'az containerapp revision activate' rollback
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
        // No stickySessions: Web PubSub for Socket.IO terminates the
        // websocket on the Web PubSub side, so the Container App ingress
        // sees stateless HTTP. (Sticky sessions also aren't supported in
        // Multiple revision mode anyway.)
      }
      registries: [
        {
          server: '${acrName}.azurecr.io'
          identity: 'system'
        }
      ]
      secrets: [
        { name: 'redis-conn', value: '${redis.properties.hostName}:${redis.properties.sslPort},password=${redis.listKeys().primaryKey},ssl=True,abortConnect=False' }
        { name: 'storage-conn', value: 'DefaultEndpointsProtocol=https;AccountName=${storage.name};AccountKey=${storage.listKeys().keys[0].value};EndpointSuffix=${environment().suffixes.storage}' }
        { name: 'webpubsub-conn', value: webPubSub.listKeys().primaryConnectionString }
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
            { name: 'WEBPUBSUB_CONNECTION_STRING', secretRef: 'webpubsub-conn' }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', secretRef: 'appinsights-conn' }
            { name: 'ENTRA_TENANT_ID', value: entraTenantId }
            { name: 'ENTRA_AUDIENCE', value: entraAudience }
          ]
          probes: [
            { type: 'Liveness',  httpGet: { path: '/healthz', port: 8000 }, periodSeconds: 30 }
            { type: 'Readiness', httpGet: { path: '/readyz',  port: 8000 }, periodSeconds: 15 }
          ]
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

// AcrPull for the Container App's managed identity.
resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(acr.id, containerApp.id, 'AcrPull')
  properties: {
    principalId: containerApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}

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
output webPubSubName string = webPubSub.name
output redisHost string = redis.properties.hostName
output storageAccountName string = storage.name
