"""The out-of-process sensitive-path resolver (``security/pathres_{helper,client}.py``).

``realpath`` releases and re-acquires the GIL once per path component, and the
anchor rebuild does ~130 of those per call; beside a few CPU-bound sibling threads
each re-acquisition waits a switch interval, so a rebuild that costs 2 ms idle
costs seconds and expires its budget on a healthy local disk, and every gate then
refuses ordinary project files. These tests pin the fix: the resolution runs in a
helper PROCESS with its own GIL, reached from the ``mc-pathres`` pool worker by one
pipe round-trip per request.
"""

from __future__ import annotations

import ast
import io
import json
import os
import sys
import threading
import time
from collections.abc import Iterator

import pytest

import kiro_crew.executors as ex
from kiro_crew import security
from kiro_crew.security import pathres_client, pathres_helper

_REAL_BLOCKED_IN_FILESYSTEM = security.paths._worker_blocked_in_filesystem
_WEDGED_HELPER_SOURCE = "import time\nwhile True:\n    time.sleep(3600)\n"


@pytest.fixture(autouse=True)
def _fresh_resolver_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(security, "_path_resolve_degraded", {})
    monkeypatch.setattr(security, "_path_resolve_wedged", [])
    monkeypatch.setattr(security, "_PATH_RESOLVE_TIMEOUT_SECS", 0.2)
    monkeypatch.setattr(security.paths, "_PATH_RESOLVE_REBUILD_TIMEOUT_SECS", 0.2)
    monkeypatch.setattr(security.paths, "_worker_blocked_in_filesystem", lambda tid: True)
    security._home_targets_cache.clear()
    yield
    security._home_targets_cache.clear()
    ex.shutdown_maintenance_executor()


@pytest.fixture
def helper(monkeypatch: pytest.MonkeyPatch) -> Iterator[pathres_client.ResolverHelper]:
    """A fresh helper process, torn down after the test."""
    fresh = pathres_client.ResolverHelper()
    monkeypatch.setattr(pathres_client, "_helper", fresh)
    try:
        yield fresh
    finally:
        fresh.close()


def _serve(lines: list[str]) -> list[dict]:
    """Drive the helper program's loop in-process over in-memory streams."""
    out = io.StringIO()
    pathres_helper.serve(io.StringIO("".join(line + "\n" for line in lines)), out)
    return [json.loads(raw) for raw in out.getvalue().splitlines()]


def _hog_the_gil(stop: threading.Event) -> None:
    while not stop.is_set():
        sum(i * i for i in range(20_000))


# ── The reproduction ────────────────────────────────────────────────────────────


def test_resolution_completes_inside_the_budget_while_a_sibling_thread_hogs_the_gil(
    helper, monkeypatch, tmp_path
) -> None:
    """Anchors and candidate, cold cache, under one GIL hog: the workload that
    expires the budget in-process (measured 1.7 s for the rebuild alone)."""
    monkeypatch.setattr(security, "_PATH_RESOLVE_TIMEOUT_SECS", 2.0)
    monkeypatch.setattr(
        security.paths, "_worker_blocked_in_filesystem", _REAL_BLOCKED_IN_FILESYSTEM
    )
    target = tmp_path / "ws" / "README.md"
    target.parent.mkdir()
    target.write_text("x")
    assert helper.resolve("/") is not None  # the one-time spawn is not the workload
    stop = threading.Event()
    hog = threading.Thread(target=_hog_the_gil, args=(stop,), daemon=True)
    hog.start()
    try:
        time.sleep(0.05)
        started = time.monotonic()
        refusal = security.sensitive_path_refusal(str(target))
        elapsed = time.monotonic() - started
    finally:
        stop.set()
        hog.join()
    assert refusal is None, f"a healthy project file must not be refused: {refusal!r}"
    if os.name != "nt":  # coarse bound with headroom for a loaded CI host; ~0.3 s alone
        assert elapsed < 1.5, f"resolution took {elapsed:.2f}s under one GIL hog"
    assert security._path_resolve_degraded == {}, "no prefix may be charged for a healthy disk"


# ── The helper program ──────────────────────────────────────────────────────────


def test_helper_answers_are_identical_to_in_process_resolution(helper, tmp_path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this platform")
    paths = [str(link / "f.txt"), str(tmp_path / "missing" / "deep"), str(tmp_path)]
    for path in paths:
        assert helper.resolve(path) == (os.path.realpath(path), str(security.Path(path).resolve()))
    assert helper.realpaths(paths) == [os.path.realpath(p) for p in paths]
    assert helper.realpaths([]) == []
    assert not helper._latched()


def test_the_helper_program_answers_single_and_list_requests_in_order(tmp_path) -> None:
    missing = str(tmp_path / "missing" / "deep")
    replies = _serve(
        [json.dumps({"i": 1, "p": str(tmp_path)}), json.dumps({"i": 2, "p": [missing, "/"]})]
    )
    assert [r["i"] for r in replies] == [1, 2]
    assert replies[0]["r"] == [os.path.realpath(str(tmp_path)), str(tmp_path.resolve())]
    assert replies[1]["r"] == [os.path.realpath(missing), os.path.realpath("/")]


@pytest.mark.parametrize(
    "bad", ["not json", '{"i": 1}', '{"i": 1, "p": 7}', '{"i": 1, "p": ["/", 7]}', '{"p": "/"}']
)
def test_the_helper_program_exits_on_a_malformed_request_instead_of_guessing(bad) -> None:
    """The client reads EOF as a transport fault; a helper that guessed at a
    malformed request could answer the wrong question."""
    replies = _serve([json.dumps({"i": 1, "p": "/"}), bad, json.dumps({"i": 3, "p": "/"})])
    assert [r["i"] for r in replies] == [1], "answers stop at the malformed line"


def test_the_helper_program_keeps_surrogate_escaped_bytes(helper, tmp_path) -> None:
    weird = str(tmp_path) + "/\udcff\udcfe/x"  # undecodable bytes, as os.fsdecode yields them
    (reply,) = _serve([json.dumps({"i": 9, "p": weird}, ensure_ascii=True)])
    assert reply["r"][0] == os.path.realpath(weird)
    assert helper.resolve(weird)[0] == os.path.realpath(weird)  # type: ignore[index]


def test_the_helper_program_imports_only_the_stdlib() -> None:
    """The child runs ``-I -S -c <source>``: no site, no package on ``sys.path``. A
    package import would make every spawn die before its first answer."""
    imported: set[str] = set()
    for node in ast.walk(ast.parse(pathres_client._HELPER_SOURCE)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported and imported <= set(sys.stdlib_module_names), sorted(imported)


def test_the_helper_runs_boot_captured_source_with_a_minimal_environment(
    helper, monkeypatch
) -> None:
    """Run BY PATH, an agent able to edit ``pathres_helper.py`` (an editable install)
    would get its code running with gateway privileges on the next path check, no
    restart needed. And the child must not inherit the gateway's credentials."""
    seen: dict[str, object] = {}
    real_popen = pathres_client.subprocess.Popen

    def recording_popen(*args, **kwargs):
        seen["argv"], seen["env"] = args[0], kwargs.get("env")
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(pathres_client.subprocess, "Popen", recording_popen)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-leak")
    helper.close()
    assert helper.resolve("/") is not None
    argv = seen["argv"]
    assert argv[:4] == [sys.executable, "-I", "-S", "-c"]  # type: ignore[index]
    assert argv[4] == pathres_client._HELPER_SOURCE  # type: ignore[index]
    assert not any(str(a).endswith("pathres_helper.py") for a in argv)  # type: ignore[union-attr]
    helper_file = security.Path(pathres_client.__file__).with_name("pathres_helper.py")
    assert helper_file.read_text(encoding="utf-8") == pathres_client._HELPER_SOURCE
    assert set(seen["env"]) <= set(pathres_client._CHILD_ENV_KEYS)  # type: ignore[arg-type]


# ── Round-trips: the anchors travel as one request ──────────────────────────────


def test_the_root_key_and_the_rebuild_are_one_helper_request_each(
    helper, monkeypatch, tmp_path
) -> None:
    """Round-trips, not resolutions, are what scales: seven single root requests per
    gate call became ~80,000 round-trips for one test file on Windows CI. Each env
    var naming the SAME directory is sent once -- the helper resolves what it is
    handed, only the answer dict collapses duplicates."""
    shared = str(tmp_path / "crew")
    monkeypatch.setenv("KIROCREW_HOME", shared)
    monkeypatch.setenv("KIRO_HOME", shared)
    requests: list[list[str]] = []
    real_many = helper.realpaths
    monkeypatch.setattr(helper, "realpaths", lambda ps: requests.append(list(ps)) or real_many(ps))
    monkeypatch.setattr(helper, "resolve", lambda p: pytest.fail(f"single request for {p!r}"))
    roots = security._resolve_root_anchors(str(security.Path.home()))
    assert len(requests) == 1 and str(security.Path.home()) in requests[0], requests
    assert requests[0].count(shared) == 1 and len(requests[0]) == len(set(requests[0]))
    assert roots.home == os.path.realpath(str(security.Path.home()))
    assert roots.crew_home == roots.kiro_home == os.path.realpath(shared)
    targets = security._home_dir_targets_uncached(security._SENSITIVE_HOME_DIRS, roots)
    assert len(requests) == 2 and len(requests[1]) == len(set(requests[1])), requests
    assert os.path.join(shared, "token_signing.key").casefold() in targets
    assert any(p.endswith("token_signing.key") for p in requests[1]), requests[1]


# ── Faults: killed, cannot start, transport ─────────────────────────────────────


def test_killing_a_wedged_helper_frees_the_pool_worker(helper, monkeypatch) -> None:
    """A timed-out THREAD doing realpath is pinned for the process lifetime; a
    timed-out HELPER is killed, its worker reads EOF and is reclaimed, and the next
    request gets a fresh helper."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pathres_client, "_HELPER_SOURCE", _WEDGED_HELPER_SOURCE)
        proc = helper._spawn()
    assert proc is not None
    with pytest.raises(security.PathResolutionStalled):
        security._resolved_forms_bounded("/some/where")
    deadline = time.monotonic() + 5.0
    while (proc.poll() is None or security._wedged_workers()) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert proc.poll() is not None, "the wedged helper must be killed, not waited for"
    assert security._wedged_workers() == 0, "the worker blocked on it unwound"
    assert not helper._latched(), "an abort is not evidence the helper cannot run"
    assert helper.resolve("/") == (os.path.realpath("/"), str(security.Path("/").resolve()))
    assert helper._proc is not proc


def test_abort_if_inflight_kills_only_for_the_owning_worker(helper) -> None:
    """A deadline owner must not fault a healthy helper busy with ANOTHER request;
    and a worker queued behind a wedged request must not sample the wedged child as
    its own stall (it falls back to its own thread's sample, the load arm)."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pathres_client, "_HELPER_SOURCE", _WEDGED_HELPER_SOURCE)
        proc = helper._spawn()
    assert proc is not None
    owner: list[int] = []
    results: list[object] = []

    def stuck() -> None:
        owner.append(threading.get_native_id())
        results.append(helper.resolve("/x"))

    thread = threading.Thread(target=stuck, daemon=True)
    thread.start()
    while helper._inflight is None:
        time.sleep(0.01)
    table = security.paths._FS_BLOCKING_SYSCALLS or frozenset({0})
    assert helper.blocked_in_filesystem(table, threading.get_native_id()) is None
    assert helper.blocked_in_filesystem(table, owner[0]) is not None or not os.path.exists("/proc")
    assert helper.abort_if_inflight(None) is False
    assert helper.abort_if_inflight(owner[0] + 1) is False
    assert helper._proc is proc
    assert helper.abort_if_inflight(owner[0]) is True
    thread.join(timeout=5)
    assert results == [None], "the aborted request is a fault, refused upstream"
    assert not helper._latched() and helper.resolve("/") is not None


def test_a_helper_that_cannot_start_falls_back_in_process_once(monkeypatch, caplog) -> None:
    fresh = pathres_client.ResolverHelper()
    monkeypatch.setattr(pathres_client, "_helper", fresh)
    monkeypatch.setattr(sys, "executable", "/nonexistent/python")
    with caplog.at_level("WARNING", logger="kiro_crew.security.pathres_client"):
        first = fresh.resolve("/")
        second = fresh.realpaths(["/"])
    assert first == (os.path.realpath("/"), str(security.Path("/").resolve()))
    assert second == [os.path.realpath("/")]
    assert fresh._latched()
    assert sum("could not start" in r.message for r in caplog.records) == 1, "warn once"


def test_an_in_process_latch_re_probes_after_the_cool_off(monkeypatch) -> None:
    """A spawn failure can be transient (``EAGAIN``, an OOM-killed child) and is
    likeliest when the gateway is loaded -- the condition the helper exists for."""
    fresh = pathres_client.ResolverHelper()
    monkeypatch.setattr(pathres_client, "_helper", fresh)
    monkeypatch.setattr(pathres_client, "_LATCH_COOLOFF_SECS", 0.05)
    real_executable = sys.executable
    monkeypatch.setattr(sys, "executable", "/nonexistent/python")
    try:
        assert fresh.resolve("/") is not None and fresh._latched()
        assert fresh._proc is None, "no spawn is attempted inside the cool-off"
        monkeypatch.setattr(sys, "executable", real_executable)
        time.sleep(0.1)
        assert fresh.resolve("/") == (os.path.realpath("/"), str(security.Path("/").resolve()))
        assert not fresh._latched(), "the re-probe succeeded and the latch is gone"
    finally:
        fresh.close()


def test_a_helper_that_dies_before_its_first_answer_latches_to_in_process(
    monkeypatch, caplog
) -> None:
    """An interpreter that starts but cannot come up in the child environment
    (Windows without ``SystemRoot``) is a fact about the host, not a wedge."""
    fresh = pathres_client.ResolverHelper()
    monkeypatch.setattr(pathres_client, "_helper", fresh)
    monkeypatch.setattr(pathres_client, "_HELPER_SOURCE", "import sys; sys.exit(3)")
    with caplog.at_level("WARNING", logger="kiro_crew.security.pathres_client"):
        assert fresh.resolve("/") == (os.path.realpath("/"), str(security.Path("/").resolve()))
    assert fresh._latched()
    assert sum("before answering its first request" in r.message for r in caplog.records) == 1
    fresh.close()


def test_a_transport_fault_fails_closed_on_both_halves_without_a_cooldown(
    helper, monkeypatch, tmp_path
) -> None:
    """A ``None`` from the helper is a resolution that did not complete. An empty
    candidate set would read as "resolved, no other spelling" and a workspace
    symlink into a credential store would pass on its lexical form; an anchor
    degraded to its lexical spelling would leave a symlinked credential home
    unmasked. Both refuse -- and charge no prefix, because the disk did not stall."""
    monkeypatch.setattr(helper, "resolve", lambda path: None)
    monkeypatch.setattr(helper, "realpaths", lambda paths: None)
    link = str(tmp_path / "ws" / "link")
    with pytest.raises(security.PathResolutionStalled):
        security._resolved_forms_bounded(link)
    refusal = security.sensitive_path_refusal(link)
    assert refusal is not None and security.is_unverifiable_path_refusal(refusal)
    with pytest.raises(security.PathResolutionStalled):
        security._realpath_or_none(str(security.Path.home()))
    with pytest.raises(security.PathResolutionStalled):
        security.sandbox_credential_targets()
    assert security._path_resolve_degraded == {}, "a transport fault is not a stalled mount"


def test_a_transport_fault_during_the_grace_wait_refuses_instead_of_going_lexical(
    monkeypatch,
) -> None:
    """The grace wait has its own exception ladder; a fault the worker raises inside
    it must reach the caller as the refusal it is, not as the pool-fault arm's
    lexical fallback."""
    monkeypatch.setattr(security.paths, "_PATH_RESOLVE_GRACE_FACTOR", 20.0)
    monkeypatch.setattr(security.paths, "_PATH_RESOLVE_GRACE_MAX_SECS", 5.0)

    def slow_then_faulting(expanded: str) -> set[str]:
        time.sleep(0.5)  # past the 0.2 s budget, faults inside the grace
        raise security.PathResolutionStalled(expanded, security._stall_prefix(expanded))

    monkeypatch.setattr(security, "_resolved_spellings", slow_then_faulting)
    with pytest.raises(security.PathResolutionStalled):
        security._candidate_forms("/home/someone/ws/link-to-credentials")
    assert security.is_sensitive_path("/home/someone/ws/link-to-credentials") is True
    assert security._path_resolve_degraded == {}, "no cooldown for a transport fault"
