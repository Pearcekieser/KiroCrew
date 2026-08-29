"""Every remaining agent-spec scan reads through the hardened reader (#6695).

Seven call sites read ``~/.kiro/agents/*.json`` with a hand-rolled
``json.loads(read_text())`` until this migration; each now goes through
``agent_discovery._read_agent_spec`` -- the size-capped, sensitive-symlink- and
non-object-refusing reader #5423 adopted for ``_resolve_agent_model``. Per
surface this pins the two properties the migration promises: a refused spec is
SKIPPED (it degrades exactly like an absent one, and the surface still
answers), and a valid spec is unaffected under the same cap.

Refusal is exercised with a LOWERED ``hooks.MAX_FILE_BYTES`` (the property is
that the cap is consulted, not its value) and with non-object JSON -- both
observable without planting symlinks, mirroring #5423's tests. One
representative symlink test proves the sensitive-target guard applies through
a migrated caller; the guard itself lives in ``_read_agent_spec`` and has its
own coverage.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web

from kiro_crew.agent import migrate_agent_specs
from kiro_crew.dashboard.chat_persistence import _build_kiro_model_map
from kiro_crew.dashboard.handlers.agents import (
    _namespaced_agent_file_exists,
    api_agent_detail,
)
from kiro_crew.dashboard.handlers.mcp import (
    _collect_server_rows,
    _launch_specs_for,
    api_mcp_active,
)

# The two refusal shapes cheap enough to plant per surface. "oversized" is the
# differential case (the old read_text path had no cap, so it PARSED these);
# "non_object" pins that valid-JSON-wrong-shape degrades as absent everywhere,
# including the surfaces whose old parse crashed on it (AttributeError past an
# ``except (JSONDecodeError, OSError)``).
REFUSALS = ("oversized", "non_object")


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    """Isolated agents dir behind a lowered read cap.

    ``KIRO_AGENTS_DIR`` is the documented override hook every migrated site
    resolves through ``kiro_agents_dir_path()``; the cap is lowered rather than
    writing a real 50 MB fixture (same trade #5423's tests made).
    """
    from kiro_crew import hooks

    monkeypatch.setattr(hooks, "MAX_FILE_BYTES", 256)
    monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", tmp_path)
    return tmp_path


def _plant(agents_dir: Path, filename: str, spec: dict) -> Path:
    p = agents_dir / filename
    p.write_text(json.dumps(spec), encoding="utf-8")
    return p


def _plant_refused(agents_dir: Path, filename: str, spec: dict, kind: str) -> Path:
    p = agents_dir / filename
    if kind == "oversized":
        body = dict(spec)
        body["pad"] = "x" * 1024  # far past the lowered 256-byte cap
        p.write_text(json.dumps(body), encoding="utf-8")
    else:  # non_object: valid JSON, wrong shape
        p.write_text(json.dumps([spec]), encoding="utf-8")
    return p


class TestMigrateAgentSpecs:
    """agent.migrate_agent_specs -- the one site that also WRITES."""

    @pytest.mark.parametrize("kind", REFUSALS)
    def test_refused_spec_is_never_rewritten(self, agents_dir, kind):
        """A spec the reader refuses is not cleaned AND not written back.

        Strictly safer than the old path, which read (and rewrote) whatever
        the file held: refusal now keeps the write from happening at all.
        """
        p = _plant_refused(agents_dir, "dirty.json", {"name": "dirty", "model_managed": True}, kind)
        before = p.read_text(encoding="utf-8")

        assert migrate_agent_specs() == 0
        assert p.read_text(encoding="utf-8") == before

    def test_valid_spec_still_cleaned_under_the_same_cap(self, agents_dir):
        p = _plant(agents_dir, "dirty.json", {"name": "dirty", "model_managed": True})

        assert migrate_agent_specs() == 1
        assert "model_managed" not in json.loads(p.read_text(encoding="utf-8"))


class TestBuildKiroModelMap:
    """chat_persistence._build_kiro_model_map -- feeds legacy session restore."""

    @pytest.mark.parametrize("kind", REFUSALS)
    def test_refused_spec_is_skipped_not_fatal(self, agents_dir, kind):
        """The refused file contributes nothing and the scan keeps going.

        Under the old parse a non-object spec raised past the inner except and
        aborted the whole scan through the outer one; now it is a per-file skip.
        """
        _plant_refused(agents_dir, "bad.json", {"name": "bad", "model": "pinned-by-bad"}, kind)
        _plant(agents_dir, "good.json", {"name": "good", "model": "pinned-by-good"})

        out = _build_kiro_model_map()

        assert out.get("good") == "pinned-by-good"
        assert "bad" not in out

    def test_valid_spec_still_maps_under_the_same_cap(self, agents_dir):
        _plant(agents_dir, "good.json", {"name": "good", "model": "pinned-by-good"})

        out = _build_kiro_model_map()

        # Keyed by both the declared name and the file stem (here identical).
        assert out == {"good": "pinned-by-good"}


class TestNamespacedAgentFileExists:
    """handlers.agents._namespaced_agent_file_exists -- the app-agent probe."""

    @pytest.mark.parametrize("kind", REFUSALS)
    def test_refused_spec_does_not_back_the_agent(self, agents_dir, kind):
        _plant_refused(agents_dir, "app--probe.json", {"name": "probe"}, kind)

        assert _namespaced_agent_file_exists("probe") is False

    def test_valid_spec_still_backs_the_agent(self, agents_dir):
        _plant(agents_dir, "app--probe.json", {"name": "probe"})

        assert _namespaced_agent_file_exists("probe") is True


def _detail_request(name: str) -> MagicMock:
    request = MagicMock(spec=web.Request)
    request.method = "GET"
    request.match_info = {"name": name}
    request.app = {"state": MagicMock()}
    return request


class TestApiAgentDetail:
    """handlers.agents.api_agent_detail -- GET by-name lookup."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", REFUSALS)
    async def test_refused_spec_reads_as_absent(self, agents_dir, kind):
        """A refused spec is a 404, not a 500: the old parse let a non-object
        file escape as AttributeError past ``except (JSONDecodeError, OSError)``."""
        _plant_refused(agents_dir, "ghost.json", {"name": "ghost"}, kind)

        resp = await api_agent_detail(_detail_request("ghost"))

        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_valid_spec_still_served_under_the_same_cap(self, agents_dir):
        _plant(agents_dir, "real.json", {"name": "real", "model": "pinned-by-real"})

        resp = await api_agent_detail(_detail_request("real"))

        assert resp.status == 200
        assert json.loads(resp.text)["name"] == "real"


def _mcp_request(agent: str) -> MagicMock:
    request = MagicMock(spec=web.Request)
    request.query = {"agent": agent}
    return request


@pytest.fixture
def identity_bindings(monkeypatch):
    """Bind every Kiro Crew agent name to a same-named kiro agent.

    Without this the real resolver maps an unknown name onto the ``kirocrew``
    default, so ``/api/mcp/active`` would always take the global-scope branch
    and the per-agent branch under test would be unreachable (same fixture
    shape as test_handlers_mcp_coverage.py).
    """
    from types import SimpleNamespace

    import kiro_crew.config.loader as loader

    monkeypatch.setattr(
        loader,
        "resolve_agent_bindings",
        lambda cfg, name: SimpleNamespace(kiro_agent=name),
    )


class TestApiMcpActive:
    """handlers.mcp.api_mcp_active -- per-agent mcpServers list."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", REFUSALS)
    async def test_refused_spec_reads_as_absent(self, agents_dir, identity_bindings, kind):
        _plant_refused(
            agents_dir, "probe.json", {"name": "probe-6695", "mcpServers": {"srv": {}}}, kind
        )

        resp = await api_mcp_active(_mcp_request("probe-6695"))

        assert resp.status == 200
        assert json.loads(resp.text) == []

    @pytest.mark.asyncio
    async def test_valid_spec_still_lists_servers(self, agents_dir, identity_bindings):
        _plant(agents_dir, "probe.json", {"name": "probe-6695", "mcpServers": {"b": {}, "a": {}}})

        resp = await api_mcp_active(_mcp_request("probe-6695"))

        assert json.loads(resp.text) == [
            {"name": "a", "enabled": True},
            {"name": "b", "enabled": True},
        ]


class TestCollectServerRows:
    """handlers.mcp._collect_server_rows -- the fleet row scan."""

    @pytest.mark.parametrize("kind", REFUSALS)
    def test_refused_spec_contributes_no_rows(self, agents_dir, kind):
        _plant_refused(
            agents_dir,
            "bad.json",
            {"name": "bad", "mcpServers": {"phantom": {"command": "x"}}},
            kind,
        )

        assert "phantom" not in _collect_server_rows()

    def test_valid_spec_rows_survive_the_same_cap(self, agents_dir):
        _plant(agents_dir, "good.json", {"name": "good", "mcpServers": {"real": {"command": "x"}}})

        assert "real" in _collect_server_rows()


class TestLaunchSpecsFor:
    """handlers.mcp._launch_specs_for -- the batch-stub spec collection."""

    @pytest.mark.parametrize("kind", REFUSALS)
    def test_refused_spec_contributes_no_launch_specs(self, agents_dir, kind):
        _plant_refused(
            agents_dir,
            "bad.json",
            {"name": "bad", "mcpServers": {"srv": {"command": "x"}}},
            kind,
        )

        assert _launch_specs_for({"srv"}) == {}

    def test_valid_spec_still_yields_a_launch_spec(self, agents_dir):
        _plant(agents_dir, "good.json", {"name": "good", "mcpServers": {"srv": {"command": "x"}}})

        specs = _launch_specs_for({"srv"})

        assert "srv" in specs
        assert specs["srv"][0].command == "x"


class TestSensitiveSymlinkGuard:
    """One representative surface proves the symlink guard flows through.

    The guard's own matrix lives with ``_read_agent_spec``; this pins that a
    migrated caller actually consults it (same shape as #5423's test).
    """

    def test_link_to_a_sensitive_target_is_refused(self, tmp_path, monkeypatch):
        from kiro_crew import agent_discovery

        target = tmp_path / "protected.json"
        target.write_text(json.dumps({"name": "linked", "model": "leaked-value"}))
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "linked.json").symlink_to(target)
        monkeypatch.setattr(agent_discovery, "is_sensitive_path", lambda p: str(target) in str(p))
        monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", agents)

        out = _build_kiro_model_map()

        assert "linked" not in out


class TestDenialAttribution:
    """The SEL denial names the calling surface, not always ``list_agents`` (#6722).

    ``_read_agent_spec`` is the one reader for every surface, so its
    sensitive-target denial event used to hardcode ``operation``/``source`` to
    ``"list_agents"`` — a denial served for e.g. a chat restore was recorded in
    the security trail as an agent-listing cache warm. The refusal itself is
    unchanged; only the attribution is threaded through. Events are captured
    with a spy SEL (the shape ``test_agent_discovery.py`` already uses), never
    read from a real log file.
    """

    @staticmethod
    def _denial_events(tmp_path, monkeypatch):
        """A planted sensitive symlink plus a spy SEL capturing denial kwargs."""
        from types import SimpleNamespace

        from kiro_crew import agent_discovery

        target = tmp_path / "protected.json"
        target.write_text(json.dumps({"name": "linked"}))
        link = tmp_path / "linked.json"
        link.symlink_to(target)
        monkeypatch.setattr(agent_discovery, "is_sensitive_path", lambda p: str(target) in str(p))
        events: list[dict] = []
        monkeypatch.setattr(
            agent_discovery,
            "_sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: events.append(kw)),
        )
        return link, events

    def test_default_call_shape_emits_exactly_the_historical_event(self, tmp_path, monkeypatch):
        """An unlabelled call reproduces today's event byte-for-byte.

        This pins the backwards-compatible default rather than assuming it: an
        unmigrated (or future, forgotten) call site must keep writing the exact
        record the trail carried before #6722.
        """
        from kiro_crew.agent_discovery import _read_agent_spec

        link, events = self._denial_events(tmp_path, monkeypatch)

        assert _read_agent_spec(link) is None
        assert events == [
            {
                "caller": "agent_discovery",
                "operation": "list_agents",
                "outcome": "denied",
                "source": "list_agents",
                "resources": str(link.resolve()),
                "error": "sensitive path rejected",
            }
        ]

    def test_labelled_call_attributes_the_denial_to_that_surface(self, tmp_path, monkeypatch):
        """``operation`` names the surface and ``source`` the interface channel.

        Red before the fix: the event recorded ``list_agents`` regardless of
        who asked. The explicit ``source`` also guards against the parameter
        being dropped from the threaded call, where ``log_api_access``'s own
        default would silently relabel every denial ``"dashboard"``.
        """
        from kiro_crew.agent_discovery import _read_agent_spec

        link, events = self._denial_events(tmp_path, monkeypatch)

        assert _read_agent_spec(link, operation="doctor", source="cli") is None
        assert len(events) == 1
        assert events[0]["operation"] == "doctor"
        assert events[0]["source"] == "cli"
        assert events[0]["caller"] == "agent_discovery"
        assert events[0]["outcome"] == "denied"

    def test_source_defaults_independently_of_operation(self, tmp_path, monkeypatch):
        """The two defaults are independent — no echo semantics.

        Round 4 (First Principles): an ``operation``-given / ``source``-omitted
        call must NOT echo the operation into ``source`` (round 1 established
        the echo corrupts the interface-channel vocabulary); it keeps the
        historical byte-compat ``list_agents`` source instead. The ratchet
        forbids this call shape in src/, so this pins the reader's own
        semantics, not a shipped consumer.
        """
        from kiro_crew.agent_discovery import _read_agent_spec

        link, events = self._denial_events(tmp_path, monkeypatch)

        assert _read_agent_spec(link, operation="doctor") is None
        assert events[0]["operation"] == "doctor"
        assert events[0]["source"] == "list_agents"

    def test_migrated_surface_emits_its_own_label_end_to_end(self, tmp_path, monkeypatch):
        """A real surface (chat restore) writes its own label into the trail.

        Red before the fix: ``_build_kiro_model_map``'s denial recorded
        ``list_agents``. This is the per-surface guard the parametrised ratchet
        below cannot give — proof the keyword actually reaches SEL through a
        migrated caller, not just that the source text names it.
        """
        from types import SimpleNamespace

        from kiro_crew import agent_discovery

        target = tmp_path / "protected.json"
        target.write_text(json.dumps({"name": "linked", "model": "leaked"}))
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "linked.json").symlink_to(target)
        monkeypatch.setattr(agent_discovery, "is_sensitive_path", lambda p: str(target) in str(p))
        monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", agents)
        events: list[dict] = []
        monkeypatch.setattr(
            agent_discovery,
            "_sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: events.append(kw)),
        )

        out = _build_kiro_model_map()

        assert "linked" not in out
        assert [e["operation"] for e in events] == ["chat_persistence"]
        # ``source`` is the interface channel, NOT the operation echo (round 1
        # finding), and for THIS helper it is ``unknown``, not ``dashboard``
        # (round 2 finding): the Slack gateway restores evicted slots through
        # the same _rehydrate_slot_from_history path, so a pinned interface
        # would record a Slack/background restore as dashboard-originated.
        assert [e["source"] for e in events] == ["unknown"]


# Every call site and the (operation, source) pair it must carry. A new call
# site (or a reverted label) fails the ratchet below, so an unlabelled site
# cannot silently write ``list_agents`` denials for a surface that is not the
# agent listing, and no site can let ``source`` fall back to mirroring the
# operation (``source`` is the interface channel: dashboard/cli/startup/...,
# or ``"unknown"`` for shared helpers serving multiple channels). Per-file
# lists are SORTED (matching the scan; an unlabelled half, recorded as
# ``None``, sorts first) — the ratchet pins the multiset of pairs per file,
# not their source order.
_EXPECTED_CALL_SITE_LABELS: dict[str, list[tuple[str, str]]] = {
    "kiro_crew/agent_discovery.py": [
        ("list_agents", "unknown"),  # global-dir scan
        ("list_agents", "unknown"),  # project-files scan
        ("resolve_project_agent_name", "unknown"),  # single-spec probe
    ],
    "kiro_crew/config/loader.py": [("load_config", "unknown")],
    "kiro_crew/agent.py": [
        ("agent_spec_lookup", "unknown"),
        ("migrate_agent_specs", "unknown"),
    ],
    "kiro_crew/session.py": [("resolve_agent_model", "unknown")],
    "kiro_crew/cli_doctor.py": [("doctor", "cli"), ("doctor", "cli")],
    # ``unknown``, not ``dashboard``: the slot-rehydration path is shared —
    # the Slack gateway restores evicted slots through the same helper
    # (slack/gateway.py -> _rehydrate_slot_from_history), so pinning an
    # interface here would misattribute background/Slack restores (review
    # round 2 finding).
    "kiro_crew/dashboard/chat_persistence.py": [("chat_persistence", "unknown")],
    "kiro_crew/dashboard/handlers/agents.py": [
        ("api_agent_detail", "dashboard"),
        ("api_agents_sync", "dashboard"),
    ],
    "kiro_crew/dashboard/handlers/mcp.py": [
        ("api_mcp_active", "dashboard"),
        ("mcp_server_rows", "dashboard"),
        ("mcp_stub_eligibility", "dashboard"),
    ],
}


def _read_agent_spec_call_sites() -> dict[str, list[tuple[str | None, str | None]]]:
    """Every ``_read_agent_spec(...)`` call under src/, with its label pair.

    AST-based rather than a substring scan (the shape of the #4210 spawn-audit
    ratchet in ``test_spawn_audit.py``): it matches any call whose callee name
    is ``_read_agent_spec`` regardless of import alias or wrapping, and records
    the literal ``operation=``/``source=`` keywords, ``None`` for an omitted
    half.
    """
    import ast

    src = Path(__file__).resolve().parent.parent / "src"
    sites: dict[str, list[tuple[str | None, str | None]]] = {}
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute) else ""
            )
            if name != "_read_agent_spec":
                continue
            labels: dict[str, str | None] = {"operation": None, "source": None}
            for kw in node.keywords:
                if kw.arg in labels and isinstance(kw.value, ast.Constant):
                    labels[kw.arg] = kw.value.value
            sites.setdefault(path.relative_to(src).as_posix(), []).append(
                (labels["operation"], labels["source"])
            )
    # Keys are as_posix() so the table matches on Windows (relative_to gives
    # backslash paths there — caught by the Windows CI shard).
    # Per-file pair lists are SORTED (None first): ast.walk is breadth-first,
    # so raw order tracks nesting depth, not line numbers — an irrelevant
    # detail the ratchet must not be sensitive to.
    return {
        k: sorted(v, key=lambda p: tuple((x is not None, x or "") for x in p))
        for k, v in sites.items()
    }


class TestCallSiteLabelRatchet:
    """Static sweep: every call site is enumerated and explicitly labelled."""

    def test_every_call_site_carries_the_expected_label(self):
        sites = _read_agent_spec_call_sites()
        assert sites == _EXPECTED_CALL_SITE_LABELS, (
            "The _read_agent_spec call-site inventory moved. A NEW site must "
            "pass explicit operation= (naming its surface) and source= (its "
            "interface channel, or 'unknown' for a shared helper) and be added "
            "to the expected table — test_no_call_site_is_silently_unlabelled "
            "forbids omitting either. See #6722."
        )

    def test_no_call_site_is_silently_unlabelled(self):
        for path, pairs in _read_agent_spec_call_sites().items():
            for operation, source in pairs:
                assert operation is not None, (
                    f"{path} calls _read_agent_spec without an explicit "
                    "operation label; its sensitive-path denials would be "
                    "attributed to 'list_agents' (#6722)"
                )
                assert source is not None, (
                    f"{path} calls _read_agent_spec without an explicit "
                    "source; the byte-compat default would label the denial "
                    "'list_agents' instead of the interface channel "
                    "(dashboard/cli/...) — pass the channel, or 'unknown' "
                    "for a shared helper (#6722 review rounds 1-4)"
                )
