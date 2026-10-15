param name string
param location string = resourceGroup().location
param tags object = {}
param containerAppsEnvironmentName string
param containerName string = 'main'
@description('Bare registry name without .azurecr.io')
param containerRegistryName string
param external bool = true
param imageName string
param targetPort int = 80
param identityName string
@description('JSON array of {name, value} environment variable objects')
param envJson string = '[]'
param containerCpuCoreCount string = '1'
param containerMemory string = '2.0Gi'
param minReplicas int = 0
param maxReplicas int = 2
param concurrentRequests string = '10'
param readinessProbePath string = ''

var envVars = json(envJson)
var acrPullRoleDefinitionId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')

resource userIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: identityName
}

resource containerRegistry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: containerRegistryName
}

resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2025-07-01' existing = {
  name: containerAppsEnvironmentName
}

resource acrPullRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(containerRegistry.id, userIdentity.id, acrPullRoleDefinitionId)
  scope: containerRegistry
  properties: {
    roleDefinitionId: acrPullRoleDefinitionId
    principalId: userIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource app 'Microsoft.App/containerApps@2023-05-01' = {
  name: name
  location: location
  tags: tags
  dependsOn: [acrPullRoleAssignment]
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${userIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerAppsEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: external
        targetPort: targetPort
        transport: 'auto'
      }
      registries: [
        {
          server: containerRegistry.properties.loginServer
          identity: userIdentity.id
        }
      ]
    }
    template: {
      containers: [
        {
          image: imageName
          name: containerName
          env: envVars
          resources: {
            cpu: json(containerCpuCoreCount)
            memory: containerMemory
          }
          probes: empty(readinessProbePath) ? [] : [
            {
              type: 'readiness'
              httpGet: {
                path: readinessProbePath
                port: targetPort
              }
              initialDelaySeconds: 5
              periodSeconds: 10
              failureThreshold: 3
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: [
          {
            name: 'http-rule'
            http: {
              metadata: {
                concurrentRequests: concurrentRequests
              }
            }
          }
        ]
      }
    }
  }
}

output uri string = 'https://${app.properties.configuration.ingress.fqdn}'