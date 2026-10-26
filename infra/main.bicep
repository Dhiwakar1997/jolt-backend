// Jolt backend environment (BE-02). Subscription-scoped so a clean environment,
// resource group included, comes from one deployment:
//
//   scripts/deploy-infra.sh                      # recommended (builds the image too)
//   JOLT_IMAGE=<image> az deployment sub create -l centralindia \
//     -f infra/main.bicep -p infra/main.dev.bicepparam
//
// What it creates, in resource group `jolt-<env>`:
//   * user-assigned Managed Identity for the app (+ one for GitHub Actions OIDC)
//   * Log Analytics workspace
//   * Azure Container Registry (Basic)
//   * Cosmos DB for NoSQL, serverless: database + all containers/partition keys/
//     composite indexes from LLD §6 (infra/cosmos-containers.json)
//   * Storage account with a private `sources` container and Blob CORS
//   * Key Vault (RBAC) with the `token-hash-pepper` and `dev-auth-secret` secrets
//   * Container Apps environment + `jolt-api` app (only when containerImage is set)
// and the least-privilege role assignments between them. See docs/DEPLOY.md.

targetScope = 'subscription'

@description('Environment name: used in resource names, tags and JOLT_ENV.')
@allowed([
  'dev'
  'prod'
])
param environmentName string = 'dev'

@description('Azure region for every resource.')
param location string = deployment().location

@description('Resource group to create or update.')
param resourceGroupName string = 'jolt-${environmentName}'

@description('Image for jolt-api, e.g. <acr>.azurecr.io/jolt-api:<git-sha>. Empty deploys everything except the Container Apps environment and app (first run, before an image exists).')
param containerImage string = ''

@description('Exact browser origins of the app besides the Netlify ones (CORS on /v1 and on Blob).')
param appOrigins array = [
  'http://localhost:5173'
]

@description('Netlify production / preview / custom-domain origins, e.g. https://jolt-app.netlify.app.')
param netlifyOrigins array = []

@description('Regex for API CORS origins that cannot be listed (Netlify deploy previews). Blob CORS does not support it.')
param corsOriginRegex string = ''

@description('Cosmos database name (COSMOS_DATABASE).')
param cosmosDatabaseName string = 'jolt'

@description('Private blob container for uploads (BLOB_CONTAINER).')
param blobContainerName string = 'sources'

@minValue(0)
param minReplicas int = 0

@description('Upper bound for jolt-api replicas. Keep 1 while MCP runs over SSE (see containerapp.bicep).')
@minValue(1)
param maxReplicas int = 1

@description('Keep Cosmos account keys and Storage shared keys enabled. Off: Managed Identity / Entra only.')
param allowKeyAuth bool = false

@description('Optional Entra object id of a developer to grant data access (Cosmos, Blob, Key Vault) for local runs with az login.')
param developerPrincipalId string = ''

@description('owner/repo whose GitHub Actions may push images and roll out revisions. Empty skips the deploy identity.')
param githubRepository string = ''

@description('GitHub environment the deploy job uses (federated credential subject). Empty trusts refs/heads/main.')
param githubEnvironment string = 'dev'

@secure()
@description('Initial token-hash pepper. Pass only on first deploy (deploy-infra.sh does); empty keeps the existing secret.')
param tokenHashPepper string = ''

@description('Google OAuth 2.0 client id(s), comma-separated, whose ID tokens POST /v1/auth/google accepts (GOOGLE_OAUTH_CLIENT_ID). Not a secret. Empty: Google sign-in answers 503.')
param googleOAuthClientId string = ''

@description('MCP address shown to users by GET /v1/me (MCP_SERVER_URL). Empty: https://<app FQDN>/mcp/sse.')
param mcpServerUrl string = ''

@description('Dev-only sign-in by email (POST /v1/auth/dev), for the deploy smoke test. Guarded by the Key Vault secret `dev-auth-secret`. Must stay false in prod (the app refuses to start).')
param devAuthEnabled bool = false

@secure()
@description('Initial dev sign-in secret. Pass only on first deploy (deploy-infra.sh does); empty keeps the existing secret.')
param devAuthSecret string = ''

@description('Extra tags.')
param tags object = {}

var allTags = union(
  {
    app: 'jolt'
    env: environmentName
    managedBy: 'bicep'
  },
  tags
)

var suffix = uniqueString(subscription().id, resourceGroupName)
var names = {
  appIdentity: 'id-jolt-${environmentName}-api'
  githubIdentity: 'id-jolt-${environmentName}-github'
  logAnalytics: 'log-jolt-${environmentName}'
  registry: 'crjolt${environmentName}${suffix}'
  cosmos: 'cosmos-jolt-${environmentName}-${suffix}'
  storage: 'stjolt${environmentName}${suffix}'
  keyVault: 'kv-jolt-${environmentName}-${take(suffix, 8)}'
  containerAppsEnvironment: 'cae-jolt-${environmentName}'
  containerApp: 'jolt-api'
}

var browserOrigins = union(appOrigins, netlifyOrigins)

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: allTags
}

module identity 'modules/identity.bicep' = {
  name: 'identity'
  scope: rg
  params: {
    location: location
    tags: allTags
    appIdentityName: names.appIdentity
    githubIdentityName: names.githubIdentity
    githubRepository: githubRepository
    githubEnvironment: githubEnvironment
  }
}

module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  scope: rg
  params: {
    location: location
    tags: allTags
    name: names.logAnalytics
  }
}

module registry 'modules/registry.bicep' = {
  name: 'registry'
  scope: rg
  params: {
    location: location
    tags: allTags
    name: names.registry
    pullPrincipalId: identity.outputs.appIdentityPrincipalId
    pushPrincipalId: identity.outputs.githubIdentityPrincipalId
  }
}

module cosmos 'modules/cosmos.bicep' = {
  name: 'cosmos'
  scope: rg
  params: {
    location: location
    tags: allTags
    accountName: names.cosmos
    databaseName: cosmosDatabaseName
    appPrincipalId: identity.outputs.appIdentityPrincipalId
    developerPrincipalId: developerPrincipalId
    allowKeyAuth: allowKeyAuth
  }
}

module storage 'modules/storage.bicep' = {
  name: 'storage'
  scope: rg
  params: {
    location: location
    tags: allTags
    name: names.storage
    containerName: blobContainerName
    corsOrigins: browserOrigins
    appPrincipalId: identity.outputs.appIdentityPrincipalId
    developerPrincipalId: developerPrincipalId
    allowKeyAuth: allowKeyAuth
  }
}

module keyVault 'modules/keyvault.bicep' = {
  name: 'keyvault'
  scope: rg
  params: {
    location: location
    tags: allTags
    name: names.keyVault
    appPrincipalId: identity.outputs.appIdentityPrincipalId
    developerPrincipalId: developerPrincipalId
    tokenHashPepper: tokenHashPepper
    devAuthSecret: devAuthSecret
    // The deploy workflow reads the dev sign-in secret for its smoke test.
    devAuthReaderPrincipalId: devAuthEnabled ? identity.outputs.githubIdentityPrincipalId : ''
  }
}

// Consuming the other modules' outputs makes the app wait for them, role
// assignments included (AcrPull before the first pull, Secrets Officer before
// the Key Vault reference resolves).
module app 'modules/containerapp.bicep' = if (!empty(containerImage)) {
  name: 'containerapp'
  scope: rg
  params: {
    location: location
    tags: allTags
    environmentName: environmentName
    containerAppsEnvironmentName: names.containerAppsEnvironment
    containerAppName: names.containerApp
    containerImage: containerImage
    logAnalyticsWorkspaceName: monitoring.outputs.name
    registryLoginServer: registry.outputs.loginServer
    appIdentityId: identity.outputs.appIdentityId
    appIdentityClientId: identity.outputs.appIdentityClientId
    cosmosEndpoint: cosmos.outputs.endpoint
    cosmosDatabaseName: cosmos.outputs.databaseName
    blobEndpoint: storage.outputs.blobEndpoint
    blobContainerName: storage.outputs.containerName
    keyVaultUri: keyVault.outputs.uri
    pepperSecretName: keyVault.outputs.pepperSecretName
    corsOrigins: join(browserOrigins, ',')
    corsOriginRegex: corsOriginRegex
    googleOAuthClientId: googleOAuthClientId
    mcpServerUrl: mcpServerUrl
    devAuthEnabled: devAuthEnabled
    devAuthSecretName: keyVault.outputs.devAuthSecretName
    minReplicas: minReplicas
    maxReplicas: maxReplicas
  }
}

output resourceGroupName string = rg.name
output location string = location
output registryName string = registry.outputs.name
output registryLoginServer string = registry.outputs.loginServer
output cosmosAccountName string = cosmos.outputs.name
output cosmosEndpoint string = cosmos.outputs.endpoint
output cosmosContainers array = cosmos.outputs.containerNames
output storageAccountName string = storage.outputs.name
output blobAccountUrl string = storage.outputs.blobEndpoint
output keyVaultName string = keyVault.outputs.name
output keyVaultUri string = keyVault.outputs.uri
output appIdentityClientId string = identity.outputs.appIdentityClientId
output containerAppName string = names.containerApp
output containerAppFqdn string = empty(containerImage) ? '' : app!.outputs.fqdn
output mcpServerUrl string = empty(containerImage) ? '' : app!.outputs.mcpServerUrl
output githubDeployClientId string = identity.outputs.githubIdentityClientId
output githubFederatedSubject string = identity.outputs.githubSubject
output tenantId string = tenant().tenantId
output subscriptionId string = subscription().subscriptionId
