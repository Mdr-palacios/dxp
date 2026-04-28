"""
Tests for chat.progress: the per-run event queue that powers the streaming
agent trace.

Covers the contract the SSE view depends on:
  - new_run_id() returns a unique opaque string
  - create() / get() / finish() round-trip and free memory
  - emit() is a no-op for unknown or missing run_ids (never raises)
  - drain() yields events in order, then stops at done/error
  - drain() emits ping heartbeats while idle
  - finish() drops the queue so a later emit() is a no-op

Usage:
    python scripts/_test_progress_queue.py
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

# Bootstrap Django so the chat package imports cleanly (chat.__init__ is fine
# without it, but be consistent with the other suites).
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "powerbuilder_app.settings")

import django  # noqa: E402

django.setup()

from chat import progress  # noqa: E402


def main() -> int:
    failures: list[str] = []

    # 1. new_run_id is unique and non-empty.
    a = progress.new_run_id()
    b = progress.new_run_id()
    if not a or not b or a == b:
        failures.append(f"new_run_id collision or empty: {a!r}, {b!r}")

    # 2. create() returns a queue and registers it.
    rid = progress.new_run_id()
    q = progress.create(rid)
    if q is None:
        failures.append("create() returned None")
    if rid not in progress.active_run_ids():
        failures.append("create() did not register run_id")

    # 3. emit() with no run_id is a no-op (no exception, no event).
    try:
        progress.emit(None, "agent_start", agent="x", label="x")
    except Exception as exc:
        failures.append(f"emit(None, ...) raised: {exc}")

    # 4. emit() to unknown run_id is a no-op.
    try:
        progress.emit("does-not-exist", "agent_start", agent="x")
    except Exception as exc:
        failures.append(f"emit(unknown, ...) raised: {exc}")

    # 5. emit() then drain() returns the events in order, terminated by 'done'.
    progress.emit(rid, "agent_start", agent="researcher", label="Searching")
    progress.emit(rid, "agent_done",  agent="researcher", label="Sources gathered")
    progress.emit(rid, "done",        label="ok")

    received: list[dict] = []
    for evt in progress.drain(rid, timeout=2.0, poll_interval=0.05):
        received.append(evt.to_dict())
        if len(received) > 10:
            break  # safety against infinite loop

    types = [e["type"] for e in received]
    if types != ["agent_start", "agent_done", "done"]:
        failures.append(f"drain() event order wrong: {types}")
    if received and received[0].get("agent") != "researcher":
        failures.append(f"event payload missing agent: {received[0]}")
    if received and received[0].get("label") != "Searching":
        failures.append(f"event payload missing label: {received[0]}")

    # 6. finish() drops the queue.
    progress.finish(rid)
    if rid in progress.active_run_ids():
        failures.append("finish() did not remove run_id from registry")

    # 7. emit() after finish() is a no-op (unknown run id).
    try:
        progress.emit(rid, "agent_start", agent="late")
    except Exception as exc:
        failures.append(f"emit() after finish() raised: {exc}")

    # 8. drain() emits a ping heartbeat when idle.
    rid2 = progress.new_run_id()
    progress.create(rid2)

    def _delayed_done():
        time.sleep(0.15)
        progress.emit(rid2, "done", label="ok")

    threading.Thread(target=_delayed_done, daemon=True).start()

    saw_ping = False
    saw_done = False
    for evt in progress.drain(rid2, timeout=2.0, poll_interval=0.05):
        if evt.type == "ping":
            saw_ping = True
        if evt.type == "done":
            saw_done = True
            break

    if not saw_ping:
        failures.append("drain() never yielded a ping heartbeat while idle")
    if not saw_done:
        failures.append("drain() did not deliver the late done event")

    progress.finish(rid2)

    # 9. ProgressEvent.to_dict() omits empty fields and includes payload.
    rid3 = progress.new_run_id()
    progress.create(rid3)
    progress.emit(rid3, "agent_done", agent="precincts", label="Targets ranked",
                  count=12)
    progress.emit(rid3, "done")
    saw_payload = False
    for evt in progress.drain(rid3, timeout=2.0, poll_interval=0.05):
        if evt.type == "agent_done":
            d = evt.to_dict()
            if d.get("count") == 12 and d.get("agent") == "precincts":
                saw_payload = True
        if evt.type == "done":
            break
    progress.finish(rid3)
    if not saw_payload:
        failures.append("to_dict() did not surface payload field 'count'")

    # 10. Concurrent emitters do not lose events.
    rid4 = progress.new_run_id()
    progress.create(rid4)
    N = 25
    def _emitter(i):
        progress.emit(rid4, "agent_start", agent=f"a{i}", label=f"l{i}")

    threads = [threading.Thread(target=_emitter, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    progress.emit(rid4, "done")

    n_starts = 0
    for evt in progress.drain(rid4, timeout=2.0, poll_interval=0.05):
        if evt.type == "agent_start":
            n_starts += 1
        if evt.type == "done":
            break
    progress.finish(rid4)
    if n_starts != N:
        failures.append(
            f"concurrent emit() lost events: expected {N} agent_start, got {n_starts}"
        )

    # Report.
    expected_assertions = 11  # 1, 2 (count as 2), 3, 4, 5 (3 sub-asserts), 6, 7, 8 (2), 9, 10
    print(f"chat.progress test: {expected_assertions} assertions across 10 cases.")
    if failures:
        print(f"FAIL: {len(failures)} assertion(s) failed:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS: all assertion groups OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
