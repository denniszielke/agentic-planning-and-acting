import assert from 'node:assert/strict';
import test from 'node:test';
import {HttpAgent, EventType} from '@ag-ui/client';
import {nanoid} from 'nanoid';
import {v4, validate} from 'uuid';

test('AG-UI event pipeline survives the aged transitive major upgrades', async () => {
  const frames = [
    {type: EventType.RUN_STARTED, threadId: 't', runId: 'r'},
    {type: EventType.STATE_SNAPSHOT, snapshot: {workflow: {workflow_id: 'wf', phase: {stage: 'awaiting_approval'}}}},
    {type: EventType.RUN_FINISHED, threadId: 't', runId: 'r'},
  ].map(event => `data: ${JSON.stringify(event)}\n\n`).join('');
  const agent = new HttpAgent({
    url: 'https://example.invalid/api/agent', threadId: 't',
    fetch: async () => new Response(frames, {headers: {'content-type': 'text/event-stream'}}),
  });
  await agent.runAgent({runId: 'r'});
  assert.equal(agent.state.workflow.workflow_id, 'wf');
  assert.equal(agent.state.workflow.phase.stage, 'awaiting_approval');
  assert.equal(validate(v4()), true);
  assert.ok(nanoid().length > 0);
});
