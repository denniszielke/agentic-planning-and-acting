from __future__ import annotations

import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from azure.ai.agentserver.core.storage import FoundryStorageConflictError, FoundryStoragePreconditionError

from src.hosted_hypothesis_agent.models import PhaseRecord, WorkflowEvent, WorkflowMeta, utc_now
from src.hosted_hypothesis_agent.state_store import FoundryWorkflowStore, WorkflowConflictError


class FakeFoundryStore:
    def __init__(self):
        self.items = {}
        self.calls = []

    async def create_item(self, key, value, *, tags=None, call_id=None):
        self.calls.append(("create", key, call_id))
        if key in self.items:
            raise FoundryStorageConflictError("duplicate")
        self.items[key] = (value, '"1"')
        return SimpleNamespace(key=key, etag='"1"')

    async def get_item(self, key, *, call_id=None):
        self.calls.append(("get", key, call_id))
        value = self.items.get(key)
        return SimpleNamespace(key=key, value=value[0], etag=value[1]) if value else None

    async def set_item(self, key, value, *, if_match=None, tags=None, call_id=None):
        self.calls.append(("set", key, call_id))
        current = self.items.get(key)
        if current is None or current[1] != if_match:
            raise FoundryStoragePreconditionError("etag", current_etag=current[1])
        self.items[key] = (value, '"2"')
        return SimpleNamespace(key=key, etag='"2"')


class FakeConflictError(Exception):
    pass


class StateStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeFoundryStore()
        self.created = []

        async def get_or_create(name, **kwargs):
            self.created.append((name, kwargs))
            return self.fake

        self.patch_state = patch("azure.ai.agentserver.core.storage.FoundryStateStore.get_or_create", side_effect=get_or_create)
        self.patch_credential = patch("azure.identity.aio.DefaultAzureCredential", return_value=object())
        self.patch_state.start()
        self.patch_credential.start()
        self.addAsyncCleanup(self._cleanup)
        self.store = FoundryWorkflowStore("caller-1")
        self.phase = PhaseRecord(phase_id="wf-p001", workflow_id="wf", ordinal=1, origin="initial", user_input="scenario")
        self.meta = WorkflowMeta(workflow_id="wf", owner_id="caller-1", title="scenario",
                                 expires_at=utc_now() + timedelta(days=7), phase_ids=[self.phase.phase_id], tail_phase_id=self.phase.phase_id)

    async def _cleanup(self):
        self.patch_state.stop()
        self.patch_credential.stop()

    async def test_foundry_store_persists_with_user_isolation_and_call_id(self):
        await self.store.create(self.meta, self.phase, call_id="call-1")
        loaded = await self.store.get_meta("wf", call_id="call-1")
        self.assertEqual(loaded.meta.owner_id, "caller-1")
        self.assertTrue(self.created[0][1]["user_isolation"])
        self.assertEqual(self.created[0][1]["user_id"], "caller-1")
        self.assertTrue(all(call[2] == "call-1" for call in self.fake.calls))

    async def test_foundry_store_compare_and_swap_and_event_replay(self):
        await self.store.create(self.meta, self.phase)
        loaded = await self.store.get_meta("wf")
        stale = await self.store.get_meta("wf")
        loaded.meta.title = "changed"
        updated = await self.store.save_meta(loaded.meta, loaded.etag)
        self.assertEqual(updated.meta.version, 1)
        stale.meta.title = "stale"
        with self.assertRaises(WorkflowConflictError):
            await self.store.save_meta(stale.meta, stale.etag)
        event = WorkflowEvent(seq=0, event_id="event-1", workflow_id="wf", phase_id="wf-p001",
                              actor="human", actor_id="hash", type="human.request")
        written = await self.store.append_event("wf", "wf-p001", event)
        self.assertEqual(written.seq, 1)
        page = await self.store.events("wf", after_seq=0)
        self.assertEqual([item.event_id for item in page], ["event-1"])

    async def test_captured_call_id_is_forwarded_without_fallback(self):
        await self.store.create(self.meta, self.phase, call_id="old-call")
        self.fake.calls.clear()
        await self.store.get_meta("wf", call_id="old-call")
        self.assertEqual(self.fake.calls[0][2], "old-call")


if __name__ == "__main__":
    unittest.main()
