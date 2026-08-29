"""The shared warm mint: the specs one kiro-cli process serves, and the rows it holds.

The cold path (:mod:`kiro_crew.connections.mint`) pays a full kiro-cli spawn PER provider
for one approval URL. The warm design serves every card's URL from ONE process instead:
mode activation costs a FIXED ~5.18s whether the spec carries one remote server or six, so
a spec holding every mintable provider yields every ``oauth_request`` in a SINGLE
activation.

Two halves have landed. The TABLE: the row shape a shared mint uses (``shared`` /
``generation`` / ``activation`` on :class:`~kiro_crew.connections.mint.MintState`), the
liveness registry those stamps are read against (:class:`_WarmMintRuntime`), and the
withdrawal chokepoint the dashboard's status path calls (:func:`expire_dead_mints`). The
SPECS: the registry scan deciding which providers a warm process could serve
(:func:`warm_spec_providers`), the plan it would spawn on (:func:`_warm_spec_plan`), and
the spec files that plan writes (:func:`_write_warm_mint_specs`). Nothing here spawns,
activates, parks or kills a process.

Redeemability takes TWO questions and they die independently: the PKCE verifier lives in
the PROCESS (``generation_is_live``) while the loopback listener answering the redirect
belongs to the SESSION (``activation_is_live``). Process liveness alone passed a
terminated-session row, which is how a card kept serving an unredeemable URL. Both
failures are recorded in ``docs/architecture/design-notes/connections-warm-table.md``.

Two rules on the spec side are load-bearing and recorded in that same note: specs are
enumerated ONCE at spawn, so a plan that merely SHRANK must not force a respawn
(:func:`_plan_is_servable`), and the spec universe is registry-derived and BLIND to grant
and cancel state, because a plan tracking who needs a URL now retires a process holding
other cards' listeners.

OWNERSHIP: these specs carry FIXED, predictable names in the user's own agents directory,
so a name is where a spec of ours would GO and never proof that the file there is one.
Every spec written here is stamped with :data:`_WARM_SPEC_SENTINEL` on its description --
the stock defaults a spec body also fixes are values a user's own agent plausibly carries,
so the sentinel is what actually discriminates. Neither :func:`_write_warm_mint_specs` nor
:func:`_remove_warm_mint_specs` unlinks or overwrites a path whose contents this module did
not write -- see :func:`_warm_spec_is_foreign`.

INVARIANT: no coroutine here touches the filesystem directly. The spec helpers read the
user's config, the shared agents dir, or kiro-cli's OAuth cache -- any of which can sit on
a network mount where a stat is unbounded -- so they are synchronous, and a coroutine
reaches them through ``asyncio.to_thread``. Enforced by a fixed-point drift guard in
``test/test_connections_warm.py``, not merely described here.

DEFERRED to the lifecycle slice: everything that spawns, activates, parks or reaps a
process, and the ``warm_mint_all`` entry point that drives them. Until it lands nothing
sets ``shared`` on a row and nothing calls the spec planner, so :func:`expire_dead_mints`
is a no-op scan and the registry below stays empty. Both are written to answer correctly
the moment that slice starts filling them, parked generations included: a reader blind to
a parked process withdraws a code that process can still redeem, so completing the
predicate later would mean revisiting this decision under a live bug.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew import agent as _agent
from kiro_crew.agent_files import AGENT_FILENAME
from kiro_crew.config.loader import data_home
from kiro_crew.connections.mint import (
    _MINT_AGENT_PREFIX,
    _MINT_NAME_RE,
    MintState,
    _dispose_mint,
    _mint_spec_body,
    _mints,
    _mints_lock,
)
from kiro_crew.connections.registry import Provider, get_visible_providers
from kiro_crew.connections.tool_aliases import declared_tool_aliases, resolve_tool_aliases
from kiro_crew.mcp_discovery import list_servers
from kiro_crew.mcp_grant import grant_presence as grant_present
from kiro_crew.mcp_utils import (
    kiro_entry_client_id,
    kiro_entry_scopes,
    kiro_oauth_wire_entry,
    mcp_server_alias,
)
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: Warm specs are FIXED names under the cold mint's prefix, so one glob finds them all. They
#: carry no ``-<pid>-<8hex>`` suffix, which keeps them out of the cold engine's manifest sweep
#: -- and is the only thing telling a warm spec from a cold one whose server is literally named
#: ``warm-*`` (see ``_is_stale_warm_spec``). The character class MUST match the cold engine's: a
#: case only ONE pattern accepts reads as ours and gets its live spec unlinked.
_WARM_AGENT_PREFIX = f"{_MINT_AGENT_PREFIX}warm-"
_WARM_NAME_RE = re.compile(rf"^{re.escape(_WARM_AGENT_PREFIX)}[a-z0-9_.-]+$")
_WARM_BASE_AGENT = f"{_WARM_AGENT_PREFIX}base"
_WARM_ALL_AGENT = f"{_WARM_AGENT_PREFIX}all"
#: Prefixed onto the ``description`` of every spec this module writes, and the mark that makes
#: ownership provable. The other fields the writer fixes (``model``, ``includeMcpJson``,
#: ``prompt``, ``allowedTools``) are STOCK DEFAULTS a hand-written or scaffolded agent
#: plausibly carries, so on their own they judged a user's own spec at a warm path as ours.
#: ``description`` is the only schema-legal field free enough to carry a marker: kiro-cli
#: rejects an unknown spec key, and the agent-spec migration sweep strips bookkeeping keys.
_WARM_SPEC_SENTINEL = "Kiro Crew warm mint spec (machine-written; safe to delete)"


def _log_warm_event(operation: str, resources: str, outcome: str = "ok") -> None:
    """Record a warm-table event. Never carries a URL or an exception message."""
    sel().log_api_access(
        caller="dashboard",
        operation=operation,
        outcome=outcome,
        source="dashboard",
        resources=resources,
    )


def connections_tool_aliases(server_aliases: list[str]) -> dict[str, str]:
    """``toolAliases`` for a spec mounting ``server_aliases``, or ``{}``.

    kiro-cli exposes MCP tool names RAW, so two mounted servers exporting the same
    name leave only one reachable. The collision set is DECLARED by the registry and
    resolved by :func:`resolve_tool_aliases`, so it is known before consent -- the
    MCP inventory carries no tool list for a server that never authorized.

    KEY SHAPE. The resolver keys by registry SLUG (``@slug/tool``) while this spec mounts
    servers under ``mcp_server_alias(slug)``, and where the two differ kiro-cli applies no
    rename and the collision comes back silently -- so keys are re-pointed at the MOUNTED
    alias here. Every registry slug is slash-free today, making this an identity map that
    holds the shape contract of the spec we WRITE rather than fixing a reachable bug; the
    design note's "Tool-alias key shape" carries the full reasoning.
    """
    declared = declared_tool_aliases()
    wanted = set(server_aliases)
    mounted = {slug: alias for slug in declared if (alias := mcp_server_alias(slug)) in wanted}
    resolved = resolve_tool_aliases(
        {slug: set(tools) for slug, tools in declared.items() if slug in mounted}
    )
    aliased: dict[str, str] = {}
    for ref, alias in resolved.items():
        # rpartition, not partition: a registry slug may itself contain a slash while a tool
        # name never does, so the LAST separator reliably splits server from tool.
        slug, _, tool = ref.lstrip("@").rpartition("/")
        aliased[f"@{mounted.get(slug, slug)}/{tool}"] = alias
    return dict(sorted(aliased.items()))


def _warm_spec_description(detail: str) -> str:
    """Every description this module writes: the sentinel, then the caller's detail.

    Sentinel-FIRST rather than appended, so the judge tests a prefix. A suffix could be
    truncated by any writer that clips the field, and a prefix a user would have to type
    verbatim to impersonate.
    """
    detail = detail.strip()
    return f"{_WARM_SPEC_SENTINEL} {detail}" if detail else _WARM_SPEC_SENTINEL


def _warm_spec_body(name: str, servers: dict[str, Any], description: str) -> dict[str, Any]:
    """A mint spec body, sentinel-stamped, plus the ``toolAliases`` its mounted set needs.

    THE writer chokepoint: every spec this module puts on disk comes through here, which is
    what lets :func:`_warm_spec_is_foreign` treat the sentinel as present-or-not-ours.
    """
    body = _mint_spec_body(name, servers, _warm_spec_description(description))
    aliases = connections_tool_aliases(list(servers))
    if aliases:
        body["toolAliases"] = aliases
    return body


def _registry_server_entry(provider: Provider) -> dict[str, Any] | None:
    """The remote MCP entry the registry implies for ``provider``, in wire shape."""
    entry: dict[str, Any] = {"url": provider["mcp_url"]}
    scopes = provider.get("recommended_scopes") or []
    if scopes:
        entry["scopes"] = list(scopes)
    client_id = provider.get("client_id")
    if client_id:
        entry["clientId"] = client_id
    # store_entry=None: registry-derived, so no store owns it.
    return kiro_oauth_wire_entry(entry, store_entry=None, server=str(provider["slug"]))


def _disabled_provider_slugs() -> set[str]:
    """Registry slugs whose configured MCP entry the user turned OFF."""
    disabled = {server.name for server in list_servers() if server.disabled}
    return {
        provider["slug"]
        for provider in get_visible_providers()
        if provider["slug"] in disabled or mcp_server_alias(provider["slug"]) in disabled
    }


def warm_spec_providers() -> list[Provider]:
    """The spec UNIVERSE: every provider the shared process ENUMERATES at spawn."""
    disabled = _disabled_provider_slugs()
    return [
        provider
        for provider in get_visible_providers()
        if provider["slug"] not in disabled and _warm_mintable_entry(provider, None) is not None
    ]


def _warm_activation_candidates(universe: list[Provider]) -> list[Provider]:
    """The subset of ``universe`` an activation should actually ask a URL for."""
    return [provider for provider in universe if not grant_present(provider["mcp_url"])]


def _warm_candidate_scan() -> tuple[list[Provider], list[Provider]]:
    """``(spec universe, activation candidates)`` from one pass over the registry."""
    try:
        universe = warm_spec_providers()
    except Exception:  # noqa: BLE001 — reads user config; degrade to warming nothing
        logger.debug("warm mint inventory read failed", exc_info=True)
        return [], []
    return universe, _warm_activation_candidates(universe)


def mintable_providers() -> list[Provider]:
    """Providers an activation should warm right now, registry order."""
    return _warm_candidate_scan()[1]


def _auth_shape(entry: dict[str, Any]) -> tuple[str, tuple[str, ...], str]:
    """The fields of an MCP entry that decide what an authorization asks for."""
    return (
        str(entry.get("url") or ""),
        tuple(kiro_entry_scopes(entry)),
        kiro_entry_client_id(entry),
    )


def _warm_mintable_entry(
    provider: Provider, configured: dict[str, Any] | None
) -> dict[str, Any] | None:
    """The REGISTRY entry the warm process would activate, or None if it cannot.

    Registry-derived on purpose: a plan built from the user's config changed on every
    Connect click, respawning a process holding other cards' live listeners.

    None in two cases: no usable auth configuration (no DCR and no pre-registered public
    client id -- GitHub is the standing example), or a CONFIGURED entry asking for
    something different from the registry, which only the cold path can honour without
    handing back a grant the user did not ask for.
    """
    entry = _registry_server_entry(provider)
    if entry is None:
        return None
    expectations: dict[str, Any] = dict(provider.get("l0_expectations") or {})
    # Through the accessor: the wire shape nests the client id under ``oauth``, so a
    # bare ``clientId`` lookup reads every registered non-DCR provider as unregistered.
    if not bool(expectations.get("dcr")) and not kiro_entry_client_id(entry):
        return None
    if isinstance(configured, dict) and _auth_shape(configured) != _auth_shape(entry):
        return None
    return entry


@dataclass(frozen=True)
class _WarmSpecPlan:
    """Every agent spec the warm process needs, plus a digest of their contents."""

    all_agent: str
    per_provider: dict[str, str]
    specs: dict[str, dict[str, Any]]
    entries: dict[str, dict[str, Any]]
    digest: str


def _plan_is_servable(resident: _WarmSpecPlan, wanted: _WarmSpecPlan) -> bool:
    """True when the RUNNING process's specs can still serve ``wanted``.

    Digest equality is the wrong test alone: it reads a set that SHRANK as a set that
    changed. The only thing a respawn can fix is a server the process was never told
    about, so a plan whose every entry is already resident with an identical authorization
    ask is servable -- and replacing the process would strand its peers' listeners for
    nothing. A changed url/scopes/client id is genuine incompatibility: authorizing the
    resident ask would hand back the wrong grant.
    """
    if not resident.all_agent:
        return False
    return all(resident.entries.get(alias) == entry for alias, entry in wanted.entries.items())


def _warm_spec_plan(providers: list[Provider]) -> _WarmSpecPlan:
    """Build (but do not write) the warm process's spec set."""
    agents_dir = _agent.kiro_agents_dir_path()
    configured = _agent._load_json(agents_dir / AGENT_FILENAME).get("mcpServers") or {}
    entries: dict[str, dict[str, Any]] = {}
    per_provider: dict[str, str] = {}
    for provider in providers:
        alias = mcp_server_alias(provider["slug"])
        entry = _warm_mintable_entry(provider, configured.get(alias))
        if entry is None:
            continue
        entries[alias] = entry
        per_provider[provider["slug"]] = f"{_WARM_AGENT_PREFIX}{alias}"

    # The BASE spec carries zero servers on purpose: it is what the process spawns on, so
    # anything it declared would be initialized -- and challenged for -- before any mint.
    specs: dict[str, dict[str, Any]] = {
        _WARM_BASE_AGENT: _warm_spec_body(
            _WARM_BASE_AGENT, {}, "Zero-server base spec for the shared approval-URL mint."
        )
    }
    if entries:
        specs[_WARM_ALL_AGENT] = _warm_spec_body(
            _WARM_ALL_AGENT, entries, "Every mintable provider: one activation warms every card."
        )
    for slug, name in per_provider.items():
        alias = mcp_server_alias(slug)
        specs[name] = _warm_spec_body(
            name, {alias: entries[alias]}, f"Single-provider approval-URL mint for {alias}."
        )
    digest = hashlib.sha256(
        json.dumps(specs, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return _WarmSpecPlan(
        all_agent=_WARM_ALL_AGENT if entries else "",
        per_provider=per_provider,
        specs=specs,
        entries=entries,
        digest=digest,
    )


def _is_stale_warm_spec(stem: str, plan_names: frozenset[str]) -> bool:
    """Whether ``stem`` is a warm spec from a previous plan, safe to unlink.

    Three conjuncts, and the third is not redundant: a COLD mint spec for a server
    literally named ``warm-*`` shares this module's prefix
    (``kirocrew-mint-warm-foo-4821-9ab3c1de``), so prefix plus not-in-plan alone would
    delete a live cold mint's spec and strand a user mid-consent. Only the
    ``-<pid>-<8hex>`` suffix separates the two, over one shared character class.

    Necessary but NOT sufficient to unlink: the name says where a spec of ours would go,
    and :func:`_warm_spec_is_foreign` is what says the file there is one.
    """
    return (
        stem not in plan_names
        and _WARM_NAME_RE.match(stem) is not None
        and _MINT_NAME_RE.match(f"{stem}.json") is None
    )


def _warm_ownership_marks(name: str) -> dict[str, Any]:
    """The fields a warm spec named ``name`` carries no matter which plan wrote it.

    Read off the WRITER rather than restated, so a change to the spec body cannot leave
    this module unable to recognise its own files -- which would turn every rewrite into a
    refusal and hand the process a stale spec. ``mcpServers`` and ``tools`` vary with the
    plan, so neither can carry ownership.

    NECESSARY BUT NOT SUFFICIENT: every value here is a stock default, so a user's own spec
    plausibly matches all four. ``description`` is what discriminates -- not because its
    detail is fixed (it is not) but because its PREFIX is :data:`_WARM_SPEC_SENTINEL`, which
    :func:`_warm_spec_is_foreign` requires on top of these marks.
    """
    probe = _mint_spec_body(name, {}, "")
    return {key: probe[key] for key in ("model", "includeMcpJson", "prompt", "allowedTools")}


def _warm_spec_is_foreign(path: Path) -> bool:
    """True when ``path`` exists but no warm plan wrote it, so this module must not touch it.

    Warm spec names are FIXED and predictable and they sit in the user's OWN agents
    directory, so the name shape :func:`_is_stale_warm_spec` checks says where a spec of
    ours would GO -- never that the file already there is one. Ownership is proved from the
    contents instead, and it takes BOTH halves: the declared ``name`` matches the file with
    every field the writer fixes still holding, AND the description carries the sentinel.
    The marks alone are generic defaults, so a wholly user-authored spec that happens to
    carry them was read as ours and clobbered; the sentinel is the half that discriminates.

    Fails closed, because the two mistakes are not symmetric. Reading a file of ours as
    foreign costs one stale spec left as clutter; reading a user's hand-written agent as
    ours deletes it, or overwrites it with a spec they never asked for. So a file that is
    unreadable, not a JSON object, or shaped like anything but our own is foreign.
    """
    body = _agent._load_json(path)
    if not body:
        # ``_load_json`` answers {} for absent, unreadable and non-object alike, and only
        # the absent one is a path we may write -- which is what the presence check separates.
        return path.exists()
    marks = _warm_ownership_marks(path.stem)
    if body.get("name") != path.stem or any(body.get(key) != value for key, value in marks.items()):
        return True
    # No legacy exposure: no warm-spec writer has ever shipped, so no unsentinelled file of
    # ours exists anywhere to be orphaned by requiring this. Were one to exist it would read
    # as foreign, which means refused and left in place -- the safe direction.
    return not str(body.get("description") or "").startswith(_WARM_SPEC_SENTINEL)


def _write_warm_mint_specs(plan: _WarmSpecPlan) -> None:
    """Write the whole spec set, removing warm specs no longer in it.

    Every unlink and every write is gated on ownership, so a file this module did not write
    survives both. A refusal is audited and skipped, never raised: a provider whose spec
    path is occupied is a provider that goes unwarmed, not a failed spawn.
    """
    agents_dir = _agent.kiro_agents_dir_path()
    agents_dir.mkdir(parents=True, exist_ok=True)
    plan_names = frozenset(plan.specs)
    try:
        for path in agents_dir.glob(f"{_WARM_AGENT_PREFIX}*.json"):
            if not _is_stale_warm_spec(path.stem, plan_names):
                continue
            if _warm_spec_is_foreign(path):
                _log_warm_event("warm_mint_spec_sweep", path.name, outcome="refused")
                continue
            path.unlink(missing_ok=True)
    except OSError:
        logger.debug("warm mint spec sweep failed", exc_info=True)
    for name, spec in plan.specs.items():
        path = agents_dir / f"{name}.json"
        if _warm_spec_is_foreign(path):
            _log_warm_event("warm_mint_spec_write", path.name, outcome="refused")
            continue
        _agent._atomic_json_write(path, spec)


def _remove_warm_mint_specs() -> None:
    """Unlink every warm spec THIS module wrote. Called when the process is retired."""
    try:
        for path in _agent.kiro_agents_dir_path().glob(f"{_WARM_AGENT_PREFIX}*.json"):
            if not _is_stale_warm_spec(path.stem, frozenset()):
                continue
            if _warm_spec_is_foreign(path):
                _log_warm_event("warm_mint_spec_removal", path.name, outcome="refused")
                continue
            path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 — spec files; the write-time sweep catches leftovers
        logger.debug("warm mint spec removal failed", exc_info=True)


def _warm_work_dir() -> Path:
    """The shared process's working directory."""
    return data_home() / "connections" / "warm-mint"


def _runtime_alive(runtime: Any) -> bool:
    """Liveness of one warm process. Never raises into a mint."""
    if runtime is None:
        return False
    try:
        return bool(runtime.is_alive())
    except Exception:  # noqa: BLE001 — liveness must never raise into a mint
        logger.debug("warm mint liveness check failed", exc_info=True)
        return False


class _WarmMintRuntime:
    """The liveness registry a shared row's ``generation``/``activation`` are read against.

    Both containers are filled by the deferred lifecycle (slice N2b) and are empty until it
    lands. They are READ here rather than in N2b because the reader is what decides whether
    a card's URL is withdrawn, and the parked case is exactly the one where a wrong answer
    destroys a code the user could still redeem.
    """

    def __init__(self) -> None:
        self._runtime: Any = None
        #: Bumped on every spawn. Rows record the generation that minted them, letting a
        #: stand-down tell "nothing needs this" from "killing it strands a user mid-consent".
        self._generation = 0
        #: Generations kept alive ONLY because a card still holds one of their URLs.
        self._retiring: list[tuple[int, Any]] = []
        #: Live sessions by activation id -- each owns the loopback servers for its
        #: challenges, so one is held while a card points at one of its URLs.
        self._sessions: dict[int, Any] = {}

    def is_alive(self) -> bool:
        return _runtime_alive(self._runtime)

    def generation_is_live(self, generation: int) -> bool:
        """True while the process that minted ``generation`` can still redeem."""
        if generation <= 0:
            return False
        if generation == self._generation:
            return self.is_alive()
        return any(
            parked == generation and _runtime_alive(runtime) for parked, runtime in self._retiring
        )

    def activation_is_live(self, activation: int) -> bool:
        """True while the SESSION that minted ``activation`` still listens."""
        if activation <= 0:
            return False
        return activation in self._sessions


_warm_mint = _WarmMintRuntime()


def _warm_row_alive(entry: MintState) -> bool:
    """Whether a SHARED row's URL can still actually be redeemed.

    Two things must be alive and they die independently: the PKCE verifier in the PROCESS,
    and the loopback listener in the SESSION. Process liveness alone passed a
    terminated-session row, which is how a card kept serving an unredeemable URL -- which
    is also why the cold engine's ``_mint_holder_alive`` is deliberately NOT reused: it
    reads the row's own ``client``, which a shared row does not own.
    """
    if not _warm_mint.generation_is_live(int(entry.get("generation") or 0)):
        return False
    return _warm_mint.activation_is_live(int(entry.get("activation") or 0))


async def expire_dead_mints() -> list[str]:
    """Withdraw every shared row whose holding process is gone. THE chokepoint."""
    doomed: list[str] = []
    async with _mints_lock:
        for slug, entry in _mints.items():
            if not entry.get("shared") or entry.get("state") != "waiting":
                continue
            if _warm_row_alive(entry):
                continue
            entry["state"] = "expired"
            entry["reason"] = "mint_process_gone"
            await _dispose_mint(entry)
            doomed.append(slug)
    if doomed:
        logger.info("Withdrew %d approval URL(s) whose minting process is gone", len(doomed))
    return doomed
