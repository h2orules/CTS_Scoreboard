@description('Globally unique Azure Managed Redis name.')
param name string

param location string = resourceGroup().location

@description('Non-HA Balanced size. Anything above B1 requires an explicit design/cost review.')
@allowed(['Balanced_B0', 'Balanced_B1'])
param skuName string = 'Balanced_B0'

resource cache 'Microsoft.Cache/redisEnterprise@2025-07-01' = {
  name: name
  location: location
  sku: {
    name: skuName
  }
  properties: {
    highAvailability: 'Disabled'
    minimumTlsVersion: '1.2'
    publicNetworkAccess: 'Enabled'
  }
}

resource database 'Microsoft.Cache/redisEnterprise/databases@2025-07-01' = {
  parent: cache
  name: 'default'
  properties: {
    // redis.asyncio.Redis and Socket.IO need a single non-cluster endpoint.
    clusteringPolicy: 'NoCluster'
    clientProtocol: 'Encrypted'
    port: 10000
    accessKeysAuthentication: 'Enabled'
    // Match the legacy Basic C0 policy and preserve TTL-based eviction.
    evictionPolicy: 'VolatileLRU'
  }
}

output hostName string = cache.properties.hostName
output port int = database.properties.port

@secure()
output connectionString string = 'rediss://:${uriComponent(database.listKeys().primaryKey)}@${cache.properties.hostName}:${database.properties.port}/0'
