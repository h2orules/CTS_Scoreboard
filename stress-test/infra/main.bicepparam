// Default parameter values for stress-test/infra/main.bicep.
// `regionTag`, `location`, and `containerImage` are supplied by the
// stress-deploy.yml workflow on the command line and override these.

using './main.bicep'

param regionTag = 'westus'
param containerImage = 'mcr.microsoft.com/azuredocs/aci-helloworld:latest'
param replicaCount = 40
param parallelism = 40
param cpu = '2.0'
param memory = '4.0Gi'
param replicaTimeoutSeconds = 7200
param replicaRetryLimit = 0
