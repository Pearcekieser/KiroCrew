"""The dashboard agent-spec scans (``_namespaced_agent_file_exists`` in
handlers/agents.py; ``_collect_server_rows`` and ``_launch_specs_for`` in
handlers/mcp.py) read the user-writable ``~/.kiro/agents/*.json`` directory,
which is shared with other tools. Each now reads through the one hardened reader
(``agent_discovery._read_agent_spec``), so an oversized, non-UTF-8/AppleDouble,
non-object or sensitive-symlink spec is skipped exactly like an absent file
rather than raising.

These focused tests pin that skip-and-continue behaviour at each site. They call
the sync helpers directly and redirect the agents dir with the documented
``KIRO_AGENTS_DIR`` override hook so the real filesystem is never touched.
"""

from __future__ import annotations

import json

import pytest

import kiro_crew.agent as agent_mod
from conftest import requires_symlinks
from kiro_crew.dashboard.handlers.agents import _namespaced_agent_file_exists
from kiro_crew.dashboard.handlers.mcp import _collect_server_rows, _launch_specs_for


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    """Point every ``kiro_agents_dir_path()`` caller at a tmp dir.

    The handlers import ``kiro_agents_dir_path`` from ``kiro_crew.agent`` and
    call it per request, and it returns ``KIRO_AGENTS_DIR`` when set, so patching
    the module global redirects all three sites without a real data home.
    """
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", d)
    return d


class TestNamespacedAgentFileExists:
    def test_true_for_a_well_formed_namespaced_file(self, agents_dir):
        """Control: a namespaced ``<app>--<agent>.json`` whose ``name`` matches
        is still reported present."""
        (agents_dir / "myapp--helper.json").write_text(
            json.dumps({"name": "helper"}), encoding="utf-8"
        )
        assert _namespaced_agent_file_exists("helper") is True

    def test_oversized_file_returns_false_not_raise(self, agents_dir, monkeypatch):
        """An oversized namespaced file is refused at the read cap and treated as
        absent -- False, not a raised FileTooLargeError."""
        import kiro_crew.hooks as hooks_mod

        monkeypatch.setattr(hooks_mod, "MAX_FILE_BYTES", 64)
        (agents_dir / "myapp--helper.json").write_text(
            json.dumps({"name": "helper", "pad": "x" * 512}), encoding="utf-8"
        )
        assert _namespaced_agent_file_exists("helper") is False

    def test_non_utf8_file_returns_false_not_raise(self, agents_dir):
        """A non-UTF-8 namespaced file is skipped, not a UnicodeDecodeError."""
        (agents_dir / "myapp--helper.json").write_bytes(b"\xff\xfe\x00\x01\xa3")
        assert _namespaced_agent_file_exists("helper") is False

    @requires_symlinks
    def test_symlink_to_sensitive_is_not_read(self, agents_dir, tmp_path, monkeypatch):
        """A namespaced symlink whose resolved target is sensitive must not be
        read: the reader refuses it, so the app agent reads as absent (False)
        even though the target would parse as a matching spec."""
        from kiro_crew import agent_discovery

        target = tmp_path / "protected.json"
        target.write_text(json.dumps({"name": "helper"}), encoding="utf-8")
        (agents_dir / "myapp--helper.json").symlink_to(target)
        monkeypatch.setattr(
            agent_discovery, "is_sensitive_path", lambda p: str(target) in str(p)
        )
        assert _namespaced_agent_file_exists("helper") is False


class TestCollectServerRows:
    def test_skips_oversized_and_non_object(self, agents_dir, monkeypatch):
        """An oversized or non-object spec is skipped, not fatal; a clean sibling
        still contributes its declared servers."""
        import kiro_crew.hooks as hooks_mod

        monkeypatch.setattr(hooks_mod, "MAX_FILE_BYTES", 128)
        # Non-object JSON (top-level array) -- previously guarded by isinstance,
        # now folded into the reader's None return.
        (agents_dir / "array.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        # Oversized spec -- refused at the read cap.
        (agents_dir / "big.json").write_text(
            json.dumps(
                {"name": "big", "mcpServers": {"s": {"command": "x"}}, "pad": "y" * 512}
            ),
            encoding="utf-8",
        )
        # Clean spec whose server must survive.
        (agents_dir / "ok.json").write_text(
            json.dumps({"name": "ok", "mcpServers": {"good": {"command": "run"}}}),
            encoding="utf-8",
        )

        rows = _collect_server_rows()

        assert "good" in rows
        assert "s" not in rows


class TestLaunchSpecsFor:
    def test_skips_oversized_and_non_object(self, agents_dir, monkeypatch):
        """An oversized/non-object spec is skipped instead of raising; the
        requested server from a clean spec is still collected."""
        import kiro_crew.hooks as hooks_mod

        monkeypatch.setattr(hooks_mod, "MAX_FILE_BYTES", 128)
        (agents_dir / "scalar.json").write_text(json.dumps("nope"), encoding="utf-8")
        (agents_dir / "big.json").write_text(
            json.dumps(
                {
                    "name": "big",
                    "mcpServers": {"good": {"command": "x"}},
                    "pad": "y" * 512,
                }
            ),
            encoding="utf-8",
        )
        (agents_dir / "ok.json").write_text(
            json.dumps({"name": "ok", "mcpServers": {"good": {"command": "run"}}}),
            encoding="utf-8",
        )

        specs = _launch_specs_for({"good"})

        # Only the clean spec's launch definition is present -- the oversized one
        # was refused, not fatal.
        assert "good" in specs
        assert len(specs["good"]) == 1
        assert specs["good"][0].command == "run"
