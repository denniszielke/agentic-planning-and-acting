targetScope = 'resourceGroup'

@description('Existing external Cognitive Services account name')
param accountName string

@description('Hosted-version managed identity object ID')
param principalId string

@description('Adopt an existing Cognitive Services OpenAI User assignment GUID on upgrades')
param assignmentName string = ''

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

resource modelUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: account
  name: empty(assignmentName) ? guid(account.id, principalId, '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd') : assignmentName
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
  }
}
