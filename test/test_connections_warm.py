"""Warm-mint tests: the spec plan and its files, the row-liveness registry, the chokepoint."""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from typing import Any

import pytest
from test_connections_mint import _FS_ATTRS, _FS_NAMES, _called_names

from kiro_crew.connections import tool_aliases, warm
from kiro_crew.connections.mint import _mints
from kiro_crew.connections.registry import Provider


@pytest.fixture(autouse=True)
def _clean_mint_table():
    _mints.clear()
    yield
    _mints.clear()


class _Runtime:
    """A stand-in for one kiro-cli process, with the liveness answer we choose."""

    def __init__(self, alive: bool | BaseException) -> None:
        self._alive = alive

    def is_alive(self) -> bool:
        if isinstance(self._alive, BaseException):
            raise self._alive
        return self._alive


# ── redeemability takes TWO questions, and they die independently ──


def test_a_row_stamped_with_no_holder_at_all_is_never_alive():
    assert warm._warm_mint.generation_is_live(0) is False
    assert warm._warm_mint.activation_is_live(0) is False


def test_the_current_generation_is_live_exactly_while_its_process_is(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(warm._warm_mint, "_generation", 4)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(True))
    assert warm._warm_mint.generation_is_live(4) is True
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(False))
    assert warm._warm_mint.generation_is_live(4) is False


def test_a_parked_generation_stays_live_while_its_own_process_can_still_redeem(
    monkeypatch: pytest.MonkeyPatch,
):
    """A parked process still holds its peers' verifiers, so answering False for it would
    withdraw a URL the user can still redeem."""
    monkeypatch.setattr(warm._warm_mint, "_generation", 9)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [(3, _Runtime(True)), (4, _Runtime(False))])
    assert warm._warm_mint.generation_is_live(3) is True
    assert warm._warm_mint.generation_is_live(4) is False
    assert warm._warm_mint.generation_is_live(5) is False


def test_a_liveness_probe_that_raises_reads_as_dead_rather_than_failing_the_scan():
    """``expire_dead_mints`` runs on every status request, so a raising probe must not
    take the request down with it."""
    assert warm._runtime_alive(_Runtime(OSError("no such process"))) is False
    assert warm._runtime_alive(None) is False


def test_a_live_process_with_a_dead_session_does_not_keep_a_row_alive(
    monkeypatch: pytest.MonkeyPatch,
):
    """Process liveness alone passed a terminated-session row -- the observed failure that
    put the session question into the predicate at all."""
    monkeypatch.setattr(warm._warm_mint, "_generation", 2)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(True))
    row = {"state": "waiting", "shared": True, "generation": 2, "activation": 6}
    assert warm._warm_row_alive(row) is False  # type: ignore[arg-type]
    monkeypatch.setattr(warm._warm_mint, "_sessions", {6: object()})
    assert warm._warm_row_alive(row) is True  # type: ignore[arg-type]


# ── withdrawal is keyed on the FACT that the holder is gone ──


@pytest.mark.asyncio
async def test_a_row_whose_generation_is_gone_is_withdrawn():
    _mints["linear"] = {
        "state": "waiting",
        "oauth_url": "https://l/consent",
        "shared": True,
        "generation": 99,
        "activation": 1,
    }
    assert await warm.expire_dead_mints() == ["linear"]
    assert _mints["linear"]["state"] == "expired"
    assert _mints["linear"]["reason"] == "mint_process_gone"


@pytest.mark.asyncio
async def test_a_cold_row_is_left_to_the_cold_engine():
    """``_mint_holder_alive`` answers False for a shared row, so the warm chokepoint
    must judge only shared rows -- and leave a cold row's own verdict alone."""
    _mints["linear"] = {"state": "waiting", "oauth_url": "https://cold", "client": object()}
    assert await warm.expire_dead_mints() == []
    assert _mints["linear"]["state"] == "waiting"


@pytest.mark.asyncio
async def test_a_shared_row_not_yet_serving_a_url_is_left_alone():
    """Only a row actually SERVING a URL can be serving a dead one; a claim still minting
    is the activation's to fill or release."""
    _mints["linear"] = {"state": "minting", "shared": True, "generation": 99}
    assert await warm.expire_dead_mints() == []
    assert _mints["linear"]["state"] == "minting"


@pytest.mark.asyncio
async def test_a_row_whose_process_and_session_both_live_keeps_its_url(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(warm._warm_mint, "_generation", 5)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(True))
    monkeypatch.setattr(warm._warm_mint, "_sessions", {2: object()})
    _mints["linear"] = {
        "state": "waiting",
        "oauth_url": "https://l/consent",
        "shared": True,
        "generation": 5,
        "activation": 2,
    }
    assert await warm.expire_dead_mints() == []
    assert _mints["linear"]["state"] == "waiting"


def _provider(slug: str, url: str = "") -> Provider:
    return {  # type: ignore[typeddict-item]
        "slug": slug,
        "mcp_url": url or f"https://{slug}.example/mcp",
        "l0_expectations": {"dcr": True},
    }


# ── the loop/filesystem invariant (mirrors the mint engine's own guard) ──
#
# Reuses the mint guard's primitive sets so the two cannot drift apart, plus the names that
# reach the filesystem only from THIS module: the MCP inventory read and the grant stat.
_WARM_FS_NAMES = _FS_NAMES | {"list_servers", "grant_present"}


def test_no_coroutine_in_the_warm_module_touches_the_filesystem_directly():
    tree = ast.parse(inspect.getsource(warm))
    sync: dict[str, Any] = {}
    coros: dict[str, Any] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            sync[node.name] = node
        elif isinstance(node, ast.AsyncFunctionDef):
            coros[node.name] = node
    assert sync and coros, "module shape changed; this guard is reading the wrong tree"

    touches = {
        name: bool(_called_names(node) & (_FS_ATTRS | _WARM_FS_NAMES))
        for name, node in sync.items()
    }
    changed = True
    while changed:
        changed = False
        for name, node in sync.items():
            if touches[name]:
                continue
            if any(touches.get(callee) for callee in _called_names(node)):
                touches[name] = changed = True
    fs_helpers = {name for name, hit in touches.items() if hit}
    # The known set, so a helper silently losing its filesystem work -- and with it
    # this guard's coverage -- is visible rather than a quietly weaker test.
    assert fs_helpers == {
        "_log_warm_event",
        "_disabled_provider_slugs",
        "warm_spec_providers",
        "_warm_activation_candidates",
        "_warm_candidate_scan",
        "mintable_providers",
        "_warm_spec_plan",
        "_warm_spec_is_foreign",
        "_write_warm_mint_specs",
        "_remove_warm_mint_specs",
        "_warm_work_dir",
    }

    offenders = {
        f"{coro} -> {callee}"
        for coro, node in coros.items()
        for callee in _called_names(node) & (fs_helpers | _FS_ATTRS | _WARM_FS_NAMES)
    }
    assert not offenders, (
        "filesystem work on the event loop: "
        + ", ".join(sorted(offenders))
        + " -- route it through asyncio.to_thread"
    )


# ── defect: tool-alias key shape ──
#
# The resolver de-collides by registry SLUG and keys ``@slug/tool``, but a warm spec mounts
# servers under ``mcp_server_alias(slug)``. Where the two differ a slug-keyed entry names a
# server the spec never mounted, so kiro-cli applies no rename and the collision returns.


@pytest.fixture
def _slash_bearing_registry(monkeypatch: pytest.MonkeyPatch):
    """Two providers whose slugs contain a slash, so slug and mounted alias differ."""
    declared = {
        "ns/alpha": {"shared_tool": "alpha_shared_tool"},
        "ns/beta": {"shared_tool": "beta_shared_tool"},
    }
    monkeypatch.setattr(tool_aliases, "declared_tool_aliases", lambda: declared)
    monkeypatch.setattr(warm, "declared_tool_aliases", lambda: declared)
    return declared


def test_alias_keys_name_the_server_the_spec_actually_mounts(_slash_bearing_registry):
    """RED before the re-key: the emitted keys were ``@ns/alpha/...``, a server the
    spec -- which mounts ``ns-alpha`` -- never declared, so no rename applied."""
    aliases = warm.connections_tool_aliases(["ns-alpha", "ns-beta"])
    assert aliases == {
        "@ns-alpha/shared_tool": "alpha_shared_tool",
        "@ns-beta/shared_tool": "beta_shared_tool",
    }
    mounted = {"ns-alpha", "ns-beta"}
    assert {key.lstrip("@").rpartition("/")[0] for key in aliases} == mounted


def test_the_spec_a_warm_plan_writes_only_mounts_aliased_servers(_slash_bearing_registry):
    body = warm._warm_spec_body(
        "probe", {"ns-alpha": {"url": "https://a"}, "ns-beta": {"url": "https://b"}}, "probe"
    )
    assert set(body["toolAliases"]) <= {f"@{alias}/shared_tool" for alias in body["mcpServers"]}


# ── defect: alias semantics are #3260's, not the pre-#3260 first-server rule ──
#
# The draft asserted that the FIRST mounted server keeps the bare name and only later ones are
# renamed. #3260 shipped rename-EVERY-claimant, slug-keyed: when two mounted servers claim a
# tool, both are renamed and neither keeps the bare name.


def test_every_claimant_of_a_collision_is_renamed_not_just_the_later_one():
    aliases = warm.connections_tool_aliases(["linear", "vercel"])
    assert aliases == {
        "@linear/get_project": "linear_get_project",
        "@linear/list_projects": "linear_list_projects",
        "@linear/list_teams": "linear_list_teams",
        "@vercel/get_project": "vercel_get_project",
        "@vercel/list_projects": "vercel_list_projects",
        "@vercel/list_teams": "vercel_list_teams",
    }
    # Tools only one of the mounted pair declares keep their natural names.
    assert not any(key.endswith(("list_issues", "get_issue")) for key in aliases)


def test_a_single_mounted_provider_needs_no_aliases():
    assert warm.connections_tool_aliases(["linear"]) == {}
    assert warm.connections_tool_aliases(["vercel"]) == {}


def test_a_warm_spec_declares_tool_aliases_only_when_a_collision_is_mounted():
    single = warm._warm_spec_body("m", {"vercel": {"url": "https://v"}}, "probe")
    assert "toolAliases" not in single
    both = warm._warm_spec_body(
        "m", {"linear": {"url": "https://l"}, "vercel": {"url": "https://v"}}, "probe"
    )
    assert both["toolAliases"]["@linear/list_teams"] == "linear_list_teams"
    assert both["toolAliases"]["@vercel/list_teams"] == "vercel_list_teams"


# ── spec sweep: never unlink a live COLD mint's spec ──


def test_the_warm_sweep_refuses_a_cold_mint_spec_that_shares_the_prefix():
    """A cold spec for a server named ``warm-*`` matches the warm prefix. Deleting it
    would strand a user mid-consent, so the cold ``-<pid>-<8hex>`` shape is refused --
    including a MIXED-CASE alias, which only a shared character class catches."""
    for cold in ("kirocrew-mint-warm-foo-4821-9ab3c1de", "kirocrew-mint-warm-Foo-4821-9ab3c1de"):
        assert warm._is_stale_warm_spec(cold, frozenset()) is False


def test_the_warm_sweep_drops_a_warm_spec_absent_from_the_plan_and_keeps_the_rest():
    assert warm._is_stale_warm_spec("kirocrew-mint-warm-notion", frozenset()) is True
    assert (
        warm._is_stale_warm_spec(
            "kirocrew-mint-warm-notion", frozenset({"kirocrew-mint-warm-notion"})
        )
        is False
    )
    assert warm._is_stale_warm_spec("some-user-agent", frozenset()) is False


# ── defect: the sweep trusted a NAME, so it deleted and overwrote files it never wrote ──
#
# Warm spec names are FIXED and predictable (``kirocrew-mint-warm-<alias>``), and they live in
# the user's own agents directory alongside the agents they hand-write. Name shape alone made
# every such path fair game: a user's agent spec sitting at one was unlinked by the sweep and
# clobbered by the write. Ownership is now proved from the file's CONTENTS, and a path that
# cannot be proved ours is left exactly as it is -- audited and skipped, never raised.


@pytest.fixture
def _agents_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """An isolated agents directory, through the module's own override hook."""
    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(warm._agent, "KIRO_AGENTS_DIR", agents)
    monkeypatch.setattr(warm, "_log_warm_event", lambda *a, **k: None)
    return agents


def _foreign_spec(agents: Path, stem: str) -> tuple[Path, str]:
    """A user's OWN agent spec, planted at a path a warm plan would claim."""
    path = agents / f"{stem}.json"
    body = json.dumps(
        {
            "name": "my-own-research-agent",
            "description": "hand-written by the user",
            "prompt": "You are my research agent.",
            "mcpServers": {"private": {"command": "my-server"}},
            "allowedTools": ["@private"],
        },
        indent=2,
    )
    path.write_text(body, encoding="utf-8")
    return path, body


def test_the_sweep_refuses_to_unlink_a_foreign_file_at_a_warm_spec_path(_agents_dir: Path):
    """RED before the ownership check: ``_is_stale_warm_spec`` matched the NAME, so the
    write-time sweep unlinked a user's own agent spec that happened to sit there."""
    planted, body = _foreign_spec(_agents_dir, "kirocrew-mint-warm-notion")

    warm._write_warm_mint_specs(warm._warm_spec_plan([]))

    assert planted.is_file(), "the sweep deleted a file no warm plan ever wrote"
    assert planted.read_text(encoding="utf-8") == body


# ── defect: the ownership marks alone are GENERIC DEFAULTS, so a mimic passed as ours ──
#
# `model: "auto"`, `includeMcpJson: false`, `prompt: ""` and `allowedTools: []` are stock
# values any hand-written or scaffolded agent plausibly carries, so name-plus-marks judged a
# wholly user-authored spec at a warm path as ours and clobbered it. That falsified the claim
# that CONTENTS prove ownership. A sentinel prefix on the description is what discriminates.


def _mimic_spec(agents: Path, stem: str) -> tuple[Path, str]:
    """A user's own spec at a warm path that COPIES every generic default we fix.

    Everything the user actually authored -- description, mcpServers, tools -- is theirs;
    only the four stock marks and the name coincide with ours.
    """
    path = agents / f"{stem}.json"
    body = json.dumps(
        {
            "name": stem,
            "description": "my own scratch agent that happens to sit here",
            "model": "auto",
            "includeMcpJson": False,
            "prompt": "",
            "mcpServers": {"private": {"command": "my-server"}},
            "tools": ["@private"],
            "allowedTools": [],
        },
        indent=2,
    )
    path.write_text(body, encoding="utf-8")
    return path, body


def test_a_mimic_carrying_only_our_generic_defaults_survives_the_write(_agents_dir: Path):
    """RED before the sentinel: name plus four stock defaults read as ours, so the write
    clobbered a spec whose description, mcpServers and tools were entirely the user's."""
    planted, body = _mimic_spec(_agents_dir, warm._WARM_BASE_AGENT)

    warm._write_warm_mint_specs(warm._warm_spec_plan([]))

    assert planted.read_text(encoding="utf-8") == body, "the write clobbered a mimic"


def test_a_mimic_carrying_only_our_generic_defaults_survives_sweep_and_teardown(
    _agents_dir: Path,
):
    """RED before the sentinel: the same mimic at a path no plan wants was unlinked by both
    the write-time sweep and teardown."""
    planted, body = _mimic_spec(_agents_dir, "kirocrew-mint-warm-notion")

    warm._write_warm_mint_specs(warm._warm_spec_plan([]))
    assert planted.is_file(), "the sweep deleted a mimic"

    warm._remove_warm_mint_specs()

    assert planted.is_file(), "teardown deleted a mimic"
    assert planted.read_text(encoding="utf-8") == body


def test_the_judge_reads_a_mimic_as_foreign_and_our_own_writer_output_as_ours(
    _agents_dir: Path,
):
    """The predicate itself, so the discriminator is pinned independently of the callers."""
    mimic, _ = _mimic_spec(_agents_dir, "kirocrew-mint-warm-mimic")
    assert warm._warm_spec_is_foreign(mimic) is True

    ours = _agents_dir / "kirocrew-mint-warm-ours.json"
    ours.write_text(
        json.dumps(warm._warm_spec_body("kirocrew-mint-warm-ours", {}, "probe")),
        encoding="utf-8",
    )
    assert warm._warm_spec_is_foreign(ours) is False


def test_every_spec_a_warm_plan_writes_carries_the_ownership_sentinel(_agents_dir: Path):
    """Whole-plan coverage: base, all-providers and per-provider specs alike."""
    warm._write_warm_mint_specs(warm._warm_spec_plan([]))

    written = list(_agents_dir.glob(f"{warm._WARM_AGENT_PREFIX}*.json"))
    assert written
    for path in written:
        body = json.loads(path.read_text(encoding="utf-8"))
        assert body["description"].startswith(warm._WARM_SPEC_SENTINEL)
        assert warm._warm_spec_is_foreign(path) is False


def test_the_write_refuses_to_clobber_a_foreign_file_at_a_planned_spec_path(_agents_dir: Path):
    """RED before the ownership check: the write was unconditional, so a user's file at a
    path the CURRENT plan wants was overwritten rather than skipped."""
    planted, body = _foreign_spec(_agents_dir, warm._WARM_BASE_AGENT)

    warm._write_warm_mint_specs(warm._warm_spec_plan([]))

    assert planted.read_text(encoding="utf-8") == body, "the write clobbered a foreign file"


def test_the_teardown_refuses_to_unlink_a_foreign_file_at_a_warm_spec_path(_agents_dir: Path):
    """RED before the ownership check: teardown swept the whole warm glob by name."""
    planted, body = _foreign_spec(_agents_dir, "kirocrew-mint-warm-notion")

    warm._remove_warm_mint_specs()

    assert planted.is_file(), "teardown deleted a file no warm plan ever wrote"
    assert planted.read_text(encoding="utf-8") == body


def test_the_sweep_still_unlinks_a_stale_spec_a_warm_plan_did_write(_agents_dir: Path):
    """The refusal must not cost the sweep its job: our own leftovers still go."""
    stale = _agents_dir / "kirocrew-mint-warm-gone.json"
    stale.write_text(
        json.dumps(warm._warm_spec_body("kirocrew-mint-warm-gone", {}, "stale")),
        encoding="utf-8",
    )

    warm._write_warm_mint_specs(warm._warm_spec_plan([]))

    assert not stale.exists(), "a spec this module wrote survived the sweep"
    assert (_agents_dir / f"{warm._WARM_BASE_AGENT}.json").is_file()


def test_teardown_unlinks_every_spec_a_warm_plan_did_write(_agents_dir: Path):
    warm._write_warm_mint_specs(warm._warm_spec_plan([]))
    assert (_agents_dir / f"{warm._WARM_BASE_AGENT}.json").is_file()

    warm._remove_warm_mint_specs()

    assert not list(_agents_dir.glob(f"{warm._WARM_AGENT_PREFIX}*.json"))


def test_a_spec_this_module_wrote_is_rewritten_in_place(_agents_dir: Path):
    """Ownership must be recognized across a plan change, or the process gets a stale spec."""
    path = _agents_dir / f"{warm._WARM_BASE_AGENT}.json"
    path.write_text(
        json.dumps(warm._warm_spec_body(warm._WARM_BASE_AGENT, {}, "an older description")),
        encoding="utf-8",
    )

    warm._write_warm_mint_specs(warm._warm_spec_plan([]))

    description = json.loads(path.read_text(encoding="utf-8"))["description"]
    assert description.startswith(warm._WARM_SPEC_SENTINEL)
    assert "Zero-server" in description and "an older description" not in description


def test_an_unreadable_file_at_a_warm_spec_path_is_left_alone(_agents_dir: Path):
    """Fail closed: a path we cannot prove is ours is not ours. The cost is clutter."""
    path = _agents_dir / "kirocrew-mint-warm-notion.json"
    path.write_text("{ this is not json", encoding="utf-8")

    warm._write_warm_mint_specs(warm._warm_spec_plan([]))
    warm._remove_warm_mint_specs()

    assert path.read_text(encoding="utf-8") == "{ this is not json"


def test_a_refusal_is_audited_rather_than_raised(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The caller sees no exception, and the refusal is not silent either."""
    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(warm._agent, "KIRO_AGENTS_DIR", agents)
    events: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        warm,
        "_log_warm_event",
        lambda operation, resources, outcome="ok": events.append((operation, resources, outcome)),
    )
    _foreign_spec(agents, warm._WARM_BASE_AGENT)

    warm._write_warm_mint_specs(warm._warm_spec_plan([]))

    assert events, "a refusal to touch a user's file was silent"
    assert all(outcome == "refused" for _, _, outcome in events)
    # The audit names the spec, never the file's contents.
    assert all(warm._WARM_AGENT_PREFIX in resources for _, resources, _ in events)


# ── servability: a set that SHRANK is still servable ──


def _plan(entries: dict[str, dict[str, Any]]) -> warm._WarmSpecPlan:
    return warm._WarmSpecPlan(
        all_agent="all" if entries else "",
        per_provider={alias: f"spec-{alias}" for alias in entries},
        specs={},
        entries=entries,
        digest=repr(sorted(entries.items())),
    )


def test_a_shrunk_candidate_set_is_served_by_the_running_process():
    resident = _plan({"linear": {"url": "https://l"}, "vercel": {"url": "https://v"}})
    assert warm._plan_is_servable(resident, _plan({"linear": {"url": "https://l"}})) is True


def test_a_changed_authorization_ask_is_not_servable():
    resident = _plan({"linear": {"url": "https://l"}})
    assert warm._plan_is_servable(resident, _plan({"linear": {"url": "https://other"}})) is False
    assert warm._plan_is_servable(resident, _plan({"notion": {"url": "https://n"}})) is False


def test_a_process_that_enumerated_nothing_serves_nothing():
    assert warm._plan_is_servable(_plan({}), _plan({"linear": {"url": "https://l"}})) is False


# ── candidates: a granted provider is warmed into the spec but asked for no URL ──


def test_a_granted_provider_is_not_an_activation_candidate(monkeypatch: pytest.MonkeyPatch):
    universe = [_provider("granted"), _provider("fresh")]
    monkeypatch.setattr(warm, "grant_present", lambda url: "granted" in url)
    assert [p["slug"] for p in warm._warm_activation_candidates(universe)] == ["fresh"]


def test_an_unreadable_inventory_warms_nothing_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        warm, "warm_spec_providers", lambda: (_ for _ in ()).throw(OSError("config unreadable"))
    )
    assert warm._warm_candidate_scan() == ([], [])
