// Bicep for the CTS Scoreboard stress-test pool.
// Deploys an Azure Container Apps Environment + a Manual-trigger Job that
// runs headless Chromium against the live site's public ingress.
// Designed to live in its own resource group, separate from the main app's
// preprod/prod RGs. Egress goes over the public internet (no VNet, no
// private endpoint), so the pool exercises the same network path as real
// users.
//
// Multi-region is achieved by deploying this template twice (once per region,
// each into its own RG) rather than looping inside the template.

@description('Azure region for all resources in this RG.')
param location string = resourceGroup().location

@description('Short region label included in resource names so a multi-region deploy gets unique ACR/Job names. Examples: westus, eastus2.')
@minLength(2)
@maxLength(12)
param regionTag string = 'westus'

@description('Container image reference (full ACR path with tag or @sha256 digest). For the very first deploy, pass a placeholder like mcr.microsoft.com/azuredocs/aci-helloworld:latest.')
param containerImage string

@description('Number of job replicas to spawn per execution. Each replica runs BROWSERS_PER_REPLICA browsers. Default 40 * 10 = 400 browsers.')
@minValue(1)
@maxValue(300)
param replicaCount int = 40

@description('Replicas to run in parallel. Set equal to replicaCount so all replicas start together.')
@minValue(1)
@maxValue(300)
param parallelism int = 40

@description('vCPU per replica. Range 0.25 - 4.0 in 0.25 increments (Consumption profile).')
param cpu string = '2.0'

@description('Memory per replica (GiB). Must follow ACA Consumption ratios (e.g. 4Gi for 2 vCPU).')
param memory string = '4.0Gi'

@description('Hard cap (seconds) for any single replica. Should exceed the longest TOTAL_DURATION_SECONDS you plan to run.')
@minValue(60)
@maxValue(86400)
param replicaTimeoutSeconds int = 7200

@description('Per-replica retry count. Keep at 0 so a crashed replica does not silently respawn and skew load.')
@minValue(0)
@maxValue(3)
param replicaRetryLimit int = 0

// ---------- naming ----------
var prefix = 'cts-sb-stress-${regionTag}'
var acrName = take(replace('${prefix}acr', '-', ''), 50)
var laName = '${prefix}-la'
var caEnvName = '${prefix}-cae'
var jobName = '${prefix}-job'
var uamiName = '${prefix}-uami'

// ---------- observability ----------
resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: laName
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

// ---------- container registry ----------
resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: acrName
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

// ---------- user-assigned managed identity (for ACR pull) ----------
// Same pattern as azure/infra/main.bicep: declare AcrPull on the UAMI BEFORE
// the workload so the first image pull does not race role propagation.
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

// ---------- container apps environment (Consumption only, no workload profile) ----------
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

// ---------- container apps job ----------
// Manual trigger: each `az containerapp job start` invocation spawns
// `replicaCompletionCount` replicas, with `parallelism` running concurrently.
// Per-execution overrides (image, env vars, cpu/memory, replica counts) are
// supplied by the stress-run workflow via `az containerapp job start --env-vars`.
resource caJob 'Microsoft.App/jobs@2024-03-01' = {
  name: jobName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${uami.id}': {} }
  }
  dependsOn: [ acrPull ]
  properties: {
    environmentId: caEnv.id
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: replicaTimeoutSeconds
      replicaRetryLimit: replicaRetryLimit
      manualTriggerConfig: {
        parallelism: parallelism
        replicaCompletionCount: replicaCount
      }
      registries: [
        {
          server: '${acrName}.azurecr.io'
          identity: uami.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'stress'
          image: containerImage
          resources: {
            cpu: json(cpu)
            memory: memory
          }
          env: [
            // Defaults; the stress-run workflow overrides these per execution
            // via `az containerapp job start --env-vars`.
            { name: 'TARGET_URL', value: 'https://example.invalid/' }
            { name: 'BROWSERS_PER_REPLICA', value: '10' }
            { name: 'MIN_HOLD_SECONDS', value: '30' }
            { name: 'MAX_HOLD_SECONDS', value: '120' }
            { name: 'MIN_DELAY_SECONDS', value: '2' }
            { name: 'MAX_DELAY_SECONDS', value: '10' }
            { name: 'CACHE_MODE', value: 'mixed' }
            { name: 'TOTAL_DURATION_SECONDS', value: '600' }
            { name: 'LOG_LEVEL', value: 'info' }
          ]
        }
      ]
    }
  }
}

// ---------- outputs ----------
output jobName string = caJob.name
output environmentName string = caEnv.name
output acrName string = acr.name
output acrLoginServer string = '${acrName}.azurecr.io'
output logAnalyticsName string = law.name
output region string = regionTag
