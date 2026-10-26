// Log Analytics workspace: Container Apps system and console logs land here.

param location string
param tags object
param name string

@description('Days to keep logs.')
param retentionInDays int = 30

@description('Daily ingestion cap in GB (cost guard for dev). -1 means no cap.')
param dailyQuotaGb int = 1

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: retentionInDays
    workspaceCapping: {
      dailyQuotaGb: dailyQuotaGb
    }
  }
}

output id string = workspace.id
output name string = workspace.name
