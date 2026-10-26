// Azure Container Registry (Basic). The app pulls with its Managed Identity
// (AcrPull) and GitHub Actions pushes with its federated identity (AcrPush), so
// there is no admin user, no password and no PAT anywhere.

param location string
param tags object
param name string

@description('Principal id of the app identity (gets AcrPull).')
param pullPrincipalId string

@description('Principal id of the GitHub deploy identity (gets AcrPush). Empty skips it.')
param pushPrincipalId string = ''

var roles = {
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  acrPush: '8311e382-0749-4cb8-b61a-304f252e45ec'
  reader: 'acdd72a7-3385-48ef-bd42-f606fba81ae7'
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

resource appPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, pullPrincipalId, roles.acrPull)
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull)
    principalId: pullPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource githubPush 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(pushPrincipalId)) {
  name: guid(registry.id, pushPrincipalId, roles.acrPush)
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPush)
    principalId: pushPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// AcrPush has no `registries/read`; `az acr login` resolves the registry through
// ARM, so the deploy identity can also read (only) this registry's metadata.
resource githubRead 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(pushPrincipalId)) {
  name: guid(registry.id, pushPrincipalId, roles.reader)
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.reader)
    principalId: pushPrincipalId
    principalType: 'ServicePrincipal'
  }
}

output name string = registry.name
output loginServer string = registry.properties.loginServer
