// Cosmos DB for NoSQL, serverless. Database + every container, partition key and
// composite index from LLD §6, read from ../cosmos-containers.json (a test keeps
// that file equal to jolt/data/cosmos.py). The app reaches it with its Managed
// Identity through data-plane RBAC; account keys are off unless allowKeyAuth.

param location string
param tags object
param accountName string
param databaseName string

@description('Principal id of the app identity (gets Built-in Data Contributor on the database).')
param appPrincipalId string

@description('Optional Entra object id of a developer who also gets data access (local runs with az login).')
param developerPrincipalId string = ''

@description('Keep account keys enabled (local dev with COSMOS_KEY). Off by default: Managed Identity only.')
param allowKeyAuth bool = false

var containerDefs = loadJsonContent('../cosmos-containers.json').containers

// Cosmos DB Built-in Data Contributor (data-plane role, not an ARM role).
var dataContributorRoleId = '00000000-0000-0000-0000-000000000002'

// API 2024-05-15: the newest GA version the pinned Bicep CLI has types for, so
// every property below is validated at build time (newer versions give BCP081).
resource account 'Microsoft.DocumentDB/databaseAccounts@2024-05-15' = {
  name: accountName
  location: location
  tags: tags
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    capabilities: [
      {
        name: 'EnableServerless'
      }
    ]
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    disableLocalAuth: !allowKeyAuth
    minimalTlsVersion: 'Tls12'
    publicNetworkAccess: 'Enabled'
    backupPolicy: {
      type: 'Periodic'
      periodicModeProperties: {
        backupIntervalInMinutes: 1440
        backupRetentionIntervalInHours: 48
        backupStorageRedundancy: 'Local'
      }
    }
  }
}

resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-05-15' = {
  parent: account
  name: databaseName
  properties: {
    resource: {
      id: databaseName
    }
  }
}

resource containers 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = [
  for c in containerDefs: {
    parent: database
    name: c.name
    properties: {
      resource: {
        id: c.name
        partitionKey: {
          paths: [
            c.partitionKey
          ]
          kind: 'Hash'
          version: 2
        }
        indexingPolicy: {
          automatic: true
          indexingMode: 'consistent'
          includedPaths: [
            {
              path: '/*'
            }
          ]
          excludedPaths: [
            {
              path: '/"_etag"/?'
            }
          ]
          compositeIndexes: c.compositeIndexes
        }
      }
    }
  }
]

resource appData 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = {
  parent: account
  name: guid(account.id, appPrincipalId, dataContributorRoleId)
  properties: {
    roleDefinitionId: '${account.id}/sqlRoleDefinitions/${dataContributorRoleId}'
    principalId: appPrincipalId
    scope: '${account.id}/dbs/${databaseName}'
  }
  dependsOn: [
    database
  ]
}

resource developerData 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = if (!empty(developerPrincipalId)) {
  parent: account
  name: guid(account.id, developerPrincipalId, dataContributorRoleId)
  properties: {
    roleDefinitionId: '${account.id}/sqlRoleDefinitions/${dataContributorRoleId}'
    principalId: developerPrincipalId
    scope: '${account.id}/dbs/${databaseName}'
  }
  dependsOn: [
    database
  ]
}

output endpoint string = account.properties.documentEndpoint
output name string = account.name
output databaseName string = database.name
output containerNames array = [for (c, i) in containerDefs: containers[i].name]
