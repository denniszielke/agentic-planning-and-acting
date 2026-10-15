targetScope = 'resourceGroup'

@description('Existing Foundry account containing the project')
param accountName string

@description('Hosted-version or account managed identity object ID')
param principalId string

@description('Grant direct model inference in addition to Foundry tools/state')
param includeInference bool = true

@description('Adopt an existing Foundry User assignment GUID on upgrades, if present')
param foundryAssignmentName string = ''

@description('Adopt an existing OpenAI User assignment GUID on upgrades, if present')
param inferenceAssignmentName string = ''

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

// Foundry User has Microsoft.CognitiveServices/* data actions. State-store caller
// isolation still requires the opaque platform call ID; RBAC is not impersonation.
resource foundryUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: account
  name: empty(foundryAssignmentName) ? guid(account.id, principalId, '53ca6127-db72-4b80-b1b0-d745d6d5456d') : foundryAssignmentName
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '53ca6127-db72-4b80-b1b0-d745d6d5456d')
  }
}

resource modelUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (includeInference) {
  scope: account
  name: empty(inferenceAssignmentName) ? guid(account.id, principalId, '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd') : inferenceAssignmentName
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
  }
}
