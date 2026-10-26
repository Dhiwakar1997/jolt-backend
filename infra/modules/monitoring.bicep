// Log Analytics workspace (Container Apps system and console logs) and a
// workspace-based Application Insights (BE-10): the app's OpenTelemetry traces,
// logs and custom metrics (jolt/telemetry.py) land in the same workspace. Plus
// the Azure Monitor workbook that charts those metrics (../workbook.json).

param location string
param tags object
param name string

@description('Application Insights resource name.')
param appInsightsName string

@description('Days to keep logs (workspace and Application Insights).')
param retentionInDays int = 30

@description('Daily ingestion cap in GB (cost guard). -1 means no cap.')
param dailyQuotaGb int = 1

@description('Display name of the workbook.')
param workbookDisplayName string = 'Jolt API'

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

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspace.id
    IngestionMode: 'LogAnalytics'
    RetentionInDays: retentionInDays
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

// Workbook ids must be GUIDs; this one is stable per resource group.
resource workbook 'Microsoft.Insights/workbooks@2023-06-01' = {
  name: guid(resourceGroup().id, 'jolt-api-workbook')
  location: location
  tags: tags
  kind: 'shared'
  properties: {
    displayName: workbookDisplayName
    category: 'workbook'
    sourceId: appInsights.id
    serializedData: string(loadJsonContent('../workbook.json'))
  }
}

output id string = workspace.id
output name string = workspace.name
output appInsightsId string = appInsights.id
output appInsightsName string = appInsights.name
output workbookId string = workbook.id
