// Storage account for uploaded sources. A private `sources` container; the app
// mints short-lived user-delegation SAS URLs (signed with its Managed Identity),
// and the browser PUTs straight to Blob, which needs the CORS rule below.

param location string
param tags object
param name string
param containerName string

@description('Browser origins allowed to PUT/GET blobs with a SAS URL (exact origins; Blob CORS has no wildcards inside a host).')
param corsOrigins array

@description('Principal id of the app identity (gets Storage Blob Data Contributor).')
param appPrincipalId string

@description('Optional Entra object id of a developer who also gets blob data access.')
param developerPrincipalId string = ''

@description('Keep shared-key access (account keys, account-key SAS). Off by default: user-delegation SAS only.')
param allowKeyAuth bool = false

// Storage Blob Data Contributor also carries
// `blobServices/generateUserDelegationKey/action`, which the user-delegation SAS
// in jolt/data/blob.py needs, so Storage Blob Delegator is not assigned on top.
var blobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'

resource account 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: name
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: allowKeyAuth
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Enabled'
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: account
  name: 'default'
  properties: {
    cors: {
      corsRules: [
        {
          allowedOrigins: corsOrigins
          allowedMethods: [
            'GET'
            'HEAD'
            'PUT'
            'OPTIONS'
          ]
          allowedHeaders: [
            '*'
          ]
          exposedHeaders: [
            'ETag'
            'Content-Length'
            'Content-Type'
            'x-ms-request-id'
          ]
          maxAgeInSeconds: 3600
        }
      ]
    }
    deleteRetentionPolicy: {
      enabled: true
      days: 7
    }
    containerDeleteRetentionPolicy: {
      enabled: true
      days: 7
    }
  }
}

resource sources 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: containerName
  properties: {
    publicAccess: 'None'
  }
}

resource appBlobData 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, appPrincipalId, blobDataContributorRoleId)
  scope: account
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      blobDataContributorRoleId
    )
    principalId: appPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource developerBlobData 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(developerPrincipalId)) {
  name: guid(account.id, developerPrincipalId, blobDataContributorRoleId)
  scope: account
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      blobDataContributorRoleId
    )
    principalId: developerPrincipalId
    principalType: 'User'
  }
}

output name string = account.name
output blobEndpoint string = account.properties.primaryEndpoints.blob
output containerName string = sources.name
