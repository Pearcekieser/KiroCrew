"""Tests for the Job SDK — app-scoped durable runs.

Covers the runner registry, per-run JSON records, cooperative cancellation via
a threading.Event, the startup reconciliation pass, and the process-wide SDK
registry.

Runs execute on real daemon threads, so terminal state is awaited with a
bounded poll (``_wait_terminal``) rather than a fixed sleep — a fixed sleep is
what makes such a suite flaky on a loaded runner.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from kiro_crew.apps import job_sdk
from kiro_crew.apps.job_sdk import (
    _CLEANUP_JOIN_SECS,
    CANCELLED,
    DONE,
    FAILED,
    INTERRUPTED,
    PROGRESS_TAIL_MAX,
    RUNNING,
    TERMINAL_STATES,
    CleanupResult,
    JobError,
    JobHandle,
    JobRun,
    JobSDK,
    JobStore,
    UnknownJobKind,
    forget_sdk,
    get_sdk,
    reconcile_all,
    register_sdk,
    registered_apps,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _wait_terminal(sdk: JobSDK, run_id: str, timeout: float = 5.0) -> JobRun:
    """Poll until the run reaches a terminal status, or fail with what we saw."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = sdk.get(run_id)
        if last is not None and last.is_terminal:
            return last
        time.sleep(0.01)
    observed = last.status if last is not None else "<no record>"
    raise AssertionError(f"run {run_id} not terminal within {timeout}s; observed status={observed}")


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"condition not met within {timeout}s")


@pytest.fixture
def sdk(tmp_path: Path):
    """A fresh SDK over a tmp data dir, unregistered from the global registry
    on teardown so it never leaks into another test in the session.

    Teardown also SIGNALS and WAITS for any run still executing. Runs are real
    daemon threads: one still alive when pytest removes ``tmp_path`` would
    mkdir and write into the deleted tree at its next progress or terminal
    write, which is a real file mutation racing the fixture. Waiting (bounded,
    and loud on timeout) is the only correct answer -- cancelling the wrapper
    would leave the thread running, which is the defect, not the fix.
    """
    s = JobSDK("test-app", tmp_path)
    yield s
    with s._lock:  # noqa: SLF001 - teardown needs the live table the SDK owns
        live = list(s._live.values())
    for entry in live:
        entry.handle.discarded.set()
        entry.handle.cancelled.set()
    for entry in live:
        entry.thread.join(timeout=5.0)
        assert not entry.thread.is_alive(), (
            "a job worker outlived its test and would write into the removed "
            f"tmp_path: {entry.thread.name}"
        )
    forget_sdk(s.app_name)


# ---------------------------------------------------------------------------
# 1. register / kinds / is_cancellable
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_register_and_kinds(self, sdk: JobSDK) -> None:
        sdk.register("build", lambda h: {"ok": True})
        sdk.register("deploy", lambda h: {"ok": True}, cancellable=True)
        assert sdk.kinds() == ["build", "deploy"]  # sorted

    def test_is_cancellable(self, sdk: JobSDK) -> None:
        sdk.register("plain", lambda h: {})
        sdk.register("stoppable", lambda h: {}, cancellable=True)
        assert sdk.is_cancellable("plain") is False
        assert sdk.is_cancellable("stoppable") is True
        # Unknown kind is not cancellable.
        assert sdk.is_cancellable("nope") is False

    def test_register_empty_kind_raises(self, sdk: JobSDK) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            sdk.register("", lambda h: {})


# ---------------------------------------------------------------------------
# 2. start() on an unregistered kind
# ---------------------------------------------------------------------------


class TestStartUnknown:
    def test_start_unknown_kind_raises(self, sdk: JobSDK) -> None:
        with pytest.raises(UnknownJobKind, match="no registered runner"):
            sdk.start("ghost")


# ---------------------------------------------------------------------------
# 3. Happy run: done + dict result; non-dict result left empty
# ---------------------------------------------------------------------------


class TestHappyRun:
    def test_run_reaches_done_and_captures_dict_result(self, sdk: JobSDK) -> None:
        sdk.register("work", lambda h, **kw: {"answer": 42})
        run_id = sdk.start("work")
        run = _wait_terminal(sdk, run_id)
        assert run.status == DONE
        assert run.result == {"answer": 42}
        assert run.finished_at != ""

    def test_non_dict_result_leaves_result_empty(self, sdk: JobSDK) -> None:
        sdk.register("work", lambda h, **kw: "not a dict")
        run_id = sdk.start("work")
        run = _wait_terminal(sdk, run_id)
        assert run.status == DONE
        assert run.result == {}

    def test_params_are_passed_to_runner(self, sdk: JobSDK) -> None:
        seen: dict = {}

        def runner(h, **kw):
            seen.update(kw)
            return {"got": kw}

        sdk.register("work", runner)
        run_id = sdk.start("work", params={"x": 1, "y": "z"})
        run = _wait_terminal(sdk, run_id)
        assert run.status == DONE
        assert seen == {"x": 1, "y": "z"}


# ---------------------------------------------------------------------------
# 4. Raising runner -> failed with truncated, recorded error
# ---------------------------------------------------------------------------


class TestFailingRun:
    def test_raising_runner_reaches_failed_with_error(self, sdk: JobSDK) -> None:
        def boom(h, **kw):
            raise RuntimeError("kaboom happened")

        sdk.register("boom", boom)
        run_id = sdk.start("boom")
        run = _wait_terminal(sdk, run_id)
        assert run.status == FAILED
        assert "kaboom happened" in run.error

    def test_error_is_truncated_to_2000_chars(self, sdk: JobSDK) -> None:
        def boom(h, **kw):
            raise RuntimeError("x" * 5000)

        sdk.register("boom", boom)
        run_id = sdk.start("boom")
        run = _wait_terminal(sdk, run_id)
        assert run.status == FAILED
        assert len(run.error) == 2000


# ---------------------------------------------------------------------------
# 5. Cancellation
# ---------------------------------------------------------------------------


class TestCancellation:
    def test_polling_runner_reaches_cancelled(self, sdk: JobSDK) -> None:
        started = threading.Event()

        def runner(h, **kw):
            started.set()
            # Poll the cancel signal cooperatively.
            for _ in range(500):
                if h.cancelled.is_set():
                    return
                time.sleep(0.01)

        sdk.register("loop", runner, cancellable=True)
        run_id = sdk.start("loop")
        assert started.wait(5.0)
        assert sdk.cancel(run_id) is True
        run = _wait_terminal(sdk, run_id)
        assert run.status == CANCELLED

    def test_cancel_unknown_run_id_returns_false(self, sdk: JobSDK) -> None:
        assert sdk.cancel("deadbeef") is False

    def test_cancel_non_cancellable_run_returns_false(self, sdk: JobSDK) -> None:
        release = threading.Event()
        started = threading.Event()

        def runner(h, **kw):
            started.set()
            release.wait(5.0)
            return {}

        # Registered cancellable=False (the default).
        sdk.register("plain", runner)
        run_id = sdk.start("plain")
        assert started.wait(5.0)
        # Live but not declared cancellable -> False.
        assert sdk.cancel(run_id) is False
        release.set()
        run = _wait_terminal(sdk, run_id)
        assert run.status == DONE

    def test_cancel_already_terminal_run_returns_false(self, sdk: JobSDK) -> None:
        sdk.register("quick", lambda h, **kw: {"done": True}, cancellable=True)
        run_id = sdk.start("quick")
        _wait_terminal(sdk, run_id)
        # Popped from _live once terminal, so cancel returns False.
        assert sdk.cancel(run_id) is False


# ---------------------------------------------------------------------------
# 6. dedupe_key
# ---------------------------------------------------------------------------


class TestDedupe:
    def test_second_start_with_same_key_adopts_run(self, sdk: JobSDK) -> None:
        release = threading.Event()
        started = threading.Event()
        calls = []

        def runner(h, **kw):
            calls.append(1)
            started.set()
            release.wait(5.0)
            return {}

        sdk.register("work", runner)
        first = sdk.start("work", dedupe_key="k1")
        assert started.wait(5.0)
        second = sdk.start("work", dedupe_key="k1")
        assert second == first  # adopted, not a new run
        release.set()
        _wait_terminal(sdk, first)
        assert calls == [1]  # body ran exactly once

    def test_two_starts_without_key_produce_two_ids(self, sdk: JobSDK) -> None:
        sdk.register("work", lambda h, **kw: {})
        a = sdk.start("work")
        b = sdk.start("work")
        assert a != b
        _wait_terminal(sdk, a)
        _wait_terminal(sdk, b)


# ---------------------------------------------------------------------------
# 7. JobHandle.progress and bounded line tail
# ---------------------------------------------------------------------------


def _handle_over(tmp_path: Path, run: JobRun) -> tuple[JobHandle, JobStore]:
    """A handle wired to a REAL SDK's guarded writer, not a stand-in.

    ``JobHandle`` takes the writer as a callable so the discard check and the
    write share one lock acquisition. A test that passed its own copy of that
    guard would only be testing the copy, so this hands over the real one.
    """
    sdk = JobSDK("handle-test", tmp_path)
    return JobHandle(run, sdk._persist), sdk.store  # noqa: SLF001 - the point of the helper


class TestProgress:
    def test_progress_records_pct_step_line(self, tmp_path: Path) -> None:
        run = JobRun(run_id="a" * 8, app="x", kind="k")
        handle, store = _handle_over(tmp_path, run)
        store.write(run)
        handle.progress(pct=25.0, step="phase-1", line="hello")
        reread = store.read(run.run_id)
        assert reread.progress_pct == 25.0
        assert reread.step == "phase-1"
        assert reread.lines == ["hello"]

    def test_line_tail_is_bounded_and_drops_oldest(self, tmp_path: Path) -> None:
        run = JobRun(run_id="b" * 8, app="x", kind="k")
        handle, store = _handle_over(tmp_path, run)
        store.write(run)
        total = PROGRESS_TAIL_MAX + 20
        for i in range(total):
            handle.progress(line=f"line-{i}")
        reread = store.read(run.run_id)
        assert len(reread.lines) == PROGRESS_TAIL_MAX
        # Oldest dropped: the tail holds the last PROGRESS_TAIL_MAX lines.
        assert reread.lines[0] == f"line-{total - PROGRESS_TAIL_MAX}"
        assert reread.lines[-1] == f"line-{total - 1}"


# ---------------------------------------------------------------------------
# 8. get / list_active / list_recent
# ---------------------------------------------------------------------------


class TestReadViews:
    def test_get_missing_returns_none(self, sdk: JobSDK) -> None:
        assert sdk.get("c" * 8) is None

    def test_list_active_excludes_terminal_and_filters_kind(self, sdk: JobSDK) -> None:
        release = threading.Event()

        def blocker(h, **kw):
            release.wait(5.0)
            return {}

        sdk.register("live", blocker)
        sdk.register("fast", lambda h, **kw: {})

        live_id = sdk.start("live")
        fast_id = sdk.start("fast")
        _wait_terminal(sdk, fast_id)

        active = sdk.list_active()
        active_ids = {r.run_id for r in active}
        assert live_id in active_ids
        assert fast_id not in active_ids  # terminal excluded

        # kind filter
        assert [r.run_id for r in sdk.list_active(kind="live")] == [live_id]
        assert sdk.list_active(kind="fast") == []

        release.set()
        _wait_terminal(sdk, live_id)

    def test_list_recent_limit_and_ordering(self, sdk: JobSDK) -> None:
        # Write records directly with controlled, distinct updated_at values.
        # (_now() is second-granularity, so real runs in the same second cannot
        # be ordered by wall clock — the ordering CONTRACT is what we pin here.)
        store = sdk.store
        specs = [
            ("a" * 8, "2026-01-01T00:00:01Z"),
            ("b" * 8, "2026-01-01T00:00:03Z"),  # newest
            ("c" * 8, "2026-01-01T00:00:02Z"),
        ]
        store.dir.mkdir(parents=True, exist_ok=True)
        for rid, ts in specs:
            run = JobRun(run_id=rid, app="test-app", kind="work", status=DONE)
            run.updated_at = ts
            run.created_at = ts
            (store.dir / f"{rid}.json").write_text(json.dumps(run.to_dict(), indent=1))

        recent = sdk.list_recent(limit=2)
        assert len(recent) == 2
        # Most-recently-updated first.
        assert [r.run_id for r in recent] == ["b" * 8, "c" * 8]

    def test_list_recent_limit_zero_returns_empty(self, sdk: JobSDK) -> None:
        sdk.register("work", lambda h, **kw: {})
        rid = sdk.start("work")
        _wait_terminal(sdk, rid)
        assert sdk.list_recent(limit=0) == []

    def test_list_recent_kind_filter(self, sdk: JobSDK) -> None:
        sdk.register("a", lambda h, **kw: {})
        sdk.register("b", lambda h, **kw: {})
        a_id = sdk.start("a")
        b_id = sdk.start("b")
        _wait_terminal(sdk, a_id)
        _wait_terminal(sdk, b_id)
        recent_a = sdk.list_recent(kind="a")
        assert [r.run_id for r in recent_a] == [a_id]


# ---------------------------------------------------------------------------
# 9. JobRun.from_dict / JobStore.read / _path / iter_runs
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_from_dict_drops_unknown_keys(self) -> None:
        run = JobRun.from_dict(
            {"run_id": "x1", "app": "a", "kind": "k", "bogus": "nope", "status": DONE}
        )
        assert run.run_id == "x1"
        assert run.status == DONE
        assert not hasattr(run, "bogus")

    def test_from_dict_tolerates_missing_required(self) -> None:
        run = JobRun.from_dict({})
        assert run.run_id == ""
        assert run.app == ""
        assert run.kind == ""

    def test_read_missing_file_returns_none(self, tmp_path: Path) -> None:
        store = JobStore(tmp_path)
        assert store.read("d" * 8) is None

    def test_read_corrupt_file_returns_none(self, tmp_path: Path) -> None:
        store = JobStore(tmp_path)
        store.dir.mkdir(parents=True, exist_ok=True)
        (store.dir / "abcdef.json").write_text("{ not json")
        assert store.read("abcdef") is None

    def test_read_invalid_run_id_returns_none(self, tmp_path: Path) -> None:
        store = JobStore(tmp_path)
        # ValueError from _path is swallowed into None by read.
        assert store.read("NOT-HEX!") is None

    def test_path_rejects_non_hex_id(self, tmp_path: Path) -> None:
        store = JobStore(tmp_path)
        with pytest.raises(ValueError, match="invalid run id"):
            store._path("zzz")
        with pytest.raises(ValueError, match="invalid run id"):
            store._path("")

    def test_iter_runs_skips_unreadable_file(self, tmp_path: Path) -> None:
        store = JobStore(tmp_path)
        store.dir.mkdir(parents=True, exist_ok=True)
        good = JobRun(run_id="a" * 8, app="x", kind="k", status=DONE)
        store.write(good)
        (store.dir / "corrupt.json").write_text("{ broken")
        runs = list(store.iter_runs())
        assert [r.run_id for r in runs] == ["a" * 8]

    def test_iter_runs_empty_when_dir_absent(self, tmp_path: Path) -> None:
        store = JobStore(tmp_path / "does-not-exist")
        assert list(store.iter_runs()) == []


# ---------------------------------------------------------------------------
# 10. reconcile()
# ---------------------------------------------------------------------------


class TestReconcile:
    def _write_raw(self, store: JobStore, run: JobRun) -> None:
        store.dir.mkdir(parents=True, exist_ok=True)
        path = store.dir / f"{run.run_id}.json"
        path.write_text(json.dumps(run.to_dict(), indent=1))

    def test_foreign_origin_nonterminal_flips_to_interrupted_known_runner(
        self, sdk: JobSDK
    ) -> None:
        sdk.register("work", lambda h, **kw: {})
        run = JobRun(
            run_id="a" * 8,
            app="test-app",
            kind="work",
            status=RUNNING,
            origin="foreign-origin-token",
        )
        self._write_raw(sdk.store, run)
        flipped = sdk.reconcile()
        assert flipped == 1
        reread = sdk.get(run.run_id)
        assert reread.status == INTERRUPTED
        assert "gateway restarted while this was running" in reread.error

    def test_foreign_origin_unknown_runner_error_names_kind(self, sdk: JobSDK) -> None:
        run = JobRun(
            run_id="b" * 8,
            app="test-app",
            kind="gone-kind",
            status=RUNNING,
            origin="foreign-origin-token",
        )
        self._write_raw(sdk.store, run)
        flipped = sdk.reconcile()
        assert flipped == 1
        reread = sdk.get(run.run_id)
        assert reread.status == INTERRUPTED
        assert "no runner is registered" in reread.error
        assert "gone-kind" in reread.error

    def test_terminal_record_left_alone(self, sdk: JobSDK) -> None:
        run = JobRun(
            run_id="c" * 8,
            app="test-app",
            kind="work",
            status=DONE,
            origin="foreign-origin-token",
        )
        self._write_raw(sdk.store, run)
        assert sdk.reconcile() == 0
        assert sdk.get(run.run_id).status == DONE

    def test_own_origin_record_is_resolved_when_nothing_is_executing_it(self, sdk: JobSDK) -> None:
        """Own origin alone is NOT a reason to spare a record.

        A record this process wrote and then lost -- a terminal write that failed
        twice -- carries this origin while nothing is running it, and sparing it
        would leave exactly the stuck-`running` state the pass exists to clear.
        The live table, not the origin, is what says a run is still executing.
        """
        run = JobRun(
            run_id="d" * 8,
            app="test-app",
            kind="work",
            status=RUNNING,
            origin=job_sdk._ORIGIN,
        )
        self._write_raw(sdk.store, run)
        assert sdk.reconcile() == 1
        assert sdk.get(run.run_id).status == INTERRUPTED

    def test_a_run_this_process_is_actually_executing_is_left_alone(self, sdk: JobSDK) -> None:
        """The other half: a genuinely live run must survive a reconcile.

        Reconciliation runs after the enable loop, and an app's on_startup may
        already have started a run, so this is a real ordering, not a hypothetical.
        """
        release = threading.Event()
        started = threading.Event()

        def runner(h, **kw):
            started.set()
            release.wait(5.0)
            return {}

        sdk.register("work", runner)
        run_id = sdk.start("work")
        assert started.wait(5.0)

        assert sdk.reconcile() == 0
        assert sdk.get(run_id).status == RUNNING

        release.set()
        _wait_terminal(sdk, run_id)


# ---------------------------------------------------------------------------
# 11. remove_all_async
# ---------------------------------------------------------------------------


class TestRemoveAll:
    def test_signals_live_and_deletes_records_idempotent(self, sdk: JobSDK) -> None:
        release = threading.Event()
        started = threading.Event()
        cancelled_seen = threading.Event()

        def runner(h, **kw):
            started.set()
            for _ in range(500):
                if h.cancelled.is_set():
                    cancelled_seen.set()
                    return
                time.sleep(0.01)
            release.wait(5.0)
            return {}

        sdk.register("live", runner, cancellable=True)
        run_id = sdk.start("live")
        assert started.wait(5.0)

        cleanup = asyncio.run(sdk.remove_all_async())
        assert cleanup == CleanupResult(1, 0, 0)
        # The live run was signalled to stop.
        assert cancelled_seen.wait(5.0)
        release.set()
        _wait_until(lambda: not any(t.name.startswith("job:") for t in threading.enumerate()))
        # The signalled worker cannot write its record back, so the deletion
        # holds and a second cleanup is a no-op.
        assert sdk.get(run_id) is None
        assert asyncio.run(sdk.remove_all_async()) == CleanupResult(0, 0, 0)

    def test_remove_all_deletes_records_of_finished_runs(self, sdk: JobSDK) -> None:
        """With no live worker to race, remove_all_async deletes the record and
        the second call is a no-op."""
        sdk.register("work", lambda h, **kw: {"ok": True})
        run_id = sdk.start("work")
        _wait_terminal(sdk, run_id)
        # No live worker remains, so nothing can rewrite the file.
        assert asyncio.run(sdk.remove_all_async()) == CleanupResult(1, 0, 0)
        assert sdk.get(run_id) is None
        assert asyncio.run(sdk.remove_all_async()) == CleanupResult(0, 0, 0)

    def test_remove_all_is_not_resurrected_by_worker(self, sdk: JobSDK) -> None:
        """A worker returning AFTER cleanup must not write its record back.

        Regression pin. JobStore.write mkdirs and writes unconditionally, so it
        cannot tell a first write from a resurrection; the guarantee comes from
        remove_all_async marking every live handle discarded BEFORE deleting,
        and both write paths honouring that mark. Without it this run reappears
        on disk as `cancelled` and the second remove_all_async returns 1.
        """
        started = threading.Event()
        removed_done = threading.Event()
        finally_done = threading.Event()

        def runner(h, **kw):
            started.set()
            # Do not exit (and thus do not reach the finally-block write) until
            # remove_all has finished deleting the record — this makes the
            # ordering deterministic rather than timing-dependent.
            removed_done.wait(5.0)
            try:
                return
            finally:
                finally_done.set()

        sdk.register("live", runner, cancellable=True)
        run_id = sdk.start("live")
        assert started.wait(5.0)

        cleanup = asyncio.run(sdk.remove_all_async())
        # Only `removed` is asserted here. Whether this worker is still alive
        # when cleanup returns is a race between its own wait and the join
        # deadline, so pinning it would make this test flaky for a reason that
        # has nothing to do with resurrection. TestCleanupDoesNotBlockTheLoop
        # covers the still-running report deterministically, with a runner that
        # ignores its cancel signal outright.
        assert cleanup.removed == 1
        assert sdk.get(run_id) is None  # deleted at this instant
        removed_done.set()
        # Let the worker's finally-block write complete.
        assert finally_done.wait(5.0)
        _wait_until(lambda: not any(t.name.startswith("job:") for t in threading.enumerate()))
        time.sleep(0.05)
        # The record stays deleted, and idempotency still holds -- a resurrected
        # record would make this second call report 1.
        assert sdk.get(run_id) is None
        assert asyncio.run(sdk.remove_all_async()) == CleanupResult(0, 0, 0)


# ---------------------------------------------------------------------------
# 12. Process-wide registry
# ---------------------------------------------------------------------------


class TestProcessRegistry:
    def test_register_get_forget_registered_apps(self, tmp_path: Path) -> None:
        a = JobSDK("app-a", tmp_path / "a")
        b = JobSDK("app-b", tmp_path / "b")
        try:
            register_sdk(a)
            register_sdk(b)
            assert get_sdk("app-a") is a
            assert get_sdk("app-b") is b
            assert "app-a" in registered_apps()
            assert "app-b" in registered_apps()
            forget_sdk("app-a")
            assert get_sdk("app-a") is None
            assert "app-a" not in registered_apps()
        finally:
            forget_sdk("app-a")
            forget_sdk("app-b")

    def test_get_sdk_unknown_returns_none(self) -> None:
        assert get_sdk("never-registered-app") is None

    def test_reconcile_all_sums_across_sdks_and_survives_raise(self, tmp_path: Path) -> None:
        good = JobSDK("good-app", tmp_path / "good")
        bad = JobSDK("bad-app", tmp_path / "bad")

        # good has one foreign non-terminal record to flip.
        good.store.dir.mkdir(parents=True, exist_ok=True)
        run = JobRun(
            run_id="e" * 8,
            app="good-app",
            kind="work",
            status=RUNNING,
            origin="foreign-origin-token",
        )
        (good.store.dir / f"{run.run_id}.json").write_text(json.dumps(run.to_dict(), indent=1))

        # bad raises from reconcile — reconcile_all must survive it.
        def boom() -> int:
            raise RuntimeError("store is broken")

        bad.reconcile = boom  # type: ignore[method-assign]

        try:
            register_sdk(good)
            register_sdk(bad)
            total = reconcile_all()
            assert total == 1  # good's one flip; bad's raise swallowed
        finally:
            forget_sdk("good-app")
            forget_sdk("bad-app")


# ---------------------------------------------------------------------------
# 13. start_async / cancel_async match sync twins
# ---------------------------------------------------------------------------


class TestAsyncTwins:
    def test_start_async_runs_and_reaches_done(self, sdk: JobSDK) -> None:
        sdk.register("work", lambda h, **kw: {"async": True})
        run_id = asyncio.run(sdk.start_async("work", params={"p": 1}))
        run = _wait_terminal(sdk, run_id)
        assert run.status == DONE
        assert run.result == {"async": True}

    def test_cancel_async_matches_sync(self, sdk: JobSDK) -> None:
        started = threading.Event()

        def runner(h, **kw):
            started.set()
            for _ in range(500):
                if h.cancelled.is_set():
                    return
                time.sleep(0.01)

        sdk.register("loop", runner, cancellable=True)
        run_id = sdk.start("loop")
        assert started.wait(5.0)
        assert asyncio.run(sdk.cancel_async(run_id)) is True
        run = _wait_terminal(sdk, run_id)
        assert run.status == CANCELLED

    def test_cancel_async_unknown_returns_false(self, sdk: JobSDK) -> None:
        assert asyncio.run(sdk.cancel_async("f" * 8)) is False


# ---------------------------------------------------------------------------
# Misc invariants
# ---------------------------------------------------------------------------


class TestMisc:
    def test_terminal_states_membership(self) -> None:
        assert DONE in TERMINAL_STATES
        assert FAILED in TERMINAL_STATES
        assert CANCELLED in TERMINAL_STATES
        assert INTERRUPTED in TERMINAL_STATES
        assert RUNNING not in TERMINAL_STATES

    def test_is_terminal_property(self) -> None:
        assert JobRun(run_id="x", app="a", kind="k", status=DONE).is_terminal is True
        assert JobRun(run_id="x", app="a", kind="k", status=RUNNING).is_terminal is False


# ---------------------------------------------------------------------------
# Defensive branches: redaction fallback, audit swallow, OSError paths
# ---------------------------------------------------------------------------


class TestDefensiveBranches:
    def test_redact_falls_back_to_raw_when_security_raises(self, sdk: JobSDK, monkeypatch) -> None:
        """If the redaction chain itself raises, the raw error text survives
        (redaction must never mask the error)."""
        import kiro_crew.security as security

        def boom(_text):
            raise RuntimeError("redaction backend down")

        monkeypatch.setattr(security, "redact_credentials", boom)

        def failer(h, **kw):
            raise ValueError("SENTINEL-ERR-TEXT")

        sdk.register("boom", failer)
        run_id = sdk.start("boom")
        run = _wait_terminal(sdk, run_id)
        assert run.status == FAILED
        assert "SENTINEL-ERR-TEXT" in run.error

    def test_audit_failure_is_swallowed(self, sdk: JobSDK, monkeypatch) -> None:
        """A SEL audit failure must not fail the job."""
        import kiro_crew.apps.job_sdk as m

        def boom():
            raise RuntimeError("sel down")

        monkeypatch.setattr(m, "sel", boom)
        sdk.register("work", lambda h, **kw: {"ok": True})
        run_id = sdk.start("work")  # _audit("job_start") swallows the raise
        run = _wait_terminal(sdk, run_id)
        assert run.status == DONE

    def test_reconcile_survives_write_oserror(self, sdk: JobSDK, monkeypatch) -> None:
        """A write failure during reconcile is logged and skipped, not raised."""
        run = JobRun(
            run_id="a" * 8,
            app="test-app",
            kind="work",
            status=RUNNING,
            origin="foreign-origin-token",
        )
        sdk.store.dir.mkdir(parents=True, exist_ok=True)
        (sdk.store.dir / f"{run.run_id}.json").write_text(json.dumps(run.to_dict(), indent=1))

        def boom(_run):
            raise OSError("disk full")

        monkeypatch.setattr(sdk.store, "write", boom)
        # Does not raise; the un-writable record is not counted as flipped.
        assert sdk.reconcile() == 0

    def test_remove_all_survives_unlink_oserror(self, tmp_path: Path, monkeypatch) -> None:
        store = JobStore(tmp_path)
        run = JobRun(run_id="b" * 8, app="x", kind="k", status=DONE)
        store.write(run)

        real_unlink = Path.unlink

        def boom(self, *a, **k):
            raise OSError("locked")

        monkeypatch.setattr(Path, "unlink", boom)
        # Counted as a FAILURE, not silently dropped: reporting only successes
        # let a partial delete read as a clean one, so disable would claim the
        # app's runs were gone while records remained.
        assert store.remove_all() == (0, 1)
        monkeypatch.setattr(Path, "unlink", real_unlink)

    def test_remove_all_empty_dir_returns_zero(self, tmp_path: Path) -> None:
        store = JobStore(tmp_path / "absent")
        assert store.remove_all() == (0, 0)


# ---------------------------------------------------------------------------
# The discard guard — the other half of the resurrection fix
# ---------------------------------------------------------------------------


class TestDiscardGuard:
    """``JobHandle`` has TWO write paths and cleanup must silence both.

    ``TestRemoveAll`` pins the worker's terminal write. This pins ``progress``:
    a runner that reports progress after its record was dropped would recreate
    the file just as surely, because ``JobStore.write`` mkdirs and writes
    unconditionally.
    """

    def test_progress_on_a_discarded_handle_writes_nothing(self, tmp_path: Path) -> None:
        run = JobRun(run_id="ab" * 8, app="demo", kind="work", status=RUNNING)
        handle, store = _handle_over(tmp_path, run)

        handle.progress(pct=10.0, step="first", line="one")
        assert store.read(run.run_id) is not None

        assert store.remove_all() == (1, 0)
        handle.discarded.set()

        # The runner keeps reporting; none of it may land. The guarded writer
        # refuses it under the same lock the discard was set with, so there is
        # no check-then-act window for cleanup to slip through.
        handle.progress(pct=99.0, step="after", line="two")
        assert store.read(run.run_id) is None

    def test_handle_exposes_its_run_id(self, tmp_path: Path) -> None:
        run = JobRun(run_id="cd" * 8, app="demo", kind="work")
        handle, _ = _handle_over(tmp_path, run)
        assert handle.run_id == run.run_id


class TestReconcilePoisonRecord:
    """One unusable record must cost only itself.

    Found by a pod end-to-end pass: a hand-written record whose run_id was not
    hex made ``JobStore._path`` raise ``ValueError``, which escaped ``reconcile``
    (it caught only ``OSError``) and abandoned every remaining run of that app.
    The symptom was the exact one the pass exists to clear -- runs stuck at
    ``running`` forever -- and it was invisible except as a logged traceback.
    """

    def test_a_record_with_an_unwritable_id_does_not_abandon_the_rest(
        self, sdk: JobSDK, tmp_path: Path
    ) -> None:
        jobs_dir = tmp_path / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)

        # Sorted first, so it is reached BEFORE the healthy record: if it aborts
        # the loop, the sibling below is never reconciled and the test fails.
        (jobs_dir / "0-bad-id.json").write_text(
            json.dumps(
                {
                    "run_id": "not-hex-at-all",
                    "app": "demo",
                    "kind": "work",
                    "status": RUNNING,
                    "origin": "f" * 32,
                }
            )
        )
        healthy_id = "ab" * 16
        (jobs_dir / f"{healthy_id}.json").write_text(
            json.dumps(
                {
                    "run_id": healthy_id,
                    "app": "demo",
                    "kind": "work",
                    "status": RUNNING,
                    "origin": "f" * 32,
                }
            )
        )

        # The poison record is skipped; the healthy one is still resolved.
        assert sdk.reconcile() == 1
        assert sdk.get(healthy_id).status == INTERRUPTED


class TestConcurrentDedupe:
    """Two simultaneous starts with one key must produce ONE run.

    The double-click / two-tabs case is the whole point of `dedupe_key`, and the
    first version checked the key and claimed it in two separate critical
    sections with a disk read in between, so both callers could see no owner and
    both run -- paying the cost twice, which is exactly the hazard dedupe exists
    to remove.
    """

    def test_two_racing_starts_yield_one_run(self, sdk: JobSDK) -> None:
        bodies = threading.Semaphore(0)
        entered: list[str] = []

        def runner(h, **kw):
            entered.append(h.run_id)
            bodies.acquire(timeout=5.0)
            return {}

        sdk.register("work", runner, cancellable=False)

        gate = threading.Barrier(3, timeout=5.0)
        ids: list[str] = []
        errors: list[BaseException] = []

        def racer() -> None:
            try:
                gate.wait()
                ids.append(sdk.start("work", dedupe_key="one"))
            except BaseException as exc:  # noqa: BLE001 - surfaced by the assert below
                errors.append(exc)

        threads = [threading.Thread(target=racer, name=f"racer-{i}") for i in range(2)]
        for t in threads:
            t.start()
        gate.wait()
        for t in threads:
            t.join(timeout=5.0)

        assert not errors, errors
        assert len(ids) == 2
        assert ids[0] == ids[1], f"dedupe let both starts win: {ids}"
        # And only one body ever ran.
        assert len(entered) == 1, entered

        bodies.release()
        _wait_terminal(sdk, ids[0])


class TestRunnerOutputIsRedacted:
    """Runner-produced text is scrubbed at INGEST, so the record on disk is
    clean too -- not only the HTTP response. A runner that shells out can quote
    back a command line carrying a credential."""

    def test_progress_lines_and_result_strings_are_scrubbed(self, sdk: JobSDK) -> None:
        secret = "AKIAIOSFODNN7EXAMPLE"

        def runner(h, **kw):
            h.progress(line=f"using aws_secret_access_key={secret}")
            return {"echo": f"token={secret}", "count": 1}

        sdk.register("leaky", runner)
        run_id = sdk.start("leaky")
        run = _wait_terminal(sdk, run_id)

        on_disk = (sdk.store.dir / f"{run_id}.json").read_text()
        assert secret not in on_disk, "the credential reached the record on disk"
        assert run.lines and secret not in run.lines[0]
        assert secret not in str(run.result["echo"])
        # Non-string leaves are untouched.
        assert run.result["count"] == 1


class TestClaimAndWriteFailurePaths:
    """The two failure paths the atomicity and persistence fixes introduced."""

    def test_a_failed_initial_write_releases_the_dedupe_claim(
        self, sdk: JobSDK, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Otherwise the key stays owned by a run that never started, and every
        later start with that key would adopt a run that does not exist."""
        sdk.register("work", lambda h, **kw: {})

        def boom(_run):
            raise OSError("disk full")

        monkeypatch.setattr(sdk.store, "write", boom)
        # The guarded writer turns any write failure into a False return, so
        # start refuses with a JobError rather than leaking the raw OSError --
        # and, crucially, without skipping the claim release below.
        with pytest.raises(JobError):
            sdk.start("work", dedupe_key="k")

        # Claim released: with a working store the same key starts a real run.
        monkeypatch.undo()
        run_id = sdk.start("work", dedupe_key="k")
        assert run_id
        assert _wait_terminal(sdk, run_id).status == DONE

    def test_a_transient_terminal_write_is_retried(
        self, sdk: JobSDK, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A lost terminal write leaves the record reading `running` while the
        work is finished, so one retry covers the transient case."""
        real_write = sdk.store.write
        calls: list[int] = []

        def flaky(run):
            calls.append(1)
            # Fail only the terminal write (the initial one is call 1).
            if len(calls) == 2:
                raise OSError("transient")
            return real_write(run)

        sdk.register("work", lambda h, **kw: {"ok": True})
        monkeypatch.setattr(sdk.store, "write", flaky)
        run_id = sdk.start("work")
        run = _wait_terminal(sdk, run_id)
        assert run.status == DONE
        assert len(calls) >= 3, f"the retry did not happen: {len(calls)} write(s)"


class TestRecordIsAlwaysWritable:
    """A runner cannot make its own record unserializable.

    `json.dumps` raises TypeError on a set, a Path, or any object, and that
    exception used to escape the terminal write and skip the live-table and
    dedupe-key cleanup that follows it -- leaking a claim no later start could
    release. Sanitizing at ingest makes the failure impossible instead of
    handling it at each writer.
    """

    def test_non_serializable_result_still_persists_and_frees_the_claim(self, sdk: JobSDK) -> None:
        sdk.register("weird", lambda h, **kw: {"path": Path("/tmp/x"), "s": {1, 2}, "n": 3})
        run_id = sdk.start("weird", dedupe_key="k")
        run = _wait_terminal(sdk, run_id)

        assert run.status == DONE
        # Coerced, not dropped, and the numeric leaf is untouched.
        assert "/tmp/x" in str(run.result["path"])
        assert run.result["n"] == 3
        # Bookkeeping was NOT skipped: the key is free, so a new start with the
        # same key begins a new run rather than adopting a finished one.
        second = sdk.start("weird", dedupe_key="k")
        assert second != run_id
        _wait_terminal(sdk, second)

    def test_non_serializable_params_do_not_break_start(self, sdk: JobSDK) -> None:
        sdk.register("p", lambda h, **kw: {})
        run_id = sdk.start("p", params={"where": Path("/tmp/y")})
        assert _wait_terminal(sdk, run_id).status == DONE

    def test_step_and_nested_result_strings_are_redacted(self, sdk: JobSDK) -> None:
        secret = "AKIAIOSFODNN7EXAMPLE"

        def runner(h, **kw):
            h.progress(step=f"uploading with token={secret}")
            return {"outer": {"inner": [f"key={secret}"]}}

        sdk.register("nested", runner)
        run_id = sdk.start("nested")
        run = _wait_terminal(sdk, run_id)

        on_disk = (sdk.store.dir / f"{run_id}.json").read_text()
        assert secret not in on_disk
        assert secret not in run.step
        assert secret not in json.dumps(run.result)


class TestDisableStopsTheWork:
    """Disable must STOP the runs, not merely forget them.

    Signalling alone left the threads running: a disabled app kept doing real,
    side-effecting work with its records already deleted. Cleanup now
    bounded-joins each worker.
    """

    def test_remove_all_waits_for_a_cooperating_worker(self, sdk: JobSDK) -> None:
        started = threading.Event()
        finished = threading.Event()

        def runner(h, **kw):
            started.set()
            while not h.cancelled.is_set():
                time.sleep(0.01)
            finished.set()
            return {}

        sdk.register("slow", runner, cancellable=True)
        sdk.start("slow")
        assert started.wait(5.0)

        cleanup = asyncio.run(sdk.remove_all_async())
        assert cleanup == CleanupResult(1, 0, 0)
        # The worker is already done by the time cleanup returns -- that is the
        # difference between stopping the work and forgetting it.
        assert finished.is_set()
        assert not any(t.name.startswith("job:") for t in threading.enumerate())


class TestSanitizeInvariant:
    """Nothing a runner supplied reaches disk unsanitized -- INCLUDING keys.

    Three review rounds each found a different channel the scrub did not cover:
    top-level result values, then `step` and nested values, then dict KEYS. The
    answer is a total funnel plus a backstop in the single writer, so a missed
    ingest site cannot open a new channel.
    """

    def test_result_dict_keys_are_redacted(self, sdk: JobSDK) -> None:
        secret = "AKIAIOSFODNN7EXAMPLE"
        sdk.register("keyleak", lambda h, **kw: {f"token={secret}": "v"})
        run_id = sdk.start("keyleak")
        run = _wait_terminal(sdk, run_id)
        on_disk = (sdk.store.dir / f"{run_id}.json").read_text()
        assert secret not in on_disk
        assert not any(secret in k for k in run.result)

    def test_the_writer_scrubs_even_a_field_set_behind_its_back(
        self, sdk: JobSDK, tmp_path: Path
    ) -> None:
        """The backstop. A field assigned directly, skipping every ingest site,
        is still scrubbed because the single writer re-scrubs before writing."""
        secret = "AKIAIOSFODNN7EXAMPLE"
        run = JobRun(run_id="ef" * 8, app="test-app", kind="work", status=RUNNING)
        run.step = f"step token={secret}"
        run.result = {"nested": {"deep": [f"k={secret}"]}}
        assert sdk._persist(run) is True  # noqa: SLF001 - the invariant under test
        on_disk = (sdk.store.dir / f"{run.run_id}.json").read_text()
        assert secret not in on_disk


class TestCleanupDoesNotBlockTheLoop:
    """An async SDK method must never park the event loop.

    `remove_all_async` bounded-joins workers, and doing that inline made every
    disable stall the whole gateway for the deadline -- the exact hazard
    CronSDK's docstring spells out. The join runs on a worker thread now.
    """

    def test_the_loop_keeps_running_while_cleanup_waits(self, sdk: JobSDK) -> None:
        started = threading.Event()
        stop = threading.Event()

        def runner(h, **kw):
            started.set()
            # Ignores the cancel signal, so cleanup must wait out its deadline.
            stop.wait(_CLEANUP_JOIN_SECS + 2.0)
            return {}

        sdk.register("stubborn", runner, cancellable=True)
        sdk.start("stubborn")
        assert started.wait(5.0)

        async def drive() -> int:
            ticks = 0

            async def ticker() -> None:
                nonlocal ticks
                while True:
                    await asyncio.sleep(0.02)
                    ticks += 1

            spin = asyncio.ensure_future(ticker())
            try:
                result = await sdk.remove_all_async()
            finally:
                spin.cancel()
            # The worker never cooperated, so cleanup reports it rather than
            # claiming a clean teardown.
            assert result.still_running == 1
            assert not result.is_clean
            return ticks

        ticks = asyncio.run(drive())
        stop.set()
        # A blocking join would have starved the ticker for the whole deadline.
        assert ticks > 5, f"the loop was parked during cleanup (only {ticks} ticks)"


class TestThreadStartFailure:
    """A refused thread must not leave a claimed run nothing will finish."""

    def test_a_refused_thread_leaves_no_ghost(
        self, sdk: JobSDK, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sdk.register("work", lambda h, **kw: {}, cancellable=False)

        def refuse(self):  # noqa: ANN001 - patching threading.Thread.start
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(threading.Thread, "start", refuse)
        with pytest.raises(JobError):
            sdk.start("work", dedupe_key="k")
        monkeypatch.undo()

        # No run is left claiming to be active, and the key is free again.
        assert sdk.list_active() == []
        again = sdk.start("work", dedupe_key="k")
        assert _wait_terminal(sdk, again).status == DONE
