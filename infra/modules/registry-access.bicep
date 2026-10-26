// Lets another environment's GitHub deploy identity pull from this registry
// (BE-10 promotion: deploy-prod.yml copies the image dev built and tested from
// the dev registry to the prod one, no rebuild). Deployed into the source
// environment's resource group by the target environment's main.bicep.

param registryName string

@description('Principal id of the identity that copies images out (gets AcrPull + Reader).')
param principalId string

var roles = {
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  reader: 'acdd72a7-3385-48ef-bd42-f606fba81ae7'
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

resource pull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, principalId, roles.acrPull)
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull)
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}

// `az acr login` resolves the registry through ARM.
resource read 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, principalId, roles.reader)
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.reader)
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}
