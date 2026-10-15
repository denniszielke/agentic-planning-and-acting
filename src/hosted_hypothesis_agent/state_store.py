"""Workflow persistence over FoundryStateStore with an ETag-backed test fake."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import uuid
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from .models import ActiveRun, PhaseRecord, WorkflowEvent, WorkflowMeta, WorkflowStatus, utc_now

logger = logging.getLogger(__name__)
MAX_ITEM_BYTES = 900_000


class WorkflowNotFoundError(KeyError):
    pass


class PhaseNotFoundError(WorkflowNotFoundError):
    pass


class WorkflowConflictError(RuntimeError):
    def __init__(self, message: str, current: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.current = current


class WorkflowExpiredError(RuntimeError):
    pass


class SizeLimitError(ValueError):
    pass


@dataclass
class StoredWorkflow:
    meta: WorkflowMeta
    etag: str | None


class WorkflowStore(ABC):
    @abstractmethod
    async def create(self, meta: WorkflowMeta, phase: PhaseRecord, call_id: str | None = None) -> None: ...
    @abstractmethod
    async def get_meta(self, workflow_id: str, call_id: str | None = None) -> StoredWorkflow: ...
    @abstractmethod
    async def get_phase(self, workflow_id: str, phase_id: str, call_id: str | None = None) -> PhaseRecord: ...
    @abstractmethod
    async def phases(self, workflow_id: str, meta: WorkflowMeta, call_id: str | None = None) -> list[PhaseRecord]: ...
    @abstractmethod
    async def save_meta(self, meta: WorkflowMeta, etag: str | None, call_id: str | None = None) -> StoredWorkflow: ...
    @abstractmethod
    async def create_phase(self, phase: PhaseRecord, call_id: str | None = None) -> PhaseRecord: ...
    @abstractmethod
    async def save_meta_and_claim(self, meta: WorkflowMeta, etag: str | None, phase: PhaseRecord, input_id: str,
                                 call_id: str | None = None) -> StoredWorkflow: ...
    @abstractmethod
    async def save_phase(self, phase: PhaseRecord, call_id: str | None = None) -> PhaseRecord: ...
    @abstractmethod
    async def request(self, workflow_id: str, request_id: str, call_id: str | None = None) -> dict[str, Any] | None: ...
    @abstractmethod
    async def create_request(self, workflow_id: str, request_id: str, value: dict[str, Any], call_id: str | None = None) -> None: ...
    @abstractmethod
    async def append_event(self, workflow_id: str, phase_id: str, event: WorkflowEvent, call_id: str | None = None) -> WorkflowEvent: ...
    @abstractmethod
    async def events(self, workflow_id: str, after_seq: int = 0, phase_id: str | None = None, call_id: str | None = None) -> list[WorkflowEvent]: ...
    @abstractmethod
    async def save_checkpoint(self, workflow_id: str, phase_id: str, label: str, value: dict[str, Any], call_id: str | None = None) -> str: ...
    @abstractmethod
    async def get_checkpoint(self, ref: str, call_id: str | None = None) -> dict[str, Any] | None: ...
    @abstractmethod
    async def get_tool_result(self, workflow_id: str, phase_id: str, key: str, call_id: str | None = None) -> Any: ...
    @abstractmethod
    async def save_tool_result(self, workflow_id: str, phase_id: str, key: str, value: Any, call_id: str | None = None) -> None: ...


class InMemoryWorkflowStore(WorkflowStore):
    """Concurrency-safe local fake matching create-once and version semantics."""
    def __init__(self) -> None:
        self._meta: dict[str, WorkflowMeta] = {}
        self._phases: dict[str, PhaseRecord] = {}
        self._requests: dict[str, dict[str, Any]] = {}
        self._events: dict[str, WorkflowEvent] = {}
        self._checkpoints: dict[str, dict[str, Any]] = {}
        self._tools: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    async def create(self, meta, phase, call_id=None):
        async with self._lock:
            if meta.workflow_id in self._meta:
                raise WorkflowConflictError("workflow already exists")
            self._meta[meta.workflow_id] = meta.model_copy(deep=True)
            self._phases[phase.phase_id] = phase.model_copy(deep=True)

    async def get_meta(self, workflow_id, call_id=None):
        async with self._lock:
            if workflow_id not in self._meta:
                raise WorkflowNotFoundError(workflow_id)
            meta = self._meta[workflow_id].model_copy(deep=True)
            return StoredWorkflow(meta, str(meta.version))

    async def get_phase(self, workflow_id, phase_id, call_id=None):
        async with self._lock:
            phase = self._phases.get(phase_id)
            if phase is None or phase.workflow_id != workflow_id:
                raise PhaseNotFoundError(phase_id)
            return phase.model_copy(deep=True)

    async def phases(self, workflow_id, meta, call_id=None):
        async with self._lock:
            return [self._phases[p].model_copy(deep=True) for p in meta.phase_ids if p in self._phases]

    async def save_meta(self, meta, etag, call_id=None):
        async with self._lock:
            current = self._meta.get(meta.workflow_id)
            if current is None:
                raise WorkflowNotFoundError(meta.workflow_id)
            if etag is not None and str(current.version) != etag:
                raise WorkflowConflictError("workflow changed concurrently")
            updated = meta.model_copy(deep=True)
            updated.version = current.version + 1
            updated.updated_at = utc_now()
            updated.expires_at = utc_now() + timedelta(hours=int(os.getenv("WORKFLOW_TTL_HOURS", "168")))
            self._meta[meta.workflow_id] = updated
            return StoredWorkflow(updated.model_copy(deep=True), str(updated.version))

    async def create_phase(self, phase, call_id=None):
        async with self._lock:
            if phase.phase_id in self._phases:
                raise WorkflowConflictError("phase already exists")
            self._phases[phase.phase_id] = phase.model_copy(deep=True)
            return phase.model_copy(deep=True)

    async def save_meta_and_claim(self, meta, etag, phase, input_id, call_id=None):
        async with self._lock:
            current = self._meta.get(meta.workflow_id)
            if current is None:
                raise WorkflowNotFoundError(meta.workflow_id)
            if current.active_run is not None or (etag is not None and str(current.version) != etag):
                raise WorkflowConflictError("phase_in_progress: another action claimed the workflow")
            updated_meta = meta.model_copy(deep=True)
            updated_meta.active_run = ActiveRun(phase_id=phase.phase_id, task_id=meta.workflow_id, input_id=input_id,
                invocation_id=uuid.uuid4().hex, stream_id=uuid.uuid4().hex, stage=phase.stage, call_id=call_id)
            updated_meta.version = current.version + 1
            updated_meta.updated_at = utc_now()
            updated_meta.expires_at = utc_now() + timedelta(hours=int(os.getenv("WORKFLOW_TTL_HOURS", "168")))
            self._meta[meta.workflow_id] = updated_meta
            return StoredWorkflow(updated_meta.model_copy(deep=True), str(updated_meta.version))

    async def save_phase(self, phase, call_id=None):
        async with self._lock:
            current = self._phases.get(phase.phase_id)
            if current is not None and current.version != phase.version:
                raise WorkflowConflictError("phase changed concurrently")
            updated = phase.model_copy(deep=True)
            updated.version += 1
            self._phases[updated.phase_id] = updated
            return updated.model_copy(deep=True)

    async def request(self, workflow_id, request_id, call_id=None):
        async with self._lock:
            value = self._requests.get(f"{workflow_id}:{request_id}")
            return copy.deepcopy(value)

    async def create_request(self, workflow_id, request_id, value, call_id=None):
        async with self._lock:
            key = f"{workflow_id}:{request_id}"
            if key in self._requests:
                raise WorkflowConflictError("request already exists")
            self._requests[key] = copy.deepcopy(value)

    async def append_event(self, workflow_id, phase_id, event, call_id=None):
        async with self._lock:
            meta = self._meta.get(workflow_id)
            if meta is None:
                raise WorkflowNotFoundError(workflow_id)
            meta = meta.model_copy(deep=True)
            meta.event_seq += 1
            meta.version += 1
            self._meta[workflow_id] = meta
            stored = event.model_copy(update={"seq": meta.event_seq, "persisted_at": utc_now()}, deep=True)
            key = f"{workflow_id}:{stored.seq}"
            self._events[key] = stored
            return stored.model_copy(deep=True)

    async def save_checkpoint(self, workflow_id, phase_id, label, value, call_id=None):
        ref = f"{workflow_id}:{phase_id}:{label}"
        async with self._lock:
            self._checkpoints.setdefault(ref, copy.deepcopy(value))
        return ref

    async def get_checkpoint(self, ref, call_id=None):
        async with self._lock:
            return copy.deepcopy(self._checkpoints.get(ref))

    async def get_tool_result(self, workflow_id, phase_id, key, call_id=None):
        async with self._lock:
            return copy.deepcopy(self._tools.get(f"{workflow_id}:{phase_id}:{key}"))

    async def save_tool_result(self, workflow_id, phase_id, key, value, call_id=None):
        async with self._lock:
            self._tools.setdefault(f"{workflow_id}:{phase_id}:{key}", copy.deepcopy(value))

    async def events(self, workflow_id, after_seq=0, phase_id=None, call_id=None):
        async with self._lock:
            values = [e.model_copy(deep=True) for e in self._events.values()
                      if e.workflow_id == workflow_id and e.seq > after_seq and (phase_id is None or e.phase_id == phase_id)]
            return sorted(values, key=lambda e: e.seq)


class FoundryWorkflowStore(WorkflowStore):
    """FoundryStateStore adapter. A provided call id is always used; errors never fall back."""
    def __init__(self, owner_id: str, store_names: tuple[str, str, str] = ("lra-workflows", "lra-events", "lra-checkpoints"), ttl_seconds: int = 7 * 24 * 3600) -> None:
        self.owner_id = owner_id
        self.store_names = store_names
        self.ttl_seconds = ttl_seconds
        self._stores: dict[str, Any] = {}
        self._credential: Any | None = None

    async def _store(self, name: str, call_id: str | None):
        if name not in self._stores:
            from azure.ai.agentserver.core.storage import FoundryStateStore
            from azure.identity.aio import DefaultAzureCredential
            if self._credential is None:
                self._credential = DefaultAzureCredential()
            endpoint = os.getenv("FOUNDRY_PROJECT_ENDPOINT") or os.getenv("AZURE_AI_PROJECT_ENDPOINT")
            self._stores[name] = await FoundryStateStore.get_or_create(
                name, credential=self._credential, endpoint=endpoint, user_isolation=True, user_id=self.owner_id,
                item_ttl_seconds=self.ttl_seconds, tags={"app": "lra", "schema": "1"},
            )
        return self._stores[name]

    @staticmethod
    def _key(workflow_id: str, kind: str, suffix: str = "") -> str:
        key = f"wf.{workflow_id}.{kind}{suffix}"
        if len(key) > 128:
            raise ValueError("state-store item key exceeds 128 characters")
        return key

    @staticmethod
    def _json(model: Any) -> dict[str, Any]:
        raw = model.model_dump(mode="json")
        size = len(json.dumps(raw, separators=(",", ":")).encode())
        if size > MAX_ITEM_BYTES:
            raise SizeLimitError(f"state item is too large: {size} bytes")
        return raw

    async def create(self, meta, phase, call_id=None):
        store = await self._store(self.store_names[0], call_id)
        try:
            await store.create_item(self._key(meta.workflow_id, "p.", "001"), self._json(phase), tags={"wf": meta.workflow_id, "kind": "phase", "phase": "001"}, call_id=call_id)
            await store.create_item(self._key(meta.workflow_id, "meta"), self._json(meta), tags={"wf": meta.workflow_id, "kind": "meta"}, call_id=call_id)
        except Exception as exc:
            from azure.ai.agentserver.core.storage import FoundryStorageConflictError
            if isinstance(exc, FoundryStorageConflictError):
                raise WorkflowConflictError("workflow already exists") from exc
            raise

    async def _get(self, store, key, call_id):
        item = await store.get_item(key, call_id=call_id)
        return item

    async def get_meta(self, workflow_id, call_id=None):
        store = await self._store(self.store_names[0], call_id)
        item = await self._get(store, self._key(workflow_id, "meta"), call_id)
        if item is None:
            raise WorkflowNotFoundError(workflow_id)
        return StoredWorkflow(WorkflowMeta.model_validate(item.value), item.etag)

    async def get_phase(self, workflow_id, phase_id, call_id=None):
        stored = await self.get_meta(workflow_id, call_id)
        try:
            ordinal = stored.meta.phase_ids.index(phase_id) + 1
        except ValueError as exc:
            raise PhaseNotFoundError(phase_id) from exc
        store = await self._store(self.store_names[0], call_id)
        item = await self._get(store, self._key(workflow_id, "p.", f"{ordinal:03d}"), call_id)
        if item is None:
            raise PhaseNotFoundError(phase_id)
        return PhaseRecord.model_validate(item.value)

    async def phases(self, workflow_id, meta, call_id=None):
        result = []
        for phase_id in meta.phase_ids:
            result.append(await self.get_phase(workflow_id, phase_id, call_id))
        return result

    async def save_meta(self, meta, etag, call_id=None):
        store = await self._store(self.store_names[0], call_id)
        meta.version += 1
        meta.updated_at = utc_now()
        meta.expires_at = utc_now() + timedelta(hours=int(os.getenv("WORKFLOW_TTL_HOURS", "168")))
        try:
            ref = await store.set_item(self._key(meta.workflow_id, "meta"), self._json(meta), if_match=etag, tags={"wf": meta.workflow_id, "kind": "meta"}, call_id=call_id)
        except Exception as exc:
            from azure.ai.agentserver.core.storage import FoundryStoragePreconditionError, FoundryStorageConflictError
            if isinstance(exc, (FoundryStoragePreconditionError, FoundryStorageConflictError)):
                raise WorkflowConflictError("workflow changed concurrently") from exc
            raise
        return StoredWorkflow(meta, getattr(ref, "etag", None))

    async def save_meta_and_claim(self, meta, etag, phase, input_id, call_id=None):
        current = await self.get_meta(meta.workflow_id, call_id)
        if current.meta.active_run is not None:
            raise WorkflowConflictError("phase_in_progress: another action claimed the workflow")
        active = ActiveRun(phase_id=phase.phase_id, task_id=meta.workflow_id, input_id=input_id,
                           invocation_id=uuid.uuid4().hex, stream_id=uuid.uuid4().hex, stage=phase.stage,
                           call_id=call_id)
        updated = meta.model_copy(deep=True)
        updated.active_run = active
        updated.status = WorkflowStatus.ACTIVE
        return await self.save_meta(updated, etag, call_id)

    async def create_phase(self, phase, call_id=None):
        meta = await self.get_meta(phase.workflow_id, call_id)
        ordinal = len(meta.meta.phase_ids) + 1
        store = await self._store(self.store_names[0], call_id)
        key = self._key(phase.workflow_id, "p.", f"{ordinal:03d}")
        try:
            await store.create_item(key, self._json(phase), tags={"wf": phase.workflow_id, "kind": "phase", "phase": f"{ordinal:03d}", "stage": phase.stage.value}, call_id=call_id)
        except Exception as exc:
            from azure.ai.agentserver.core.storage import FoundryStorageConflictError
            if isinstance(exc, FoundryStorageConflictError):
                raise WorkflowConflictError("phase already exists") from exc
            raise
        return phase

    async def save_phase(self, phase, call_id=None):
        meta = await self.get_meta(phase.workflow_id, call_id)
        ordinal = meta.meta.phase_ids.index(phase.phase_id) + 1
        store = await self._store(self.store_names[0], call_id)
        key = self._key(phase.workflow_id, "p.", f"{ordinal:03d}")
        existing = await store.get_item(key, call_id=call_id)
        if existing is None:
            raise WorkflowNotFoundError(phase.phase_id)
        if phase.version != int(existing.value.get("version", 0)):
            raise WorkflowConflictError("phase changed concurrently")
        updated_phase = phase.model_copy(deep=True)
        updated_phase.version += 1
        try:
            await store.set_item(key, self._json(updated_phase), if_match=existing.etag, tags={"wf": phase.workflow_id, "kind": "phase", "phase": f"{ordinal:03d}", "stage": phase.stage.value}, call_id=call_id)
        except Exception as exc:
            from azure.ai.agentserver.core.storage import FoundryStoragePreconditionError, FoundryStorageConflictError
            if isinstance(exc, (FoundryStoragePreconditionError, FoundryStorageConflictError)):
                raise WorkflowConflictError("phase changed concurrently") from exc
            raise
        phase.version = updated_phase.version
        return phase

    async def request(self, workflow_id, request_id, call_id=None):
        store = await self._store(self.store_names[0], call_id)
        key = self._key(workflow_id, "req.", hashlib.sha256(request_id.encode()).hexdigest()[:40])
        item = await store.get_item(key, call_id=call_id)
        return dict(item.value) if item else None

    async def create_request(self, workflow_id, request_id, value, call_id=None):
        store = await self._store(self.store_names[0], call_id)
        key = self._key(workflow_id, "req.", hashlib.sha256(request_id.encode()).hexdigest()[:40])
        try:
            await store.create_item(key, value, tags={"wf": workflow_id, "kind": "req"}, call_id=call_id)
        except Exception as exc:
            from azure.ai.agentserver.core.storage import FoundryStorageConflictError
            if isinstance(exc, FoundryStorageConflictError):
                raise WorkflowConflictError("request already exists") from exc
            raise

    async def append_event(self, workflow_id, phase_id, event, call_id=None):
        for _ in range(5):
            current = await self.get_meta(workflow_id, call_id)
            meta = current.meta.model_copy(deep=True)
            meta.event_seq += 1
            try:
                await self.save_meta(meta, current.etag, call_id)
                break
            except WorkflowConflictError:
                continue
        else:
            raise WorkflowConflictError("busy: could not allocate an event sequence")
        stored = event.model_copy(update={"seq": meta.event_seq, "persisted_at": utc_now()}, deep=True)
        events_store = await self._store(self.store_names[1], call_id)
        key = self._key(workflow_id, "ev.", f"{stored.seq:07d}")
        await events_store.create_item(key, self._json(stored), tags={"wf": workflow_id, "kind": "event", "phase": phase_id}, call_id=call_id)
        return stored

    async def save_checkpoint(self, workflow_id, phase_id, label, value, call_id=None):
        phase = await self.get_phase(workflow_id, phase_id, call_id)
        ref = self._key(workflow_id, "sess.", f"{phase.ordinal:03d}.{label}")
        store = await self._store(self.store_names[2], call_id)
        payload = {"schema_version": 1, "kind": "session", "value": value}
        if len(json.dumps(payload, separators=(",", ":")).encode()) > MAX_ITEM_BYTES:
            raise SizeLimitError("session checkpoint exceeds the state-store item limit")
        try:
            await store.create_item(ref, payload, tags={"wf": workflow_id, "kind": "session", "phase": f"{phase.ordinal:03d}"}, call_id=call_id)
        except Exception as exc:
            from azure.ai.agentserver.core.storage import FoundryStorageConflictError
            if not isinstance(exc, FoundryStorageConflictError):
                raise
            item = await store.get_item(ref, call_id=call_id)
            if item is None:
                raise
        return ref

    async def get_checkpoint(self, ref, call_id=None):
        store = await self._store(self.store_names[2], call_id)
        item = await store.get_item(ref, call_id=call_id)
        return dict(item.value.get("value", {})) if item else None

    async def get_tool_result(self, workflow_id, phase_id, key, call_id=None):
        phase = await self.get_phase(workflow_id, phase_id, call_id)
        item_key = self._key(workflow_id, "tool.", f"{phase.ordinal:03d}.{key}")
        store = await self._store(self.store_names[2], call_id)
        item = await store.get_item(item_key, call_id=call_id)
        return item.value.get("result") if item else None

    async def save_tool_result(self, workflow_id, phase_id, key, value, call_id=None):
        phase = await self.get_phase(workflow_id, phase_id, call_id)
        item_key = self._key(workflow_id, "tool.", f"{phase.ordinal:03d}.{key}")
        store = await self._store(self.store_names[2], call_id)
        payload = {"schema_version": 1, "kind": "tool", "result": value}
        if len(json.dumps(payload, default=str).encode()) > 64 * 1024:
            payload["result"] = str(value)[:8000]
            payload["truncated"] = True
        try:
            await store.create_item(item_key, payload, tags={"wf": workflow_id, "kind": "tool", "phase": f"{phase.ordinal:03d}"}, call_id=call_id)
        except Exception as exc:
            from azure.ai.agentserver.core.storage import FoundryStorageConflictError
            if not isinstance(exc, FoundryStorageConflictError):
                raise

    async def events(self, workflow_id, after_seq=0, phase_id=None, call_id=None):
        store = await self._store(self.store_names[1], call_id)
        meta = (await self.get_meta(workflow_id, call_id)).meta
        events = []
        for seq in range(after_seq + 1, meta.event_seq + 1):
            key = self._key(workflow_id, "ev.", f"{seq:07d}")
            item = await store.get_item(key, call_id=call_id)
            if item is None:
                continue
            event = WorkflowEvent.model_validate(item.value)
            if phase_id is None or event.phase_id == phase_id:
                events.append(event)
        return events

    async def aclose(self) -> None:
        for store in self._stores.values():
            if hasattr(store, "aclose"):
                await store.aclose()
        if self._credential is not None and hasattr(self._credential, "close"):
            await self._credential.close()
