// Key Vault (RBAC model). Holds the token-hash pepper and the dev sign-in secret,
// which the Container App reads as Key Vault secret references, and the per-user
// provider API keys the app writes and deletes at runtime (LLD §5.1).

param location string
param tags object
param name string

@description('Principal id of the app identity (gets Key Vault Secrets Officer).')
param appPrincipalId string

@description('Optional Entra object id of a developer who also gets Secrets Officer.')
param developerPrincipalId string = ''

@secure()
@description('Value for the `token-hash-pepper` secret. Empty leaves an existing secret untouched (scripts/deploy-infra.sh passes a fresh one only when the secret does not exist yet).')
param tokenHashPepper string = ''

@secure()
@description('Value for the `dev-auth-secret` secret (DEV_AUTH_SECRET, BE-03). Empty leaves an existing secret untouched (scripts/deploy-infra.sh passes a fresh one only when the secret does not exist yet).')
param devAuthSecret string = ''

@description('Principal allowed to read `dev-auth-secret` only (the GitHub deploy identity, so the deploy smoke test can sign in). Empty: nobody besides the app.')
param devAuthReaderPrincipalId string = ''

@description('Days a deleted vault or secret is kept before it can be purged.')
param softDeleteRetentionInDays int = 7

@description('Log Analytics workspace that receives the vault\'s audit log (every secret read, write, delete and purge, with the caller). Empty: no diagnostic setting.')
param logAnalyticsWorkspaceId string = ''

// Secrets Officer, not Secrets User: the app also sets, deletes and purges the
// per-user `apikey-*` secrets. It covers reading the pepper too.
var secretsOfficerRoleId = 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'
// Key Vault Secrets User: read secret values. Granted on one secret, not the vault.
var secretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'
var devAuthSecretName = 'dev-auth-secret'

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: softDeleteRetentionInDays
    // Purge protection stays off in dev so the API-key delete can purge, and a
    // torn-down environment can be recreated under the same name.
    publicNetworkAccess: 'Enabled'
  }
}

resource pepper 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (!empty(tokenHashPepper)) {
  parent: vault
  name: 'token-hash-pepper'
  properties: {
    value: tokenHashPepper
    contentType: 'text/plain'
  }
}

resource devAuth 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (!empty(devAuthSecret)) {
  parent: vault
  name: devAuthSecretName
  properties: {
    value: devAuthSecret
    contentType: 'text/plain'
  }
}

// The secret may come from an earlier deployment (devAuthSecret is passed once).
resource devAuthCurrent 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = {
  parent: vault
  name: devAuthSecretName
}

resource devAuthReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(devAuthReaderPrincipalId)) {
  name: guid(vault.id, devAuthSecretName, devAuthReaderPrincipalId, secretsUserRoleId)
  scope: devAuthCurrent
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      secretsUserRoleId
    )
    principalId: devAuthReaderPrincipalId
    principalType: 'ServicePrincipal'
  }
  dependsOn: [
    devAuth
  ]
}

resource appSecrets 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, appPrincipalId, secretsOfficerRoleId)
  scope: vault
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      secretsOfficerRoleId
    )
    principalId: appPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource developerSecrets 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(developerPrincipalId)) {
  name: guid(vault.id, developerPrincipalId, secretsOfficerRoleId)
  scope: vault
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      secretsOfficerRoleId
    )
    principalId: developerPrincipalId
    principalType: 'User'
  }
}

// BE-10 security review: Key Vault access is logged. The `audit` category
// group is the AuditEvent log: every data-plane call (get / set / delete /
// purge of a secret, including the per-user `apikey-*` secrets) with the
// caller's identity, result and client IP, kept for the workspace retention.
// Query: AzureDiagnostics | where ResourceProvider == "MICROSOFT.KEYVAULT".
resource auditLogs 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (!empty(logAnalyticsWorkspaceId)) {
  name: 'audit-to-log-analytics'
  scope: vault
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        categoryGroup: 'audit'
        enabled: true
      }
    ]
  }
}

output name string = vault.name
output uri string = vault.properties.vaultUri
output pepperSecretName string = 'token-hash-pepper'
output devAuthSecretName string = devAuthSecretName
