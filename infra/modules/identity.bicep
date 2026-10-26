// Identities: the app's user-assigned Managed Identity, and (optionally) a second
// one GitHub Actions signs in as through OIDC to push images and roll out
// revisions. Using a user-assigned identity with a federated credential means no
// Entra app registration and no client secret anywhere.

@description('Azure region.')
param location string

@description('Tags applied to every resource.')
param tags object

@description('Name of the app identity (used by the jolt-api Container App).')
param appIdentityName string

@description('Name of the GitHub deploy identity.')
param githubIdentityName string

@description('owner/repo allowed to deploy, e.g. Dhiwakar1997/jolt-backend. Empty skips the GitHub identity.')
param githubRepository string

@description('GitHub environment the deploy job runs in. Empty trusts pushes to refs/heads/main instead.')
param githubEnvironment string

var deployFromGitHub = !empty(githubRepository)
var githubSubject = empty(githubEnvironment)
  ? 'repo:${githubRepository}:ref:refs/heads/main'
  : 'repo:${githubRepository}:environment:${githubEnvironment}'

// Built-in role definition ids.
var roles = {
  containerAppsContributor: '358470bc-b998-42bd-ab17-a7e34c199c0f'
  managedIdentityOperator: 'f1a07417-d97a-45cb-824c-7a7467783830'
}

resource appIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: appIdentityName
  location: location
  tags: tags
}

resource githubIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = if (deployFromGitHub) {
  name: githubIdentityName
  location: location
  tags: tags
}

resource githubFederation 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = if (deployFromGitHub) {
  parent: githubIdentity
  name: 'github-actions'
  properties: {
    issuer: 'https://token.actions.githubusercontent.com'
    subject: githubSubject
    audiences: [
      'api://AzureADTokenExchange'
    ]
  }
}

// The deploy identity may update Container Apps in this resource group (image
// and revision changes; includes `managedEnvironments/join/action`), nothing else.
resource githubAppsContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployFromGitHub) {
  name: guid(resourceGroup().id, githubIdentityName, roles.containerAppsContributor)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      roles.containerAppsContributor
    )
    principalId: deployFromGitHub ? githubIdentity!.properties.principalId : ''
    principalType: 'ServicePrincipal'
  }
}

// Updating an app that carries a user-assigned identity re-asserts that identity,
// which ARM checks as `userAssignedIdentities/assign/action` on it.
resource githubAssignsAppIdentity 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployFromGitHub) {
  name: guid(appIdentity.id, githubIdentityName, roles.managedIdentityOperator)
  scope: appIdentity
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      roles.managedIdentityOperator
    )
    principalId: deployFromGitHub ? githubIdentity!.properties.principalId : ''
    principalType: 'ServicePrincipal'
  }
}

output appIdentityId string = appIdentity.id
output appIdentityPrincipalId string = appIdentity.properties.principalId
output appIdentityClientId string = appIdentity.properties.clientId
output githubIdentityPrincipalId string = deployFromGitHub ? githubIdentity!.properties.principalId : ''
output githubIdentityClientId string = deployFromGitHub ? githubIdentity!.properties.clientId : ''
output githubSubject string = deployFromGitHub ? githubSubject : ''
