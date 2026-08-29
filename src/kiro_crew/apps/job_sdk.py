"""Job SDK — app-scoped durable runs for long, user-initiated foreground work.

``CronSDK`` schedules work for later; this owns work a human started and is
watching NOW. The gap it closes is that the product had no server-side
representation of "a task of mine is running": the fact lived only in the React
component that started it, so navigating away destroyed the fact while the work
kept going, and the UI then reported the task as stopped. See
``docs/system-specs/features/app-sdk-durable-jobs-and-view-state.md``.

Three design points are load-bearing rather than incidental.

**A runner is REGISTERED, not passed per call.** ``register(kind, fn)`` binds a
kind to the callable that services it, once, at app init. ``start(kind, ...)``
then names the kind only. This is what lets a caller that cannot hold a Python
callable — the browser, and the startup reconciliation pass — address a run.

**Cancellation is cooperative and DECLARED.** A worker thread cannot be killed,
so ``cancel()`` can only ask. The SDK cannot inspect an arbitrary ``fn`` to find
out whether it ever checks, so cancellability is the app's assertion at
``register(..., cancellable=True)`` and defaults to False. A run recorded
``cancellable: false`` answers ``cancel()`` with ``False`` rather than
pretending, and the UI hides the control instead of offering a button that does
nothing.

**One writer per run file.** There is no lock helper beside ``atomic_write`` and
concurrent read-modify-write of one document is last-writer-wins, so each run is
its own file and writers never share a path: ``start`` writes the initial record
BEFORE handing off, the worker thread is the sole writer from then on, and
``cancel`` writes nothing at all (it sets an in-memory event; the worker records
the outcome at its next checkpoint). Reconciliation writes only records from a
process that is already gone.

Staleness is decided by ``_ORIGIN``, a token minted once per gateway process —
not by pid, which can be reused by the very process doing the reconciling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from kiro_crew.atomic_write import atomic_write
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: Identity of THIS gateway process. A run record carrying a different origin
#: belongs to a process that no longer exists, which is what makes staleness
#: decidable without trusting a pid (pids are reused, and the reconciling
#: process could hold the very pid a stale record names).
_ORIGIN = uuid.uuid4().hex

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
INTERRUPTED = "interrupted"

#: A run in a terminal state is never resumed and never reconciled.
TERMINAL_STATES = frozenset({DONE, FAILED, CANCELLED, INTERRUPTED})

#: The progress tail keeps a window, not a transcript — same discipline as the
#: session work ledger's event tail, so a chatty runner cannot grow a record
#: without bound.
PROGRESS_TAIL_MAX = 50

#: How long disable waits for one worker to notice its cancel signal. Bounded:
#: a runner that never polls its handle must not be able to block an app's
#: disable indefinitely, so it is reported instead.
_CLEANUP_JOIN_SECS = 5.0

_RUNS_DIRNAME = "jobs"


class JobError(RuntimeError):
    """Base class for Job SDK refusals."""


class UnknownJobKind(JobError):
    """Raised when starting a kind that has no registered runner."""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _redact(text: str) -> str:
    """Scrub runner-produced text before it is persisted or served.

    A run's error, its progress lines and its result all reach disk and the UI,
    and a runner that shells out can quote back a command line carrying a
    credential — so the same chain the app route boundaries apply runs here, at
    the point the text stops being local. Applied at INGEST rather than on the
    way out, so the record on disk is clean too.
    """
    try:
        out, _ = redact_credentials(text)
        out, _ = redact_exfiltration_urls(out)
        return out
    except Exception:  # noqa: BLE001 - redaction must never mask the error itself
        logger.debug("job text redaction failed", exc_info=True)
        return text


def _json_safe(value: Any, _depth: int = 0) -> Any:
    """Coerce runner-supplied data into something a record can always hold.

    Two jobs, deliberately in ONE pass, because they have the same reach: every
    string is redacted (a runner that shells out can put a credential in a
    progress label or a nested result value, and the record is both written to
    disk and served over HTTP), and every non-JSON value becomes its ``repr``.

    The second half is not cosmetic. ``json.dumps`` raises ``TypeError`` on a
    set, a ``Path``, or any object, and that exception used to escape the write
    and skip the live-table and dedupe-key cleanup that followed it -- leaking a
    claim no later start could ever release. An unserializable record is
    therefore made impossible at ingest rather than handled at every writer.
    """
    if _depth > 6:  # a runner cannot make us recurse forever
        return "<nested too deeply>"
    if isinstance(value, str):
        return _redact(value)
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        # NaN and infinities serialize as bare literals a stricter reader
        # rejects, so they become text.
        finite = value == value and value not in (float("inf"), float("-inf"))
        return value if finite else repr(value)
    if isinstance(value, dict):
        # Keys are runner-supplied too. Three review rounds each found a
        # different channel this pass did not cover -- top-level values, then
        # nested ones, then keys -- which is why the funnel has to be TOTAL
        # rather than extended one field at a time.
        return {_redact(str(k)): _json_safe(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(v, _depth + 1) for v in value]
    return _redact(repr(value))


@dataclass
class JobRun:
    """One run's durable record. Serialized whole; never partially updated."""

    run_id: str
    app: str
    kind: str
    status: str = QUEUED
    origin: str = ""
    pid: int = 0
    params: dict[str, Any] = field(default_factory=dict)
    dedupe_key: str = ""
    cancellable: bool = False
    created_at: str = ""
    updated_at: str = ""
    finished_at: str = ""
    progress_pct: float | None = None
    step: str = ""
    lines: list[str] = field(default_factory=list)
    error: str = ""
    result: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JobRun:
        """Build a record from disk, tolerating a hand-edited or older file.

        Unknown keys are dropped rather than raising: a run record is data the
        gateway re-reads across upgrades, so one unexpected field must not make
        an app's whole run history unreadable.
        """
        known = {f for f in cls.__dataclass_fields__}
        kwargs = {k: v for k, v in data.items() if k in known}
        kwargs.setdefault("run_id", "")
        kwargs.setdefault("app", "")
        kwargs.setdefault("kind", "")
        return cls(**kwargs)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATES


class JobHandle:
    """What a runner is handed: a cancel signal it must poll, and progress.

    ``cancelled`` is a ``threading.Event`` and checking it is the runner's own
    responsibility — the SDK has no way to interrupt a thread that never looks.
    """

    def __init__(self, run: JobRun, persist: Callable[[JobRun, "JobHandle"], bool]) -> None:
        self.cancelled = threading.Event()
        #: Set when this run's record has been deliberately dropped (app
        #: disable). Checked INSIDE the guarded writer, under the same lock the
        #: discard is set with -- checking it here and writing afterwards was a
        #: check-then-act race: cleanup could delete the file in between and the
        #: write would recreate it, because ``JobStore.write`` mkdirs and writes
        #: unconditionally and cannot tell a first write from a resurrection.
        self.discarded = threading.Event()
        self._run = run
        self._persist = persist

    @property
    def run_id(self) -> str:
        return self._run.run_id

    def progress(
        self,
        *,
        pct: float | None = None,
        step: str = "",
        line: str = "",
    ) -> None:
        """Record progress from the worker thread.

        Every field a runner supplies is sanitized on the way in, and the write
        goes through the SDK's single guarded writer, so a discarded run cannot
        be resurrected even if cleanup lands mid-call.
        """
        if pct is not None:
            self._run.progress_pct = pct
        if step:
            self._run.step = _redact(step)[:500]
        if line:
            self._run.lines.append(_redact(line)[:2000])
            if len(self._run.lines) > PROGRESS_TAIL_MAX:
                del self._run.lines[:-PROGRESS_TAIL_MAX]
        self._persist(self._run, self)


#: A runner receives its handle first, then the start call's params as kwargs.
JobFn = Callable[..., Any]


@dataclass
class CleanupResult:
    """What a disable actually achieved.

    ``still_running`` is a field rather than a log line because a cleanup that
    left app code executing must not be reportable as clean -- the caller has to
    be able to say so in the disable result.
    """

    removed: int = 0
    failed: int = 0
    still_running: int = 0

    @property
    def is_clean(self) -> bool:
        return not self.failed and not self.still_running


@dataclass
class _Runner:
    fn: JobFn
    cancellable: bool


@dataclass
class _Live:
    handle: JobHandle
    thread: threading.Thread


class JobStore:
    """One JSON file per run under ``<app data dir>/jobs/``.

    File-per-run rather than one document: ``atomic_write`` gives crash-safety
    (no reader sees a torn file) but not mutual exclusion, so two writers on one
    path would silently drop the loser's update. Separate paths remove the race
    instead of needing a lock the tree does not offer.
    """

    def __init__(self, data_dir: Path) -> None:
        self.dir = Path(data_dir) / _RUNS_DIRNAME

    def _path(self, run_id: str) -> Path:
        # run ids are SDK-minted hex; reject anything else rather than letting a
        # caller-supplied id become a path.
        if not run_id or not all(c in "0123456789abcdef" for c in run_id):
            raise ValueError(f"invalid run id: {run_id!r}")
        return self.dir / f"{run_id}.json"

    def write(self, run: JobRun) -> None:
        run.updated_at = _now()
        path = self._path(run.run_id)
        self.dir.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps(run.to_dict(), indent=1))

    def read(self, run_id: str) -> JobRun | None:
        try:
            raw = self._path(run_id).read_text()
        except (FileNotFoundError, NotADirectoryError, ValueError):
            return None
        try:
            return JobRun.from_dict(json.loads(raw))
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.warning("unreadable job run record: %s", run_id)
            return None

    def iter_runs(self) -> Iterator[JobRun]:
        if not self.dir.is_dir():
            return
        for path in sorted(self.dir.glob("*.json")):
            try:
                run = JobRun.from_dict(json.loads(path.read_text()))
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                logger.warning("skipping unreadable job record: %s", path.name)
                continue
            yield run

    def remove_all(self) -> tuple[int, int]:
        """Delete every record. Returns ``(removed, failed)``.

        The failure count is returned rather than swallowed: reporting only the
        successes let a partial delete read as a clean one, so disable would
        claim the app's runs were gone while records remained. The cron contract
        this mirrors reports a failed cleanup, and so does this.
        """
        removed = 0
        failed = 0
        if not self.dir.is_dir():
            return 0, 0
        for path in list(self.dir.glob("*.json")):
            try:
                path.unlink()
                removed += 1
            except OSError:
                failed += 1
                logger.warning("could not remove job record: %s", path.name)
        return removed, failed


class JobSDK:
    """App-scoped durable runs. One instance per app, for the gateway's life."""

    def __init__(self, app_name: str, data_dir: Path) -> None:
        self._app_name = app_name
        self._store = JobStore(data_dir)
        self._runners: dict[str, _Runner] = {}
        self._live: dict[str, _Live] = {}
        #: (kind, dedupe_key) -> run_id for runs live in THIS process. Held in
        #: memory rather than derived from the store so the dedupe check and the
        #: claim can happen in ONE critical section: the previous version read
        #: the disk between them, so two near-simultaneous starts both saw no
        #: owner and both ran -- exactly the double-click case dedupe exists to
        #: stop.
        self._keys: dict[tuple[str, str], str] = {}
        # A plain threading lock: it guards two small dicts with no awaits
        # inside, and an asyncio primitive here would bind this SDK to the loop
        # that happened to construct it.
        self._lock = threading.Lock()

    @property
    def app_name(self) -> str:
        return self._app_name

    @property
    def store(self) -> JobStore:
        return self._store

    # ── The one writer ──

    def _persist(self, run: JobRun, handle: JobHandle | None = None) -> bool:
        """Write a run's record. The ONLY path that writes one.

        Four callers used to write directly -- start, progress, the worker's
        terminal write, and reconcile -- and each had to remember the same three
        rules. Two review rounds found a different one missed each time, so the
        rules live here instead:

        * the discard check and the write happen under ONE lock acquisition, the
          same lock ``remove_all_async`` sets ``discarded`` with, so cleanup can
          no longer land between a caller's check and its write and have the
          record recreated;
        * a serialization or I/O failure returns ``False`` instead of raising, so
          a caller's bookkeeping (the live table, the dedupe claim) can never be
          skipped by an exception escaping mid-cleanup;
        * the record is JSON-safe by construction, because everything a runner
          supplies passed through ``_json_safe`` on the way in.

        Returns True when the record is on disk.
        """
        # INVARIANT, enforced here rather than at each assignment site: nothing a
        # runner supplied reaches disk unsanitized. Ingest-time scrubbing keeps
        # the in-memory record clean for the HTTP view; this is the backstop that
        # makes a MISSED ingest site harmless, which is what three rounds of
        # "you forgot this one field" says is needed. Adding a field to JobRun
        # can no longer open a new channel.
        run.step = _redact(run.step)[:500]
        run.error = _redact(run.error)[:2000]
        run.lines = [_redact(line)[:2000] for line in run.lines]
        run.params = _json_safe(run.params)
        run.result = _json_safe(run.result)
        with self._lock:
            if handle is not None and handle.discarded.is_set():
                return False
            try:
                self._store.write(run)
                return True
            except Exception:  # noqa: BLE001 - a write failure is a result, not a crash
                logger.exception("could not persist job record %s", run.run_id)
                return False

    # ── Registration ──

    def register(self, kind: str, fn: JobFn, *, cancellable: bool = False) -> None:
        """Bind ``kind`` to the callable that services it.

        Call from the app's ``on_startup`` hook. ``cancellable=True`` is the
        app's assertion that ``fn`` polls ``handle.cancelled`` at checkpoints;
        the SDK cannot verify it, so the consumer's migration checklist has to
        name those checkpoints.
        """
        if not kind:
            raise ValueError("job kind must be a non-empty string")
        with self._lock:
            self._runners[kind] = _Runner(fn=fn, cancellable=cancellable)
        logger.info("App %s registered job kind: %s", self._app_name, kind)

    def kinds(self) -> list[str]:
        with self._lock:
            return sorted(self._runners)

    def is_cancellable(self, kind: str) -> bool:
        with self._lock:
            runner = self._runners.get(kind)
        return bool(runner and runner.cancellable)

    # ── Start ──

    def start(
        self,
        kind: str,
        *,
        params: dict[str, Any] | None = None,
        dedupe_key: str = "",
    ) -> str:
        """Start a run of ``kind`` and return its run id.

        With a ``dedupe_key``, a second start while a run of the same kind and
        key is still in flight ADOPTS that run instead of beginning another —
        which is what stops a double click, or two tabs, from doing the paid
        work twice.

        Synchronous, and safe on the event loop: the only blocking work is one
        small ``atomic_write``. Unlike ``CronSDK``'s mutators there is no
        bounded store-lock spin to park the loop on, so this does not refuse an
        on-loop caller. :meth:`start_async` exists for callers who would rather
        not touch the disk from the loop thread at all.
        """
        with self._lock:
            runner = self._runners.get(kind)
        if runner is None:
            raise UnknownJobKind(
                f"app {self._app_name} has no registered runner for job kind {kind!r}"
            )

        run = JobRun(
            run_id=uuid.uuid4().hex,
            app=self._app_name,
            kind=kind,
            status=RUNNING,
            origin=_ORIGIN,
            pid=os.getpid(),
            params=_json_safe(dict(params or {})),
            dedupe_key=dedupe_key,
            cancellable=runner.cancellable,
            created_at=_now(),
        )
        handle = JobHandle(run, self._persist)
        thread = threading.Thread(
            target=self._execute,
            args=(run, runner, handle),
            name=f"job:{self._app_name}:{run.kind}",
            daemon=True,
        )

        # CHECK AND CLAIM IN ONE CRITICAL SECTION. Building the record, handle
        # and (unstarted) thread first keeps every await-free line above out of
        # the lock, so the section below holds no I/O at all -- which is what
        # makes it safe to be atomic. Splitting the check from the claim is what
        # let two concurrent starts both win.
        key = (kind, dedupe_key) if dedupe_key else None
        with self._lock:
            if key is not None:
                existing = self._keys.get(key)
                if existing is not None:
                    logger.info(
                        "App %s adopted in-flight job %s for kind=%s key=%s",
                        self._app_name,
                        existing,
                        kind,
                        dedupe_key,
                    )
                    return existing
                self._keys[key] = run.run_id
            self._live[run.run_id] = _Live(handle=handle, thread=thread)

        # Outside the lock, and BEFORE the worker exists, so this write still has
        # no competing writer. If it fails the claim must not leak, or the kind's
        # dedupe key would stay owned by a run that never started.
        if not self._persist(run):
            with self._lock:
                self._live.pop(run.run_id, None)
                if key is not None:
                    self._keys.pop(key, None)
            raise JobError(f"could not persist the initial record for job kind {kind!r}")
        try:
            thread.start()
        except RuntimeError as exc:
            # The OS refused a thread. The claim, the live entry and a record
            # saying `running` are all in place by now, so unwinding all three is
            # the only way this does not become a ghost run nothing will ever
            # finish or reconcile (its origin is ours and reconcile spares a live
            # entry, so it would sit `running` for the process's whole life).
            with self._lock:
                self._live.pop(run.run_id, None)
                if key is not None:
                    self._keys.pop(key, None)
            run.status = FAILED
            run.error = "the host refused a new thread for this job"
            run.finished_at = _now()
            self._persist(run)
            self._audit("job_start", run.run_id, "failed", error=str(exc))
            raise JobError(f"could not start a worker for job kind {kind!r}: {exc}") from exc
        self._audit("job_start", run.run_id, "ok")
        return run.run_id

    async def start_async(
        self,
        kind: str,
        *,
        params: dict[str, Any] | None = None,
        dedupe_key: str = "",
    ) -> str:
        """Loop-native :meth:`start` — the initial record write is offloaded so
        an on-loop caller never touches the disk on the loop thread."""
        return await asyncio.to_thread(self.start, kind, params=params, dedupe_key=dedupe_key)

    def _execute(self, run: JobRun, runner: _Runner, handle: JobHandle) -> None:
        """The worker body. Sole writer of this run's record from here on."""
        try:
            result = runner.fn(handle, **run.params)
            if handle.cancelled.is_set():
                run.status = CANCELLED
            else:
                run.status = DONE
                if isinstance(result, dict):
                    # A runner's return value is served over HTTP like its
                    # A runner's return value is served over HTTP like its
                    # progress lines are, so it gets the same ingest-time pass:
                    # redacted AND made JSON-safe, all the way down. Redacting
                    # only top-level strings left a credential reachable inside
                    # a nested dict or list.
                    run.result = _json_safe(result)
        except Exception as exc:  # noqa: BLE001 - a runner's failure is data, not a crash
            run.status = FAILED
            run.error = _redact(str(exc))[:2000]
            logger.warning(
                "App %s job %s (%s) failed: %s",
                self._app_name,
                run.run_id,
                run.kind,
                run.error,
            )
        finally:
            run.finished_at = _now()
            # The guarded writer owns the discard check, so a cleanup landing
            # mid-write cannot have this record recreated, and a failure comes
            # back as False rather than as an exception that would skip the
            # bookkeeping below. That skip is what leaked a dedupe claim no
            # later start could release.
            self._write_terminal(run, handle)
            with self._lock:
                self._live.pop(run.run_id, None)
                if run.dedupe_key:
                    self._keys.pop((run.kind, run.dedupe_key), None)
            self._audit(f"job_{run.status}", run.run_id, "ok")

    def _write_terminal(self, run: JobRun, handle: JobHandle) -> None:
        """Persist a run's final state, retrying once.

        A lost terminal write is not cosmetic: the record stays ``running`` and
        the UI keeps reporting work that has finished. One retry covers a
        transient failure. If it still fails the run has already been dropped
        from the live table, so ``reconcile`` resolves it -- immediately if
        anything calls it, and at the next gateway start regardless, since by
        then the record's origin is foreign. That residue is bounded but real,
        and a periodic sweep is deliberately out of scope here.

        A discarded run is not retried: ``_persist`` refuses it by design, and
        retrying would only burn the delay before refusing again.
        """
        if handle.discarded.is_set():
            return
        for attempt in (1, 2):
            if self._persist(run, handle):
                return
            if attempt == 1:
                time.sleep(0.05)
        logger.error(
            "could not persist terminal state for job %s; it will be reconciled "
            "as interrupted rather than left running",
            run.run_id,
        )

    # ── Read ──

    def get(self, run_id: str) -> JobRun | None:
        return self._store.read(run_id)

    def list_active(self, kind: str = "") -> list[JobRun]:
        """Runs that are not in a terminal state — what a fresh mount adopts."""
        return [
            r for r in self._store.iter_runs() if not r.is_terminal and (not kind or r.kind == kind)
        ]

    def list_recent(self, kind: str = "", limit: int = 20) -> list[JobRun]:
        """Most recently updated runs first, terminal ones included."""
        runs = [r for r in self._store.iter_runs() if not kind or r.kind == kind]
        runs.sort(key=lambda r: (r.updated_at, r.created_at), reverse=True)
        return runs[: max(0, limit)]

    # ── Cancel ──

    def cancel(self, run_id: str) -> bool:
        """Ask a live, cancellable run to stop. Writes nothing.

        Returns False — rather than pretending — when the run is not live in
        this process or was never declared cancellable. The worker records the
        outcome itself at its next checkpoint, which keeps this run's file to a
        single writer.
        """
        with self._lock:
            live = self._live.get(run_id)
        if live is None:
            return False
        run = self._store.read(run_id)
        if run is None or not run.cancellable or run.is_terminal:
            return False
        live.handle.cancelled.set()
        self._audit("job_cancel", run_id, "ok")
        return True

    async def cancel_async(self, run_id: str) -> bool:
        """Loop-native :meth:`cancel`. Present so an on-loop caller does not
        have to know which methods happen to touch the disk."""
        return await asyncio.to_thread(self.cancel, run_id)

    # ── Reconciliation ──

    def reconcile(self) -> int:
        """Resolve records left non-terminal by a process that is gone.

        A run must never be left ``running`` forever and must never silently
        vanish — the two directions the hand-rolled predecessors got wrong. The
        reason distinguishes a lost process from a kind whose runner is no
        longer registered (the app was disabled, or the kind was removed), which
        is why this runs only after every app has registered.

        This is the ONE path that consumes records it did not write -- a file
        left by an older build, or hand-edited during an incident -- so a single
        unusable record must cost only itself. ``JobStore._path`` raises
        ``ValueError`` on a run id it will not turn into a path, and letting that
        escape would abandon every remaining run of this app, leaving exactly the
        stuck-``running`` state the pass exists to clear.
        """
        flipped = 0
        for run in self._store.iter_runs():
            if run.is_terminal:
                continue
            with self._lock:
                live = run.run_id in self._live
                known = run.kind in self._runners
            # Skip only a run this process is ACTUALLY executing. Matching on
            # origin alone would spare a record this process wrote and then lost
            # (a terminal write that failed twice), which is the stuck-`running`
            # state the pass exists to clear.
            if run.origin == _ORIGIN and live:
                continue
            run.status = INTERRUPTED
            run.finished_at = _now()
            run.error = (
                "the gateway restarted while this was running"
                if known
                else f"the gateway restarted and no runner is registered for {run.kind!r}"
            )
            if not self._persist(run):
                continue
            flipped += 1
            self._audit("job_interrupted", run.run_id, "ok")
        if flipped:
            logger.info("App %s: reconciled %d interrupted job run(s)", self._app_name, flipped)
        return flipped

    # ── Cleanup ──

    async def remove_all_async(self) -> CleanupResult:
        """Stop this app's runs and drop their records.

        Called on disable, mirroring ``CronSDK.remove_all_async``, and it now
        does what "disable" implies: every live handle is marked discarded and
        cancelled under the lock the guarded writer checks, and then each worker
        is **bounded-joined**. Signalling alone left the threads running -- a
        disabled app kept doing real, side-effecting work with its records
        already deleted. Waiting is the only correct answer: a thread cannot be
        killed, and abandoning it is the defect rather than the fix.

        A worker that outlives the deadline is reported, not waited on forever.
        Gateway shutdown is a separate case left as accepted residue: these are
        daemon threads, so the interpreter reaps them at exit without a chance
        to finish, and draining every app's runs there would delay shutdown for
        work nobody is waiting on.
        """
        with self._lock:
            live = list(self._live.values())
            # Marked and cleared under the SAME lock the guarded writer takes,
            # so a worker cannot slip a write in between this and the delete.
            # The dedupe index goes too, or a key would stay owned by a run
            # whose record no longer exists and the next start would adopt a
            # ghost.
            for entry in live:
                entry.handle.discarded.set()
                entry.handle.cancelled.set()
            self._live.clear()
            self._keys.clear()

        # Joined OFF THE LOOP and outside the lock. Off the loop because a
        # blocking join in an async method parks the whole gateway for its
        # deadline -- the exact hazard CronSDK's docstring spells out, which this
        # method walked into while fixing the previous round. Outside the lock
        # because a worker's final write needs that lock, so holding it here
        # would deadlock against the thread being waited on.
        stubborn = await asyncio.to_thread(self._join_workers, live)
        if stubborn:
            logger.warning(
                "App %s: %d job worker(s) did not stop within %.0fs and are still "
                "running with their records removed: %s",
                self._app_name,
                len(stubborn),
                _CLEANUP_JOIN_SECS,
                ", ".join(stubborn),
            )

        removed, failed = await asyncio.to_thread(self._store.remove_all)
        if removed or failed:
            self._audit(
                "job_remove_all",
                f"removed={removed} failed={failed} stubborn={len(stubborn)}",
                "ok" if not failed and not stubborn else "partial",
            )
            logger.info(
                "App %s removed %d job record(s), %d failed", self._app_name, removed, failed
            )
        return CleanupResult(removed=removed, failed=failed, still_running=len(stubborn))

    def _join_workers(self, live: list[_Live]) -> list[str]:
        """Wait for each worker, bounded. Runs on a worker thread, never the loop."""
        stubborn = []
        for entry in live:
            entry.thread.join(timeout=_CLEANUP_JOIN_SECS)
            if entry.thread.is_alive():
                stubborn.append(entry.thread.name)
        return stubborn

    # ── Audit ──

    def _audit(self, operation: str, resources: str, outcome: str, *, error: str = "") -> None:
        try:
            sel().log_api_access(
                caller=f"app:{self._app_name}",
                operation=f"jobs.{operation}",
                outcome=outcome,
                source=self._app_name,
                resources=resources[:200],
                error=error[:200],
            )
        except Exception:  # noqa: BLE001 - an audit failure must not fail the job
            logger.debug("job SEL audit failed", exc_info=True)


# ── Process-wide registry ──
#
# The shared ``_jobs/*`` route family is mounted ONCE for every app and resolves
# the app from the URL, so it needs a name -> SDK lookup; startup reconciliation
# needs the same table. That makes this registry part of the design rather than
# a shortcut around passing the SDK around.

_SDKS: dict[str, JobSDK] = {}
_SDKS_LOCK = threading.Lock()


def register_sdk(sdk: JobSDK) -> None:
    with _SDKS_LOCK:
        _SDKS[sdk.app_name] = sdk


def get_sdk(app_name: str) -> JobSDK | None:
    with _SDKS_LOCK:
        return _SDKS.get(app_name)


def forget_sdk(app_name: str) -> None:
    with _SDKS_LOCK:
        _SDKS.pop(app_name, None)


def registered_apps() -> list[str]:
    with _SDKS_LOCK:
        return sorted(_SDKS)


def reconcile_all() -> int:
    """Reconcile every registered app's runs. Call once, after startup.

    Placed after the enable loop deliberately: before it, a kind with no runner
    is indistinguishable from an app that has not loaded yet, so an early pass
    would blame the app for the gateway's own boot order.
    """
    with _SDKS_LOCK:
        sdks = list(_SDKS.values())
    total = 0
    for sdk in sdks:
        try:
            total += sdk.reconcile()
        except Exception:  # noqa: BLE001 - one app's bad store must not stop the rest
            logger.exception("job reconciliation failed for app %s", sdk.app_name)
    return total
