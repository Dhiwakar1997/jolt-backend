// Managed (free, auto-renewed) TLS certificate for the API's custom domain
// (BE-10). Issued after the hostname is on the app (containerapp.bicep adds it
// unbound on the first deploy) and DNS validates it:
//   CNAME <domain>        -> <app>.<environment default domain>
//   TXT   asuid.<domain>  -> the environment's customDomainVerificationId
// The next deploy (customDomainCertificateReady=true) binds it with SNI.

param location string
param tags object
param containerAppsEnvironmentName string
param certificateName string

@description('The custom domain, e.g. api.jolt.example.com (a subdomain: CNAME validation).')
param subjectName string

resource managedEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: containerAppsEnvironmentName
}

resource certificate 'Microsoft.App/managedEnvironments/managedCertificates@2024-03-01' = {
  parent: managedEnvironment
  name: certificateName
  location: location
  tags: tags
  properties: {
    subjectName: subjectName
    domainControlValidation: 'CNAME'
  }
}

output id string = certificate.id
