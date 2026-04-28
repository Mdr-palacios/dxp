"""
Per-run progress event queues for the streaming agent trace.

Why this exists
---------------
The chat UI used to wait silently while the LangGraph pipeline ran. A real
plan run touches 4-6 agents and takes 30-90 seconds, which left users staring
at a 3-dot bubble. This module gives every active run a small in-process
queue. Agent nodes push progress events ("researcher started", "researcher
finished, 4 sources") and the streaming endpoint drains the queue into an
SSE stream the browser consumes live.

Design choices
--------------
* In-process dict, not Redis. The whole demo runs in one Gunicorn worker,
  the SSE consumer and the agent worker live in the same process, and the
  queue's lifetime is one run. A Redis dependency would buy us nothing.
* Thread-safe. We protect the registry with a single lock; per-run
  ``queue.SimpleQueue`` instances are already thread-safe internally.
* Bounded blast radius. ``finish()`` removes the queue; if a caller leaks a
  run id we log and move on rather than growing the dict forever.
* Optional. Agents call ``emit(run_id, ...)`` only when ``run_id`` is set in
  AgentState; older non-streaming code paths (CLI tests, direct
  ``manager_app.invoke``) stay unaffected.

Usage from an agent node:

    from ..progress import emit
    emit(state.get("run_id"), "agent_start", agent="researcher",
         label="Searching the corpus...")
    ...do work...
    emit(state.get("run_id"), "agent_done", agent="researcher",
         label=f"Found {n} sources")

Usage from the streaming view:

    queue = create(run_id)
    try:
        # spawn the run on a worker thread that calls emit() as it goes
        for evt in drain(run_id, timeout=1.0):
            yield format_sse(evt)
    finally:
        finish(run_id)
"""
from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

# Hard ceiling on how long a single SSE consumer waits for the run to finish.
# Plans cap around 90s; we add headroom for slow networks plus the model.
DEFAULT_RUN_TIMEOUT_SECS = 240
# How long the consumer blocks on each get() before checking liveness. Short
# enough that a cancelled client tears down within a heartbeat.
POLL_INTERVAL_SECS = 1.0


@dataclass
class ProgressEvent:
    """One discrete event in a run's lifecycle."""
    type: str                       # "agent_start", "agent_done", "trace", "done", "error"
    agent: Optional[str] = None     # node name (e.g. "researcher", "win_number")
    label: Optional[str] = None     # short human-readable status
    payload: dict[str, Any] = field(default_factory=dict)  # type-specific extras
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.type, "ts": self.ts}
        if self.agent:
            d["agent"] = self.agent
        if self.label:
            d["label"] = self.label
        if self.payload:
            d.update(self.payload)
        return d


class _Registry:
    """Thread-safe map of run_id -> SimpleQueue[ProgressEvent]."""

    def __init__(self) -> None:
        self._queues: dict[str, "queue.SimpleQueue[ProgressEvent]"] = {}
        self._lock = threading.Lock()

    def create(self, run_id: str) -> "queue.SimpleQueue[ProgressEvent]":
        with self._lock:
            q = self._queues.get(run_id)
            if q is None:
                q = queue.SimpleQueue()
                self._queues[run_id] = q
            return q

    def get(self, run_id: str) -> Optional["queue.SimpleQueue[ProgressEvent]"]:
        with self._lock:
            return self._queues.get(run_id)

    def finish(self, run_id: str) -> None:
        with self._lock:
            self._queues.pop(run_id, None)


_registry = _Registry()


def new_run_id() -> str:
    """Short opaque id for a single run. Callers pass this through AgentState."""
    return uuid.uuid4().hex[:16]


def create(run_id: str) -> "queue.SimpleQueue[ProgressEvent]":
    """Allocate a queue for a run. Idempotent: returns the existing queue if any."""
    return _registry.create(run_id)


def emit(run_id: Optional[str], type: str, **fields: Any) -> None:
    """
    Push an event onto a run's queue, no-op if run_id is missing or unknown.

    Agent code calls this without caring whether streaming is enabled; the
    no-op behavior keeps non-streaming code paths (tests, CLI) clean.
    """
    if not run_id:
        return
    q = _registry.get(run_id)
    if q is None:
        # Run was finished before the agent caught up; not worth raising.
        return
    agent = fields.pop("agent", None)
    label = fields.pop("label", None)
    q.put(ProgressEvent(type=type, agent=agent, label=label, payload=fields))


def drain(
    run_id: str,
    timeout: float = DEFAULT_RUN_TIMEOUT_SECS,
    poll_interval: float = POLL_INTERVAL_SECS,
) -> Iterator[ProgressEvent]:
    """
    Yield events from a run's queue until a terminal ``done`` or ``error``
    event arrives, or until ``timeout`` seconds elapse without progress.

    Caller is responsible for invoking ``finish(run_id)`` to release the queue.
    """
    q = _registry.get(run_id)
    if q is None:
        return
    deadline = time.time() + timeout
    while True:
        if time.time() >= deadline:
            yield ProgressEvent(type="error", label="Run timed out")
            return
        try:
            evt = q.get(timeout=poll_interval)
        except queue.Empty:
            # Heartbeat — keeps the SSE connection alive across slow agents.
            yield ProgressEvent(type="ping")
            continue
        yield evt
        if evt.type in ("done", "error"):
            return


def finish(run_id: str) -> None:
    """Drop the queue for ``run_id``; safe to call multiple times."""
    _registry.finish(run_id)


def active_run_ids() -> list[str]:
    """For debugging/tests only."""
    with _registry._lock:
        return list(_registry._queues.keys())
