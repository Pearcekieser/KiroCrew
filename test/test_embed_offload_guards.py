"""Source-assertion tests for embedding offload contracts.

These tests verify that specific call sites route blocking embedding work
through the ``mc-embed`` bulkhead pool (``run_in_embed_pool``) rather than
the shared default executor, and that ``kill_process_tree`` is never called
synchronously on the event loop.

The pattern mirrors ``TestWindowsTeardownOffLoop`` in ``test_mcp_discovery.py``:
assert against the shipped source rather than simulating a platform-specific
run, because the branches may be unreachable on CI's platform.
"""

from __future__ import annotations

import inspect


class TestPersonalShopperEmbedOffload:
    """personal_shopper routes must use run_in_embed_pool for embed-heavy ops.

    ``store.add``, ``store.search``, and ``store.reembed_all`` call the
    synchronous embedder and block for 60-90s per invocation. These MUST
    route through ``run_in_embed_pool`` (the bounded mc-embed bulkhead pool)
    instead of ``asyncio.to_thread`` (the shared default executor) so that
    embedding work cannot starve fast I/O offloads that share the default pool.
    """

    def test_store_add_uses_embed_pool(self) -> None:
        from kiro_crew.apps.builtins.personal_shopper.backend import routes

        src = inspect.getsource(routes._handle_add_preference)
        assert "run_in_embed_pool" in src, (
            "store.add must be offloaded via run_in_embed_pool, not asyncio.to_thread"
        )
        assert "store.add" in src, "handler must call store.add"

    def test_store_search_uses_embed_pool(self) -> None:
        from kiro_crew.apps.builtins.personal_shopper.backend import routes

        src = inspect.getsource(routes._handle_search_preferences)
        assert "run_in_embed_pool" in src, (
            "store.search must be offloaded via run_in_embed_pool, not asyncio.to_thread"
        )
        assert "store.search" in src, "handler must call store.search"

    def test_store_reembed_all_uses_embed_pool(self) -> None:
        from kiro_crew.apps.builtins.personal_shopper.backend import routes

        src = inspect.getsource(routes._handle_reembed_preferences)
        assert "run_in_embed_pool" in src, (
            "store.reembed_all must be offloaded via run_in_embed_pool, not asyncio.to_thread"
        )
        assert "store.reembed_all" in src, "handler must call store.reembed_all"

    def test_non_embed_ops_still_use_to_thread(self) -> None:
        """list_all, update, delete are fast and should remain on asyncio.to_thread."""
        from kiro_crew.apps.builtins.personal_shopper.backend import routes

        list_src = inspect.getsource(routes._handle_list_preferences)
        assert "asyncio.to_thread" in list_src, "list_all should use asyncio.to_thread"

        update_src = inspect.getsource(routes._handle_update_preference)
        assert "asyncio.to_thread" in update_src, "update should use asyncio.to_thread"

        delete_src = inspect.getsource(routes._handle_delete_preference)
        assert "asyncio.to_thread" in delete_src, "delete should use asyncio.to_thread"


class TestOverlayKillProcessTreeOffLoop:
    """overlay.py must not call kill_process_tree synchronously on the event loop.

    ``platform_compat.kill_process_tree`` shells out to ``taskkill /T /F`` via
    a blocking ``subprocess.run`` on Windows. Running it inline on the event loop
    stalls all concurrent coroutines for the duration of the process spawn.
    """

    def test_kill_process_tree_is_offloaded(self) -> None:
        from kiro_crew.computer_use import overlay

        src = inspect.getsource(overlay)
        assert "kill_process_tree" in src, "overlay must reference kill_process_tree"
        # Every kill_process_tree call must be wrapped in asyncio.to_thread.
        for line in src.splitlines():
            if "kill_process_tree" in line and not line.strip().startswith("#"):
                # The line should either be a to_thread wrapper argument or a comment
                assert (
                    "to_thread" in line
                    or "platform_compat.kill_process_tree," in line
                ), (
                    f"kill_process_tree called on the loop: {line.strip()}"
                )

    def test_asyncio_to_thread_present(self) -> None:
        from kiro_crew.computer_use import overlay

        src = inspect.getsource(overlay)
        assert "asyncio.to_thread(" in src, (
            "overlay must use asyncio.to_thread to offload kill_process_tree"
        )
