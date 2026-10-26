// Alerts and the resource-group budget (BE-10). One action group e-mails the
// alert recipients; every rule below notifies it:
//
//   * Cosmos RU:      total request units per hour above cosmosRuPerHour
//   * Blob capacity:  stored bytes above blobCapacityGb
//   * 5xx count:      Container Apps `Requests` with status 5xx in 5 minutes
//   * 5xx rate:       share of failed requests in Application Insights over 15
//                     minutes (at least 20 requests) above http5xxRatePercent
//   * Budget:         actual cost at 80 % and forecast at 100 % of budgetAmount
//
// Test the e-mail path without waiting for a real incident:
//   az monitor action-group test-notifications create -g <rg> \
//     --action-group <name> --alert-type budget   (docs/DEPLOY.md, "Alerts")

param location string
param tags object
param environmentName string

@description('E-mail addresses that receive alerts and budget notifications.')
@minLength(1)
param alertEmails array

param cosmosAccountId string
param storageAccountId string
param appInsightsId string

@description('Container App resource id; empty skips the 5xx count rule (no app yet).')
param containerAppId string = ''

@description('Cosmos request units per hour that raise an alert (serverless bills per RU).')
param cosmosRuPerHour int

@description('Blob capacity in GiB that raises an alert.')
param blobCapacityGb int

@description('5xx responses in 5 minutes that raise an alert.')
param http5xxCount int

@description('Failed-request percentage over 15 minutes that raises an alert.')
param http5xxRatePercent int

@description('Monthly budget for this resource group in the billing currency. 0: no budget.')
param budgetAmount int = 0

@description('First day of the budget period, yyyy-MM-01. Required with a budget.')
param budgetStartDate string = ''

var prefix = 'jolt-${environmentName}'

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: 'ag-${prefix}'
  location: 'global'
  tags: tags
  properties: {
    groupShortName: take(prefix, 12)
    enabled: true
    emailReceivers: [
      for (email, i) in alertEmails: {
        name: 'email-${i}'
        emailAddress: email
        useCommonAlertSchema: true
      }
    ]
  }
}

var actions = [
  {
    actionGroupId: actionGroup.id
  }
]

resource cosmosRu 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: '${prefix}-cosmos-ru'
  location: 'global'
  tags: tags
  properties: {
    description: 'Cosmos DB request units in the last hour above ${cosmosRuPerHour} (serverless cost).'
    severity: 2
    enabled: true
    scopes: [
      cosmosAccountId
    ]
    evaluationFrequency: 'PT15M'
    windowSize: 'PT1H'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          criterionType: 'StaticThresholdCriterion'
          name: 'TotalRequestUnits'
          metricNamespace: 'Microsoft.DocumentDB/databaseAccounts'
          metricName: 'TotalRequestUnits'
          operator: 'GreaterThan'
          threshold: cosmosRuPerHour
          timeAggregation: 'Total'
        }
      ]
    }
    autoMitigate: true
    actions: actions
  }
}

resource blobCapacity 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: '${prefix}-blob-capacity'
  location: 'global'
  tags: tags
  properties: {
    description: 'Blob storage above ${blobCapacityGb} GiB.'
    severity: 3
    enabled: true
    scopes: [
      '${storageAccountId}/blobServices/default'
    ]
    // BlobCapacity is emitted about once an hour.
    evaluationFrequency: 'PT1H'
    windowSize: 'PT6H'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          criterionType: 'StaticThresholdCriterion'
          name: 'BlobCapacity'
          metricNamespace: 'Microsoft.Storage/storageAccounts/blobServices'
          metricName: 'BlobCapacity'
          operator: 'GreaterThan'
          threshold: blobCapacityGb * 1024 * 1024 * 1024
          timeAggregation: 'Average'
        }
      ]
    }
    autoMitigate: true
    actions: actions
  }
}

resource http5xx 'Microsoft.Insights/metricAlerts@2018-03-01' = if (!empty(containerAppId)) {
  name: '${prefix}-http-5xx'
  location: 'global'
  tags: tags
  properties: {
    description: 'More than ${http5xxCount} 5xx responses from jolt-api in 5 minutes.'
    severity: 1
    enabled: true
    scopes: [
      containerAppId
    ]
    evaluationFrequency: 'PT1M'
    windowSize: 'PT5M'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          criterionType: 'StaticThresholdCriterion'
          name: 'Requests5xx'
          metricNamespace: 'Microsoft.App/containerApps'
          metricName: 'Requests'
          dimensions: [
            {
              name: 'statusCodeCategory'
              operator: 'Include'
              values: [
                '5xx'
              ]
            }
          ]
          operator: 'GreaterThan'
          threshold: http5xxCount
          timeAggregation: 'Total'
        }
      ]
    }
    autoMitigate: true
    actions: actions
  }
}

resource http5xxRate 'Microsoft.Insights/scheduledQueryRules@2023-12-01' = {
  name: '${prefix}-http-5xx-rate'
  location: location
  tags: tags
  kind: 'LogAlert'
  properties: {
    displayName: '${prefix}: 5xx rate above ${http5xxRatePercent}%'
    description: 'Share of failed requests (5xx) over 15 minutes, from Application Insights, with at least 20 requests.'
    severity: 1
    enabled: true
    scopes: [
      appInsightsId
    ]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    criteria: {
      allOf: [
        {
          query: 'requests | summarize total = count(), failed = countif(toint(resultCode) >= 500) | where total >= 20 and 100.0 * failed / total > ${http5xxRatePercent}'
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    autoMitigate: true
    actions: {
      actionGroups: [
        actionGroup.id
      ]
    }
  }
}

resource budget 'Microsoft.Consumption/budgets@2023-11-01' = if (budgetAmount > 0) {
  name: '${prefix}-monthly'
  properties: {
    category: 'Cost'
    amount: budgetAmount
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
          actionGroup.id
        ]
      }
      forecast100: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 100
        thresholdType: 'Forecasted'
        contactEmails: alertEmails
        contactGroups: [
          actionGroup.id
        ]
      }
    }
  }
}

output actionGroupId string = actionGroup.id
output actionGroupName string = actionGroup.name
