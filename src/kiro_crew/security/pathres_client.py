"""Client for the out-of-process symlink resolver (see :mod:`pathres_helper`).

One long-lived child per gateway, one request in flight at a time, driven from
an ``mc-pathres`` pool worker: the worker re-acquires the GIL twice per request
instead of once per path component. The pool, the budget, the per-prefix
cooldown and the stall classifier in :mod:`paths` keep their roles.

* The child's CODE is fixed at gateway boot: the source is read once at import
  and run as ``python -I -S -c <source>``, never the file by path. The child
  respawns on demand, so run by path an agent able to write
  ``pathres_helper.py`` (an editable install is the working tree the agent edits)
  would get its code running with the gateway's privileges on the next path check.
* A wedged child is KILLED at the deadline (:meth:`abort_if_inflight`); the worker
  blocked in ``readline`` reads EOF and is freed, which a thread doing ``realpath``
  itself never could be.
* In-process fallback latches only on a child that cannot RUN (``Popen`` raising,
  or exiting before its first answer), for a cool-off, then re-probes: the
  transient shapes (``EAGAIN``, the OOM killer) cluster exactly when the gateway
  is loaded. A kill this client performed never latches, nor does a crash after a
  first answer: both cost that one request a fail-closed refusal.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from .pathres_helper import _resolve_one

logger = logging.getLogger(__name__)

_LATCH_COOLOFF_SECS = 60.0
_HELPER_SOURCE: str = Path(__file__).with_name("pathres_helper.py").read_text(encoding="utf-8")
#: What CPython needs to START and nothing else: inheriting the gateway's
#: environment would hand a resolver its credentials. Without ``SystemRoot`` the
#: Windows interpreter cannot load its own DLLs.
_CHILD_ENV_KEYS: tuple[str, ...] = (
    ("PATH", "SystemRoot", "SYSTEMROOT", "TEMP", "TMP") if os.name == "nt" else ("PATH",)
)


class ResolverHelper:
    """One child process; requests are serialised through ``_request_lock``."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen[bytes] | None = None
        self._state_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._seq = 0
        self._inflight: tuple[int, int] | None = None  # (child pid, worker native tid)
        self._inprocess_until: float | None = None
        self._latches = 0
        self._answered_once = False
        self._killed_pids: set[int] = set()
        atexit.register(self.close)

    def _spawn(self) -> subprocess.Popen[bytes] | None:
        with self._state_lock:
            proc = self._proc
            if proc is not None and proc.poll() is None:
                return proc
            try:
                proc = subprocess.Popen(
                    # ``-I -S``: isolated, no ``site`` -- no ``.pth`` or
                    # ``sitecustomize`` runs; the helper needs only the stdlib.
                    [sys.executable, "-I", "-S", "-c", _HELPER_SOURCE],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    env={k: os.environ[k] for k in _CHILD_ENV_KEYS if k in os.environ},
                )
            except (OSError, ValueError) as exc:
                self._latch_inprocess(f"could not start ({exc})")
                return None
            self._proc = proc
            self._answered_once = False
            return proc

    def _latch_inprocess(self, why: str) -> None:
        self._inprocess_until = time.monotonic() + _LATCH_COOLOFF_SECS
        self._latches += 1
        logger.log(
            logging.WARNING if self._latches == 1 else logging.DEBUG,
            "sensitive-path resolver helper %s; resolving in-process for the next %.0f s, "
            "where a busy interpreter can make a healthy resolution exceed its budget",
            why,
            _LATCH_COOLOFF_SECS,
        )

    def _latched(self) -> bool:
        until = self._inprocess_until
        return until is not None and time.monotonic() < until

    def _kill(self, proc: subprocess.Popen[bytes], *, wait: bool) -> None:
        with self._state_lock:
            if self._proc is proc:
                self._proc = None
        self._killed_pids.add(proc.pid)
        try:
            proc.kill()
            if wait:
                proc.wait(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def close(self) -> None:
        if self._proc is not None:
            self._kill(self._proc, wait=True)

    def abort_if_inflight(self, tid: int | None) -> bool:
        """Kill the child if worker *tid*'s request is the one in flight.

        Runs on the event loop, so it never waits: the worker blocked on the pipe
        reaps the child on its own thread. A worker merely queued behind another
        request is not in flight, and killing a healthy child for it would only
        fault the other request.
        """
        inflight, proc = self._inflight, self._proc
        if tid is None or proc is None or inflight is None or inflight[1] != tid:
            return False
        self._kill(proc, wait=False)
        return True

    def resolve(self, path: str) -> tuple[str | None, str | None] | None:
        """``(realpath, resolve)`` for *path*, or ``None`` on a transport fault."""
        answers = self._request(path, [path], both=True)
        return None if answers is None else (answers[0][0], answers[0][1])

    def realpaths(self, paths: list[str]) -> list[str | None] | None:
        """``os.path.realpath`` for several anchors in ONE round-trip, in order."""
        if not paths:
            return []
        answers = self._request(paths, paths, both=False)
        return None if answers is None else [real for real, _ in answers]

    def _request(
        self, payload: str | list[str], paths: list[str], *, both: bool
    ) -> list[list[str | None]] | None:
        if self._latched():
            return [_resolve_one(p, both=both) for p in paths]
        with self._request_lock:
            proc = self._spawn()
            if proc is None:
                return [_resolve_one(p, both=both) for p in paths]
            assert proc.stdin is not None and proc.stdout is not None
            self._seq += 1
            seq = self._seq
            self._inflight = (proc.pid, threading.get_native_id())
            try:
                proc.stdin.write(json.dumps({"i": seq, "p": payload}).encode("ascii") + b"\n")
                proc.stdin.flush()
                raw = proc.stdout.readline()
            except (OSError, ValueError):
                raw = b""
            finally:
                self._inflight = None
            if not raw:
                # EOF: a kill this client performed is a wedge, not a defect; a
                # child that died unbidden before ever answering cannot run here.
                killed_by_us = proc.pid in self._killed_pids
                self._kill(proc, wait=True)
                self._killed_pids.discard(proc.pid)
                if not killed_by_us and not self._answered_once:
                    self._latch_inprocess("exited before answering its first request")
                    return [_resolve_one(p, both=both) for p in paths]
                return None
            try:
                reply = json.loads(raw.decode("ascii"))
                answers = [reply["r"]] if both else [[real, None] for real in reply["r"]]
                if reply["i"] != seq or len(answers) != len(paths):
                    raise ValueError("reply does not match the request")
                for pair in answers:
                    if len(pair) != 2 or not all(s is None or isinstance(s, str) for s in pair):
                        raise ValueError("non-string spelling")
            except (ValueError, KeyError, TypeError, UnicodeDecodeError):
                # A child answering the wrong shape cannot be trusted with the next question.
                self._kill(proc, wait=True)
                return None
            self._answered_once = True
            self._inprocess_until = None
            return answers

    def blocked_in_filesystem(self, fs_syscalls: frozenset[int], tid: int | None) -> bool | None:
        """Whether the child answering worker *tid*'s request sits in a filesystem syscall.

        Single-threaded and GIL-free, the child reports ``lstat``/``readlink`` in
        ``/proc/<pid>/syscall`` exactly when it is blocked on the mount. Attributed
        to *tid* so a worker queued behind a wedged request cannot charge its own
        healthy prefix with the other mount's stall. ``None`` when there is nothing
        to say (no request of *tid*'s in flight, no table, no ``/proc``); the caller
        then samples its own thread instead.
        """
        inflight = self._inflight
        if inflight is None or tid is None or inflight[1] != tid or not fs_syscalls:
            return None
        try:
            with open(f"/proc/{inflight[0]}/syscall", "rb") as fh:
                head = fh.read().split()
        except OSError:
            return None
        if not head:
            return None
        if head[0] == b"running":
            return False
        try:
            return int(head[0]) in fs_syscalls
        except ValueError:
            return None


_helper: ResolverHelper | None = None
_helper_lock = threading.Lock()


def helper() -> ResolverHelper:
    """The process-wide client, created on first use."""
    global _helper
    with _helper_lock:
        if _helper is None:
            _helper = ResolverHelper()
        return _helper
