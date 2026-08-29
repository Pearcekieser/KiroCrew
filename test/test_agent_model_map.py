"""Tests for the shared name->model scan ``agent_discovery.agent_model_map``.

``agent_model_map`` is the ONE name->model reader that collapsed the three
former spellings (``_build_kiro_model_map``, ``SessionManager._resolve_agent_model``,
and the model field ``list_agents`` derives). It routes every read through the
hardened ``_read_agent_spec`` and coerces the model via ``spec_model``, so this
module mirrors ``test_agent_discovery.py``'s robustness/security coverage:

- macOS AppleDouble (``._*.json``) and non-UTF-8 files must not crash the scan.
- An oversized spec must be refused at the size cap, not slurped.
- Non-object JSON (a top-level array/scalar) must be skipped.
- A ``*.json`` symlink whose RESOLVED target is sensitive must NOT be read.

A refused spec must degrade exactly like an absent one (skip-and-continue), so
``_build_kiro_model_map`` still returns ``{}`` rather than raising when every
spec is refused. Tests use a tmp_path fake $HOME so the real filesystem is never
touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import requires_symlinks
from kiro_crew.agent_discovery import agent_model_map


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def _agents_dir(home: Path) -> Path:
    d = home / ".kiro" / "agents"
    d.mkdir(parents=True)
    return d


class TestAgentModelMap:
    """The happy-path mapping and the model coercion contract."""

    def test_maps_both_name_and_stem_to_the_model(self, fake_home):
        """A lookup by declared name OR file stem must resolve the same model,
        because the former scans both keyed the map that way and consumers look
        up by either spelling."""
        d = _agents_dir(fake_home)
        (d / "bot-file.json").write_text(
            json.dumps({"name": "declared-bot", "model": "claude-opus-4.8"}),
            encoding="utf-8",
        )

        result = agent_model_map(agents_dir=d)

        assert result["declared-bot"] == "claude-opus-4.8"
        assert result["bot-file"] == "claude-opus-4.8"

    def test_stem_only_when_name_absent_or_falsy(self, fake_home):
        """A spec with no truthy ``name`` still contributes its file stem — the
        fallback identity the former scans used."""
        d = _agents_dir(fake_home)
        (d / "nameless.json").write_text(json.dumps({"model": "m1"}), encoding="utf-8")
        (d / "blank.json").write_text(
            json.dumps({"name": "", "model": "m2"}), encoding="utf-8"
        )

        result = agent_model_map(agents_dir=d)

        assert result == {"nameless": "m1", "blank": "m2"}

    def test_non_string_model_folds_to_auto(self, fake_home):
        """A foreign spec's structured/null ``model`` must coerce to ``"auto"``
        via ``spec_model``, never leak a dict where the map annotates ``str``."""
        d = _agents_dir(fake_home)
        (d / "structured-file.json").write_text(
            json.dumps({"name": "structured", "model": {"id": "anthropic:claude"}}),
            encoding="utf-8",
        )
        (d / "nulled.json").write_text(
            json.dumps({"name": "nulled", "model": None}), encoding="utf-8"
        )

        result = agent_model_map(agents_dir=d)

        # coerced to "auto" under BOTH the declared name and the file stem
        assert result["structured"] == "auto"
        assert result["structured-file"] == "auto"
        assert result["nulled"] == "auto"

    def test_missing_agents_dir_returns_empty(self, fake_home):
        """A dir that does not exist (clean install) yields ``{}`` — no scan, no
        raise."""
        missing = fake_home / ".kiro" / "agents"  # never created
        assert agent_model_map(agents_dir=missing) == {}


class TestAgentModelMapRobustness:
    """The size-cap / AppleDouble / non-UTF-8 / non-object / symlink guards.

    These come from routing through ``_read_agent_spec``; a refused spec is
    skipped exactly like an absent one, so the map simply omits it.
    """

    def test_oversized_spec_is_skipped(self, fake_home, monkeypatch):
        """The agents dir is user-writable, so an oversized file must be refused
        at the cap instead of slurped. Uses a LOWERED cap (the property is "the
        cap is consulted", not its value), as test_agent_discovery/test_agent do."""
        from kiro_crew import hooks

        monkeypatch.setattr(hooks, "MAX_FILE_BYTES", 64)
        d = _agents_dir(fake_home)
        (d / "small.json").write_text(
            json.dumps({"name": "small", "model": "m"}), encoding="utf-8"
        )
        (d / "big.json").write_text(
            json.dumps({"name": "big", "model": "m", "pad": "x" * 1024}),
            encoding="utf-8",
        )

        result = agent_model_map(agents_dir=d)

        assert result == {"small": "m"}
        assert "big" not in result

    def test_appledouble_sidecar_is_skipped(self, fake_home):
        """A macOS ``._*.json`` sidecar is not a spec and must not be read."""
        d = _agents_dir(fake_home)
        (d / "real.json").write_text(
            json.dumps({"name": "real", "model": "m"}), encoding="utf-8"
        )
        (d / "._real.json").write_text(
            json.dumps({"name": "sidecar", "model": "leaked"}), encoding="utf-8"
        )

        result = agent_model_map(agents_dir=d)

        assert result == {"real": "m"}
        assert "sidecar" not in result

    def test_non_utf8_spec_is_skipped(self, fake_home):
        """Non-UTF-8 bytes must not crash the scan; the file is skipped."""
        d = _agents_dir(fake_home)
        (d / "good.json").write_text(
            json.dumps({"name": "good", "model": "m"}), encoding="utf-8"
        )
        (d / "binary.json").write_bytes(b"\xff\xfe\x00\x01not utf-8")

        result = agent_model_map(agents_dir=d)

        assert result == {"good": "m"}

    def test_non_object_json_is_skipped(self, fake_home):
        """A top-level array or scalar is not an agent spec and is rejected."""
        d = _agents_dir(fake_home)
        (d / "arr.json").write_text(
            json.dumps(["not", "an", "object"]), encoding="utf-8"
        )
        (d / "scalar.json").write_text(json.dumps("just-a-string"), encoding="utf-8")
        (d / "ok.json").write_text(
            json.dumps({"name": "ok", "model": "m"}), encoding="utf-8"
        )

        result = agent_model_map(agents_dir=d)

        assert result == {"ok": "m"}

    @requires_symlinks
    def test_symlink_to_sensitive_target_is_not_read(self, fake_home, monkeypatch):
        """``_read_agent_spec`` refuses a symlink whose RESOLVED target is
        sensitive (the documented ``evil.json -> ~/.aws/credentials`` case), so
        the map must NOT surface that target's model. Mirrors the cli_doctor
        symlink test's ``is_sensitive_path`` monkeypatch."""
        from kiro_crew import agent_discovery

        d = _agents_dir(fake_home)
        target = fake_home / "protected.json"
        target.write_text(json.dumps({"model": "leaked-value"}), encoding="utf-8")
        (d / "evil.json").symlink_to(target)
        monkeypatch.setattr(
            agent_discovery, "is_sensitive_path", lambda p: str(target) in str(p)
        )

        result = agent_model_map(agents_dir=d)

        # The refused spec contributes nothing under either key spelling.
        assert "leaked-value" not in result.values()
        assert "evil" not in result


class TestBuildKiroModelMapDegrade:
    """``_build_kiro_model_map`` must keep its degrade-to-empty contract: a
    refused spec is treated like an absent one, never a fatal path."""

    def test_all_specs_refused_yields_empty_map(self, fake_home, monkeypatch):
        """Every spec refused (here: oversized under a lowered cap) leaves the
        map empty rather than raising — skip-and-continue preserved."""
        from kiro_crew import hooks
        from kiro_crew.dashboard import chat_persistence

        monkeypatch.setattr(hooks, "MAX_FILE_BYTES", 8)
        d = _agents_dir(fake_home)
        (d / "toobig.json").write_text(
            json.dumps({"name": "toobig", "model": "m", "pad": "x" * 256}),
            encoding="utf-8",
        )
        # Point the shared reader's default dir resolution at the fake agents dir.
        monkeypatch.setattr(
            chat_persistence, "agent_model_map", lambda: agent_model_map(agents_dir=d)
        )

        assert chat_persistence._build_kiro_model_map() == {}

    def test_unexpected_failure_degrades_to_empty(self, monkeypatch):
        """The outer guard turns any unexpected raise into ``{}`` so callers see
        identical degrade semantics — no new fatal path."""
        from kiro_crew.dashboard import chat_persistence

        def _boom():
            raise RuntimeError("scan blew up")

        monkeypatch.setattr(chat_persistence, "agent_model_map", _boom)

        assert chat_persistence._build_kiro_model_map() == {}
