"""``migrate_agent_specs`` reads AND rewrites each spec, so it is the one site
whose migration onto the hardened reader must ALSO keep the stricter
no-symlink-at-all fence: the copy-out hazard ``_spec_path_is_safe`` documents
(following a symlink reads the target and writes a modified copy into the freely
readable agents directory) is broader than the reader's refusal of a symlink
whose RESOLVED target is sensitive.

These tests assert both halves of that:
- an oversized spec is skipped at the read cap, not slurped or fatal;
- a symlink is refused BEFORE it is read or rewritten -- the target is left
  byte-for-byte unchanged and the cleaned count excludes it.

Tests use a tmp_path fake ``$HOME`` and the ``KIRO_AGENTS_DIR`` override hook so
the real filesystem is never touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import kiro_crew.agent as agent_mod
from conftest import requires_symlinks


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    """Redirect the agents dir onto a tmp path via the documented override hook.

    ``kiro_agents_dir_path()`` returns ``KIRO_AGENTS_DIR`` when set, so patching
    the module global points every caller (including ``migrate_agent_specs``) at
    this directory without touching the real data home.
    """
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", d)
    return d


def test_clean_spec_is_rewritten_and_counted(agents_dir):
    """Control: a normal spec carrying a bookkeeping key is still cleaned.

    Confirms the migration did not break the happy path -- the key is lifted out
    and the count reflects the rewrite.
    """
    (agents_dir / "a.json").write_text(
        json.dumps({"name": "a", "model": "auto", "model_managed": True}),
        encoding="utf-8",
    )

    assert agent_mod.migrate_agent_specs() == 1

    data = json.loads((agents_dir / "a.json").read_text(encoding="utf-8"))
    assert "model_managed" not in data
    assert data["name"] == "a"


def test_oversized_spec_is_skipped_not_fatal(agents_dir, monkeypatch):
    """A spec over the safety cap is refused by the hardened reader (returns
    None) and skipped exactly like an absent file -- not slurped whole and not a
    new fatal path. A clean sibling is still cleaned in the same pass."""
    import kiro_crew.hooks as hooks_mod

    monkeypatch.setattr(hooks_mod, "MAX_FILE_BYTES", 64)
    # Well under the cap, and carries a bookkeeping key so it is a rewrite target.
    (agents_dir / "small.json").write_text(
        json.dumps({"name": "small", "model_managed": True}),
        encoding="utf-8",
    )
    # Over the cap AND carries a bookkeeping key; the read must refuse it before
    # the rewrite, so it does NOT count and its bytes are left untouched.
    big_text = json.dumps({"name": "big", "cc_model": "x", "pad": "y" * 512})
    (agents_dir / "big.json").write_text(big_text, encoding="utf-8")

    assert agent_mod.migrate_agent_specs() == 1

    # The oversized file was never rewritten: still carries its bookkeeping key.
    assert (agents_dir / "big.json").read_text(encoding="utf-8") == big_text
    small = json.loads((agents_dir / "small.json").read_text(encoding="utf-8"))
    assert "model_managed" not in small


@requires_symlinks
def test_refuses_to_rewrite_through_a_symlink(agents_dir, tmp_path):
    """A ``*.json`` symlink is refused BEFORE being read or rewritten.

    This is the copy-out case: following the link would read the target and
    write a modified copy into the agents directory, laundering a file the
    reader may not otherwise be allowed to open. The ``_spec_path_is_safe`` fence
    refuses ANY symlink here, so the target must be left byte-for-byte unchanged
    and excluded from the cleaned count.
    """
    # A real file OUTSIDE the agents dir that declares a bookkeeping key. If the
    # link were followed, migrate would strip the key and rewrite it (as a copy
    # inside the agents dir); we assert it is untouched instead.
    target = tmp_path / "outside.json"
    target_text = json.dumps({"name": "victim", "model_managed": True})
    target.write_text(target_text, encoding="utf-8")
    (agents_dir / "link.json").symlink_to(target)

    # A clean in-dir spec so the pass has real work to do and a non-zero count
    # would be attributable to the symlink if the fence failed.
    (agents_dir / "real.json").write_text(
        json.dumps({"name": "real", "cc_model": "z"}),
        encoding="utf-8",
    )

    assert agent_mod.migrate_agent_specs() == 1

    # The symlink target is byte-for-byte unchanged: never read, never rewritten.
    assert target.read_text(encoding="utf-8") == target_text
    # And no copy of it landed in the agents dir under the link name.
    assert Path(agents_dir / "link.json").is_symlink()
    real = json.loads((agents_dir / "real.json").read_text(encoding="utf-8"))
    assert "cc_model" not in real
