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
//
// BE-10 (production): workspace-based Application Insights + an Azure Monitor
// workbook, the `jolt-maintenance` scheduled job, an optional custom domain
// with a managed certificate, alerts (action group, Cosmos RU, Blob capacity,
// 5xx count and rate) and budgets (resource group, optionally subscription),
// Cosmos continuous backup and Blob versioning as parameters, and promotion
// access to another environment's registry. Same template for dev and prod:
// main.dev.bicepparam / main.prod.bicepparam.

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

// ---- Production hardening (BE-10) -------------------------------------------

@description('Custom domain for the API (a subdomain, e.g. api.jolt.example.com). Empty: the default Container Apps host only. See modules/certificate.bicep for the DNS records.')
param customDomainName string = ''

@description('The managed certificate for customDomainName exists: bind it. false on the first deploy with a domain (the certificate is issued then). scripts/deploy-infra.sh sets it.')
param customDomainCertificateReady bool = false

@description('Cosmos backup: Periodic (24 h / 48 h) or continuous point-in-time restore (7 or 30 days). Periodic -> Continuous cannot be undone.')
@allowed([
  'Periodic'
  'Continuous7Days'
  'Continuous30Days'
])
param cosmosBackupPolicy string = 'Periodic'

@description('Days deleted blobs stay recoverable.')
param blobSoftDeleteDays int = 7

@description('Blob versioning (previous versions kept blobSoftDeleteDays).')
param blobVersioning bool = false

@description('Days deleted Key Vault secrets stay recoverable. Fixed once the vault exists.')
@minValue(7)
@maxValue(90)
param keyVaultSoftDeleteDays int = 7

@description('Log Analytics and Application Insights retention in days.')
param logRetentionDays int = 30

@description('Log ingestion cap in GB per day (-1: none).')
param logDailyQuotaGb int = 1

@description('Run the maintenance job (abandoned uploads, data retention) on maintenanceSchedule.')
param maintenanceEnabled bool = true

@description('Cron (UTC) of the maintenance job.')
param maintenanceSchedule string = '17 3 * * *'

@description('Alert and budget recipients. Empty: no action group, alerts or resource-group budget.')
param alertEmails array = []

@description('Cosmos request units per hour that raise an alert.')
param cosmosRuAlertPerHour int = 1000000

@description('Blob capacity in GiB that raises an alert.')
param blobCapacityAlertGb int = 50

@description('5xx responses in 5 minutes that raise an alert.')
param http5xxAlertCount int = 10

@description('Failed-request percentage over 15 minutes that raises an alert.')
param http5xxAlertRatePercent int = 5

@description('Monthly budget for this resource group (billing currency). 0: none. Needs alertEmails.')
param budgetAmount int = 0

@description('Monthly budget for the whole subscription. 0: none. Set it in one environment only (prod).')
param subscriptionBudgetAmount int = 0

@description('First day of the budget period, yyyy-MM-01.')
param budgetStartDate string = ''

@description('Environment whose registry this one promotes images from (prod: dev). Its GitHub deploy identity gets AcrPull + Reader there. Empty: none.')
param promoteFromEnvironment string = ''

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
  appInsights: 'appi-jolt-${environmentName}'
  maintenanceJob: 'jolt-maintenance'
}

// The source environment's registry, named the way this template names it.
var promotionResourceGroup = 'jolt-${promoteFromEnvironment}'
var promotionRegistry = 'crjolt${promoteFromEnvironment}${uniqueString(subscription().id, promotionResourceGroup)}'

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
    appInsightsName: names.appInsights
    retentionInDays: logRetentionDays
    dailyQuotaGb: logDailyQuotaGb
    workbookDisplayName: 'Jolt API (${environmentName})'
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
    backupPolicy: cosmosBackupPolicy
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
    softDeleteDays: blobSoftDeleteDays
    versioning: blobVersioning
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
    softDeleteRetentionInDays: keyVaultSoftDeleteDays
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
    appInsightsName: monitoring.outputs.appInsightsName
    customDomainName: customDomainName
    customDomainCertificateReady: customDomainCertificateReady
    maintenanceEnabled: maintenanceEnabled
    maintenanceSchedule: maintenanceSchedule
    maintenanceJobName: names.maintenanceJob
  }
}

// The managed certificate is issued once the hostname is on the app; the next
// deploy (customDomainCertificateReady) binds it. Redeploying it is a no-op.
module certificate 'modules/certificate.bicep' = if (!empty(containerImage) && !empty(customDomainName)) {
  name: 'certificate'
  scope: rg
  params: {
    location: location
    tags: allTags
    containerAppsEnvironmentName: app!.outputs.environmentName
    certificateName: app!.outputs.certificateName
    subjectName: customDomainName
  }
}

module alerts 'modules/alerts.bicep' = if (!empty(alertEmails)) {
  name: 'alerts'
  scope: rg
  params: {
    location: location
    tags: allTags
    environmentName: environmentName
    alertEmails: alertEmails
    cosmosAccountId: cosmos.outputs.id
    storageAccountId: storage.outputs.id
    appInsightsId: monitoring.outputs.appInsightsId
    containerAppId: empty(containerImage) ? '' : app!.outputs.id
    cosmosRuPerHour: cosmosRuAlertPerHour
    blobCapacityGb: blobCapacityAlertGb
    http5xxCount: http5xxAlertCount
    http5xxRatePercent: http5xxAlertRatePercent
    budgetAmount: budgetAmount
    budgetStartDate: budgetStartDate
  }
}

// Whole-subscription budget (BE-10 brief: "budget alert on the subscription").
resource subscriptionBudget 'Microsoft.Consumption/budgets@2023-11-01' = if (subscriptionBudgetAmount > 0 && !empty(alertEmails)) {
  name: 'jolt-subscription-monthly'
  properties: {
    category: 'Cost'
    amount: subscriptionBudgetAmount
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: budgetStartDate
    }
    notifications: {
      actual80: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 80
        thresholdType: 'Actual'
        contactEmails: alertEmails
        contactGroups: [
          alerts!.outputs.actionGroupId
        ]
      }
      forecast100: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 100
        thresholdType: 'Forecasted'
        contactEmails: alertEmails
        contactGroups: [
          alerts!.outputs.actionGroupId
        ]
      }
    }
  }
}

// Promotion (prod): this environment's deploy identity may pull from the
// source environment's registry, to copy an image without rebuilding it.
module promotionAccess 'modules/registry-access.bicep' = if (!empty(promoteFromEnvironment) && !empty(githubRepository)) {
  name: 'promotion-access-${environmentName}'
  scope: resourceGroup(promotionResourceGroup)
  params: {
    registryName: promotionRegistry
    principalId: identity.outputs.githubIdentityPrincipalId
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
output publicUrl string = empty(containerImage) ? '' : app!.outputs.publicUrl
output customDomainVerificationId string = empty(containerImage) ? '' : app!.outputs.customDomainVerificationId
output maintenanceJobName string = empty(containerImage) ? '' : app!.outputs.maintenanceJobName
output appInsightsName string = monitoring.outputs.appInsightsName
output actionGroupName string = empty(alertEmails) ? '' : alerts!.outputs.actionGroupName
output promotionRegistryName string = empty(promoteFromEnvironment) ? '' : promotionRegistry
output githubDeployClientId string = identity.outputs.githubIdentityClientId
output githubFederatedSubject string = identity.outputs.githubSubject
output tenantId string = tenant().tenantId
output subscriptionId string = subscription().subscriptionId
