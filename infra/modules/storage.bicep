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

@description('Days deleted blobs and containers stay recoverable (soft delete).')
@minValue(1)
@maxValue(365)
param softDeleteDays int = 7

@description('Blob versioning: an overwritten or deleted blob keeps its previous version, removed by a lifecycle rule after softDeleteDays.')
param versioning bool = false

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
      days: softDeleteDays
    }
    containerDeleteRetentionPolicy: {
      enabled: true
      days: softDeleteDays
    }
    isVersioningEnabled: versioning
  }
}

resource sources 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: containerName
  properties: {
    publicAccess: 'None'
  }
}

// Data exports (POST /v1/export, BE-08) are written under `exports/` in the
// container and handed out as a short-lived signed URL. The next export and
// account delete remove them; this rule removes any left after a day.
var expireExports = {
  name: 'expire-exports'
  enabled: true
  type: 'Lifecycle'
  definition: {
    filters: {
      blobTypes: [
        'blockBlob'
      ]
      prefixMatch: [
        '${containerName}/exports/'
      ]
    }
    actions: {
      baseBlob: {
        delete: {
          daysAfterModificationGreaterThan: 1
        }
      }
    }
  }
}

// With versioning, previous versions (overwritten or deleted blobs) go after
// the same window soft delete gives, so deleted data does not linger.
var expireVersions = {
  name: 'expire-previous-versions'
  enabled: true
  type: 'Lifecycle'
  definition: {
    filters: {
      blobTypes: [
        'blockBlob'
      ]
    }
    actions: {
      version: {
        delete: {
          daysAfterCreationGreaterThan: softDeleteDays
        }
      }
    }
  }
}

resource lifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = {
  parent: account
  name: 'default'
  properties: {
    policy: {
      rules: versioning ? [expireExports, expireVersions] : [expireExports]
    }
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
output id string = account.id
output blobEndpoint string = account.properties.primaryEndpoints.blob
output containerName string = sources.name
