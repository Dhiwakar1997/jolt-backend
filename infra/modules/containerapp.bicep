// Container Apps environment + the `jolt-api` app: HTTPS ingress on port 8000,
// /health probes, scale 0..N, pulling from ACR with the app's Managed Identity
// and reading TOKEN_HASH_PEPPER as a Key Vault secret reference.

param location string
param tags object
param environmentName string
param containerAppsEnvironmentName string
param containerAppName string

@description('Full image reference, e.g. crjoltdevxxxx.azurecr.io/jolt-api:<git-sha>.')
param containerImage string

param logAnalyticsWorkspaceName string
param registryLoginServer string

param appIdentityId string
param appIdentityClientId string

param cosmosEndpoint string
param cosmosDatabaseName string
param blobEndpoint string
param blobContainerName string
param keyVaultUri string
param pepperSecretName string

@description('Comma-separated exact origins for CORS_ORIGINS.')
param corsOrigins string

@description('CORS_ORIGIN_REGEX, e.g. for Netlify deploy previews. Empty disables it.')
param corsOriginRegex string = ''

@minValue(0)
param minReplicas int = 0

@description('Keep at 1 while MCP runs over SSE: the SSE stream and its POSTs must reach the same replica (session state is in memory).')
@minValue(1)
param maxReplicas int = 1

param cpu string = '0.5'
param memory string = '1Gi'

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsWorkspaceName
}

resource managedEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: containerAppsEnvironmentName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: workspace.properties.customerId
        sharedKey: workspace.listKeys().primarySharedKey
      }
    }
    zoneRedundant: false
  }
}

var healthProbe = {
  path: '/health'
  port: 8000
  scheme: 'HTTP'
}

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: containerAppName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${appIdentityId}': {}
    }
  }
  properties: {
    managedEnvironmentId: managedEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      registries: [
        {
          server: registryLoginServer
          identity: appIdentityId
        }
      ]
      secrets: [
        {
          name: 'token-hash-pepper'
          keyVaultUrl: '${keyVaultUri}secrets/${pepperSecretName}'
          identity: appIdentityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'jolt-api'
          image: containerImage
          resources: {
            cpu: json(cpu)
            memory: memory
          }
          env: [
            { name: 'JOLT_ENV', value: environmentName }
            { name: 'STORAGE_MODE', value: 'azure' }
            { name: 'AUTH_MODE', value: 'managed_identity' }
            // DefaultAzureCredential picks the user-assigned identity by client id.
            { name: 'AZURE_CLIENT_ID', value: appIdentityClientId }
            { name: 'COSMOS_ENDPOINT', value: cosmosEndpoint }
            { name: 'COSMOS_DATABASE', value: cosmosDatabaseName }
            { name: 'BLOB_ACCOUNT_URL', value: blobEndpoint }
            { name: 'BLOB_CONTAINER', value: blobContainerName }
            { name: 'KEYVAULT_URL', value: keyVaultUri }
            { name: 'CORS_ORIGINS', value: corsOrigins }
            { name: 'CORS_ORIGIN_REGEX', value: corsOriginRegex }
            { name: 'TOKEN_HASH_PEPPER', secretRef: 'token-hash-pepper' }
          ]
          probes: [
            {
              type: 'Startup'
              httpGet: healthProbe
              initialDelaySeconds: 2
              periodSeconds: 3
              timeoutSeconds: 3
              failureThreshold: 10
            }
            {
              type: 'Readiness'
              httpGet: healthProbe
              periodSeconds: 10
              timeoutSeconds: 3
              failureThreshold: 3
            }
            {
              type: 'Liveness'
              httpGet: healthProbe
              periodSeconds: 30
              timeoutSeconds: 5
              failureThreshold: 3
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: [
          {
            name: 'http'
            http: {
              metadata: {
                concurrentRequests: '50'
              }
            }
          }
        ]
      }
    }
  }
}

output name string = app.name
output fqdn string = app.properties.configuration.ingress.fqdn
output environmentName string = managedEnvironment.name
