"""Login-posture rebuild withholds non-managed MCP."""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.context import reset_context, set_context
from kiro_crew.platform.defaults import DefaultAgentIdentityProvider
from kiro_crew.platform.governance import parse_policy


class _ForcedOnIdentity(DefaultAgentIdentityProvider):
    def enabled(self) -> bool:
        return True


def _ceiling(*, posture: str) -> Any:
    return parse_policy(
        {
            "version": 1,
            "boot": {"fail_closed": True},
            "capabilities": {"agentcore": {"enabled": True, "posture": posture}},
        }
    )


def _enable(posture: str, *, identity_on: bool = True) -> None:
    base = build_default_context(KiroCrewConfig())
    adapter = _ForcedOnIdentity() if identity_on else DefaultAgentIdentityProvider()
    set_context(
        dataclasses.replace(base, agent_identity=adapter, governance=_ceiling(posture=posture))
    )


def _seed_rebuild_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate kiro-cli home + agent dir; seed dummy servers. Never writes ~/.kiro."""
    kiro_dir = tmp_path / ".kiro" / "agents"
    kiro_dir.mkdir(parents=True)
    settings_dir = tmp_path / ".kiro" / "settings"
    settings_dir.mkdir(parents=True)
    (settings_dir / "mcp.json").write_text(
        json.dumps({"mcpServers": {"dummy-kiro-global": {"command": "dummy-srv"}}}),
        encoding="utf-8",
    )
    seam_global = tmp_path / "seam-global.json"
    seam_global.write_text(
        json.dumps({"mcpServers": {"dummy-seam-global": {"command": "dummy-srv"}}}),
        encoding="utf-8",
    )
    from kiro_crew.config import config_dir

    crew_mcp = config_dir() / "mcp.json"
    crew_mcp.write_text(
        json.dumps({"mcpServers": {"dummy-crew-store": {"command": "dummy-srv"}}}),
        encoding="utf-8",
    )
    leftover = kiro_dir / "kirocrew.json"
    leftover.write_text(
        json.dumps(
            {
                "name": "kirocrew",
                "mcpServers": {
                    "dummy-leftover": {"command": "dummy-srv"},
                    "dummy-kiro-global": {"command": "dummy-srv"},
                },
                "tools": ["@dummy-leftover"],
                "allowedTools": ["@dummy-leftover"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", kiro_dir)
    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", settings_dir / "mcp.json")
    monkeypatch.setattr("kiro_crew.agent._CC_MCP_JSON", tmp_path / "nonexistent_cc.json")
    monkeypatch.setattr("kiro_crew.agent._KIROCREW_BIN", sys.executable)
    monkeypatch.setattr("kiro_crew.agent._extra_mcp_scope_globals", lambda: [seam_global])
    monkeypatch.setattr("shutil.which", lambda cmd, path=None: sys.executable)
    return kiro_dir


def test_login_rebuild_withholds_kiro_global(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import rebuild_agent_config

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    try:
        _enable("login")
        rebuild_agent_config()
    finally:
        reset_context()

    servers = (
        json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8")).get("mcpServers") or {}
    )
    assert "kirocrew-core" in servers
    assert "dummy-kiro-global" not in servers
    assert "dummy-seam-global" not in servers
    assert "dummy-crew-store" not in servers
    # Leftover agent-file servers are omitted so kiro-cli cannot exec them
    # before inbound attach. Source mcp.json is not mutated.
    assert "dummy-leftover" not in servers
    tools = json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8")).get("tools") or []
    assert "@dummy-leftover" not in tools


def test_login_rebuild_withholds_without_companion_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import rebuild_agent_config

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    try:
        _enable("login", identity_on=False)
        rebuild_agent_config()
    finally:
        reset_context()

    servers = (
        json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8")).get("mcpServers") or {}
    )
    assert "dummy-kiro-global" not in servers
    assert "kirocrew-core" in servers


def test_login_rebuild_stashes_authored_mcp_and_restores_on_workload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, rebuild_agent_config
    from kiro_crew.config import config_dir

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    settings = tmp_path / ".kiro" / "settings" / "mcp.json"
    source_before = settings.read_text(encoding="utf-8")
    try:
        _enable("login")
        rebuild_agent_config()
        sidecar = json.loads((config_dir() / AUTHORED_MCP_SIDECAR).read_text(encoding="utf-8"))
        runtime = (
            json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8")).get("mcpServers")
            or {}
        )
        assert "dummy-leftover" not in runtime
        assert "dummy-leftover" in sidecar.get("mcpServers", {})
        assert "@dummy-leftover" in sidecar.get("tools", [])
        assert "@dummy-leftover" in sidecar.get("allowedTools", [])
        assert settings.read_text(encoding="utf-8") == source_before

        _enable("workload")
        rebuild_agent_config()
    finally:
        reset_context()

    restored = (
        json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8")).get("mcpServers") or {}
    )
    assert "dummy-leftover" in restored
    assert "dummy-kiro-global" in restored
    assert settings.read_text(encoding="utf-8") == source_before
    assert not (config_dir() / AUTHORED_MCP_SIDECAR).exists()


def test_clean_login_rebuild_discards_authored_mcp_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, rebuild_agent_config
    from kiro_crew.config import config_dir

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    try:
        _enable("login")
        rebuild_agent_config()
        assert (config_dir() / AUTHORED_MCP_SIDECAR).exists()
        rebuild_agent_config(clean=True)
        assert not (config_dir() / AUTHORED_MCP_SIDECAR).exists()
        _enable("workload")
        rebuild_agent_config()
    finally:
        reset_context()

    restored = (
        json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8")).get("mcpServers") or {}
    )
    assert "dummy-leftover" not in restored
    assert "dummy-kiro-global" in restored


def test_login_rebuild_does_not_overwrite_stash_with_empty_retract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, rebuild_agent_config
    from kiro_crew.config import config_dir

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    try:
        _enable("login")
        rebuild_agent_config()
        first = json.loads((config_dir() / AUTHORED_MCP_SIDECAR).read_text(encoding="utf-8"))
        rebuild_agent_config()
        second = json.loads((config_dir() / AUTHORED_MCP_SIDECAR).read_text(encoding="utf-8"))
    finally:
        reset_context()

    assert first.get("mcpServers", {}).get("dummy-leftover")
    assert second == first
    runtime = (
        json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8")).get("mcpServers") or {}
    )
    assert "dummy-leftover" not in runtime


def test_second_login_rebuild_keeps_qualified_authored_tool_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, rebuild_agent_config
    from kiro_crew.config import config_dir

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    leftover = kiro_dir / "kirocrew.json"
    spec = json.loads(leftover.read_text(encoding="utf-8"))
    spec["tools"] = ["@dummy-leftover/search"]
    spec["allowedTools"] = ["@dummy-leftover/search"]
    leftover.write_text(json.dumps(spec), encoding="utf-8")
    try:
        _enable("login")
        rebuild_agent_config()
        first = json.loads((config_dir() / AUTHORED_MCP_SIDECAR).read_text(encoding="utf-8"))
        leftover.write_text(
            json.dumps({"name": "kirocrew", "mcpServers": {}, "tools": []}),
            encoding="utf-8",
        )
        rebuild_agent_config()
        second = json.loads((config_dir() / AUTHORED_MCP_SIDECAR).read_text(encoding="utf-8"))
    finally:
        reset_context()

    assert first.get("mcpServers", {}).get("dummy-leftover")
    assert "@dummy-leftover/search" in first.get("tools", [])
    assert "@dummy-leftover/search" in first.get("allowedTools", [])
    assert "@dummy-leftover/search" in second.get("tools", [])
    assert "@dummy-leftover/search" in second.get("allowedTools", [])
    assert "dummy-leftover" in second.get("mcpServers", {})


def test_merge_keeps_qualified_tool_ref_for_kept_server() -> None:
    from kiro_crew.agent import _merge_authored_mcp_payload

    existing = {
        "mcpServers": {"gateway": {"command": "old"}},
        "tools": ["@gateway/toolA", "@gone/toolB"],
        "allowedTools": ["@gateway/toolA"],
        "sourceServers": ["gateway", "gone"],
    }
    incoming = {
        "mcpServers": {"gateway": {"command": "new"}},
        "tools": ["@gateway/toolA"],
        "allowedTools": ["@gateway/toolA"],
        "sourceServers": ["gateway"],
    }
    merged = _merge_authored_mcp_payload(existing, incoming)
    assert merged["mcpServers"]["gateway"]["command"] == "new"
    assert "@gateway/toolA" in merged["tools"]
    assert "@gateway/toolA" in merged["allowedTools"]
    assert "@gone/toolB" not in merged["tools"]
    assert "gone" not in merged["mcpServers"]


def test_extract_does_not_stash_qualified_managed_ref() -> None:
    from kiro_crew.agent import _extract_non_managed_mcp

    config: dict[str, Any] = {
        "mcpServers": {"kirocrew-core": {}, "dummy": {}},
        "tools": ["@kirocrew-core/search", "@dummy/x"],
        "allowedTools": ["@kirocrew-core/search", "@dummy/x"],
    }
    extracted = _extract_non_managed_mcp(config, {"kirocrew-core"})
    assert "dummy" in extracted["mcpServers"]
    assert "kirocrew-core" not in extracted["mcpServers"]
    assert extracted["tools"] == ["@dummy/x"]
    assert extracted["allowedTools"] == ["@dummy/x"]


def test_retract_keeps_qualified_ref_for_managed_server() -> None:
    from kiro_crew.agent import _retract_non_managed_mcp

    config: dict[str, Any] = {
        "mcpServers": {"kirocrew-core": {}, "dummy": {}},
        "tools": ["@kirocrew-core/search", "@dummy/x"],
        "allowedTools": ["@kirocrew-core/search", "@dummy/x"],
    }
    _retract_non_managed_mcp(config, {"kirocrew-core"})
    assert config["tools"] == ["@kirocrew-core/search"]
    assert config["allowedTools"] == ["@kirocrew-core/search"]
    assert "dummy" not in config["mcpServers"]


def test_login_rebuild_drops_deleted_source_from_stash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, rebuild_agent_config
    from kiro_crew.config import config_dir

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    settings = tmp_path / ".kiro" / "settings" / "mcp.json"
    try:
        _enable("login")
        rebuild_agent_config()
        first = json.loads((config_dir() / AUTHORED_MCP_SIDECAR).read_text(encoding="utf-8"))
        assert "dummy-kiro-global" in first.get("mcpServers", {})
        settings.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        leftover = kiro_dir / "kirocrew.json"
        leftover.write_text(
            json.dumps({"name": "kirocrew", "mcpServers": {}, "tools": []}),
            encoding="utf-8",
        )
        rebuild_agent_config()
        second = json.loads((config_dir() / AUTHORED_MCP_SIDECAR).read_text(encoding="utf-8"))
    finally:
        reset_context()

    assert "dummy-kiro-global" not in second.get("mcpServers", {})
    assert "dummy-leftover" in second.get("mcpServers", {})
    assert "@dummy-leftover" in second.get("allowedTools", [])


def test_restore_does_not_overwrite_source_edited_during_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import rebuild_agent_config

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    settings = tmp_path / ".kiro" / "settings" / "mcp.json"
    try:
        _enable("login")
        rebuild_agent_config()
        settings.write_text(
            json.dumps(
                {"mcpServers": {"dummy-kiro-global": {"command": "dummy-srv", "timeout": 42}}}
            ),
            encoding="utf-8",
        )
        _enable("workload")
        rebuild_agent_config()
    finally:
        reset_context()

    restored = (
        json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8")).get("mcpServers") or {}
    )
    assert restored["dummy-kiro-global"]["timeout"] == 42
    assert "dummy-leftover" in restored


def test_restore_keeps_sidecar_until_runtime_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, _restore_authored_mcp, rebuild_agent_config
    from kiro_crew.config import config_dir

    _seed_rebuild_sources(tmp_path, monkeypatch)
    try:
        _enable("login")
        rebuild_agent_config()
        sidecar = config_dir() / AUTHORED_MCP_SIDECAR
        assert sidecar.exists()
        _restore_authored_mcp({"mcpServers": {}})
        assert sidecar.exists()
    finally:
        reset_context()


def test_workload_rebuild_still_merges_kiro_global(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import rebuild_agent_config

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    try:
        _enable("workload")
        rebuild_agent_config()
    finally:
        reset_context()

    servers = (
        json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8")).get("mcpServers") or {}
    )
    assert "dummy-kiro-global" in servers
    assert "kirocrew-core" in servers


def test_login_probe_succeeds_iam_invoke_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import rebuild_agent_config
    from kiro_crew.cloud import iam
    from kiro_crew.sel import sel

    kiro_dir = _seed_rebuild_sources(tmp_path, monkeypatch)
    monkeypatch.setattr(iam, "probe_instance_invoke_gateway", lambda: True)
    try:
        _enable("login")
        rebuild_agent_config()
        events = sel().recent(limit=50)
    finally:
        reset_context()

    servers = (
        json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8")).get("mcpServers") or {}
    )
    mismatch = [e for e in events if e.get("operation") == "agentcore.posture_mismatch"]
    assert mismatch, f"expected SEL agentcore.posture_mismatch row in {events!r}"
    assert mismatch[0].get("outcome") == "denied"
    assert not any("gateway" in name.lower() for name in servers)


def test_source_mcp_names_include_enabled_app_and_edition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import agent as agent_mod

    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", tmp_path / "missing-kiro.json")
    monkeypatch.setattr(agent_mod, "_collect_app_mcp_servers", lambda: {"notes:tools": {}})
    monkeypatch.setattr(agent_mod, "_extra_mcp_servers", lambda: {"edition-internal": {}})
    names = agent_mod._source_mcp_server_names()
    assert "notes:tools" in names
    assert "edition-internal" in names


def test_source_mcp_names_store_normalized_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slash source keys must match the alias emitted into the runtime stash."""
    from kiro_crew import agent as agent_mod

    src = tmp_path / "kiro-mcp.json"
    src.write_text(
        json.dumps({"mcpServers": {"namespace/name": {"command": "x"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", src)
    monkeypatch.setattr(agent_mod, "_collect_app_mcp_servers", lambda: {})
    monkeypatch.setattr(agent_mod, "_extra_mcp_servers", lambda: {})
    names = agent_mod._source_mcp_server_names()
    assert "namespace/name" not in names
    assert "namespace-name" in names


def test_empty_reconciliation_unlinks_authored_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, _stash_authored_mcp
    from kiro_crew.config import config_dir

    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", tmp_path / "missing-kiro.json")
    monkeypatch.setattr("kiro_crew.agent._collect_app_mcp_servers", lambda: {})
    monkeypatch.setattr("kiro_crew.agent._extra_mcp_servers", lambda: {})
    sidecar = config_dir() / AUTHORED_MCP_SIDECAR
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps(
            {
                "mcpServers": {"gone": {"command": "x"}},
                "sourceServers": ["gone"],
            }
        ),
        encoding="utf-8",
    )
    _stash_authored_mcp({"mcpServers": {}, "tools": [], "allowedTools": []}, set())
    assert sidecar.exists() is False


def test_restore_drops_disabled_app_mcp_from_stash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, _restore_authored_mcp
    from kiro_crew.config import config_dir

    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", tmp_path / "missing-kiro.json")
    monkeypatch.setattr("kiro_crew.agent._collect_app_mcp_servers", lambda: {})
    monkeypatch.setattr("kiro_crew.agent._extra_mcp_servers", lambda: {})
    sidecar = config_dir() / AUTHORED_MCP_SIDECAR
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps(
            {
                "mcpServers": {"notes:tools": {"command": "notes-mcp"}},
                "sourceServers": ["notes:tools"],
            }
        ),
        encoding="utf-8",
    )
    config: dict[str, Any] = {"mcpServers": {}}
    assert _restore_authored_mcp(config) is True
    assert "notes:tools" not in config["mcpServers"]


def test_restore_keeps_live_definition_when_name_already_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, _restore_authored_mcp
    from kiro_crew.config import config_dir

    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", tmp_path / "kiro-mcp.json")
    (tmp_path / "kiro-mcp.json").write_text(
        json.dumps({"mcpServers": {"custom": {"command": "updated"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr("kiro_crew.agent._collect_app_mcp_servers", lambda: {})
    monkeypatch.setattr("kiro_crew.agent._extra_mcp_servers", lambda: {})
    sidecar = config_dir() / AUTHORED_MCP_SIDECAR
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "custom": {
                        "command": "stale",
                        "env": {"TOKEN": "keep"},
                        "args": ["--extra"],
                    }
                },
                "sourceServers": ["custom"],
            }
        ),
        encoding="utf-8",
    )
    config: dict[str, Any] = {"mcpServers": {"custom": {"command": "updated"}}}
    assert _restore_authored_mcp(config) is True
    assert config["mcpServers"]["custom"] == {"command": "updated"}


def test_restore_drops_qualified_ref_when_source_server_vanished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent import AUTHORED_MCP_SIDECAR, _restore_authored_mcp
    from kiro_crew.config import config_dir

    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", tmp_path / "missing-kiro.json")
    monkeypatch.setattr("kiro_crew.agent._collect_app_mcp_servers", lambda: {})
    monkeypatch.setattr("kiro_crew.agent._extra_mcp_servers", lambda: {})
    sidecar = config_dir() / AUTHORED_MCP_SIDECAR
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps(
            {
                "mcpServers": {"notes:tools": {"command": "notes-mcp"}},
                "sourceServers": ["notes:tools"],
                "tools": ["@notes:tools/search"],
                "allowedTools": ["@notes:tools/search"],
            }
        ),
        encoding="utf-8",
    )
    config: dict[str, Any] = {"mcpServers": {}, "tools": [], "allowedTools": []}
    assert _restore_authored_mcp(config) is True
    assert "notes:tools" not in config["mcpServers"]
    assert "@notes:tools/search" not in config["tools"]
    assert "@notes:tools/search" not in config["allowedTools"]


def test_authored_mcp_directory_fences_atomic_write_temp() -> None:
    """A file-leaf classification would leave mkstemp siblings agent-writable."""
    from kiro_crew.security import is_sensitive_path

    assert is_sensitive_path("~/.kiro/crew/agentcore-authored-mcp/stash.json")
    assert is_sensitive_path("~/.kiro/crew/agentcore-authored-mcp/.stash.json.tmp")
    assert is_sensitive_path("~/.kirocrew/agentcore-authored-mcp/tmpXXXX")


def test_unlink_authored_mcp_sidecar_propagates_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A locked sidecar must fail the rebuild, not report success and restore later."""
    from kiro_crew import agent as agent_mod

    class _Locked:
        def unlink(self) -> None:
            raise OSError(16, "Device or resource busy")

    monkeypatch.setattr(agent_mod, "_authored_mcp_path", lambda: _Locked())
    with pytest.raises(OSError):
        agent_mod._unlink_authored_mcp_sidecar()


def test_unlink_authored_mcp_sidecar_ignores_missing_file() -> None:
    from kiro_crew.agent import _unlink_authored_mcp_sidecar

    _unlink_authored_mcp_sidecar()
