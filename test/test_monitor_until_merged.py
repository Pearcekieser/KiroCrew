"""Labels on the GitHub pull-request watch, and the ``until_merged`` hold objective.

``review_ready`` ends a watch the moment a pull request is ready. A hold loop --
keep the pull request ready until a maintainer merges it, rebase on conflict,
answer new comments -- needs the watch to stay armed through readiness and to see
the readiness LABEL move, because that is where the readiness gate publishes its
verdict. These tests pin both halves without a model turn.
"""

from __future__ import annotations

from kiro_crew.monitoring.controller import format_monitor_wake
from kiro_crew.monitoring.decision import decide_monitor, monitor_stall_reason
from kiro_crew.monitoring.github_pull_request import (
    _normalize_labels,
    _rest_labels,
    _rest_primary_node,
)
from kiro_crew.monitoring.models import (
    DEFAULT_MONITOR_STALL_MIN_SECS,
    DEFAULT_MONITOR_STALL_TICKS,
    MAX_PULL_REQUEST_LABEL_CHARS,
    MAX_PULL_REQUEST_LABELS,
    MonitorBudgets,
    PULL_REQUEST_LABELS_INCOMPLETE,
    MonitorDecision,
    MonitorObservationStatus,
    MonitorState,
    monitor_state_public_dict,
)
from kiro_crew.monitoring.pull_request import (
    PullRequestCheck,
    PullRequestFacts,
    build_pull_request_probe_result,
    canonical_pull_request_facts,
    normalize_pull_request_labels,
    pull_request_conditions,
)
from kiro_crew.monitoring.registry import (
    GITHUB_PULL_REQUEST,
    GITLAB_MERGE_REQUEST,
    REVIEW_READY,
    UNTIL_MERGED,
    kind_supports_objective,
    objective_holds_past_success,
    objective_success_is_terminal,
)
from kiro_crew.validation import MONITOR_WATCH_SCHEMA, validate_tool_args

_HEAD = "0123456789abcdef0123456789abcdef01234567"
_OTHER_HEAD = "fedcba9876543210fedcba9876543210fedcba98"


def _facts(**changes: object) -> PullRequestFacts:
    values: dict[str, object] = {
        "kind": GITHUB_PULL_REQUEST,
        "target": "github.com/owner/repo#123",
        "state": "open",
        "draft": False,
        "head_revision": _HEAD,
        "mergeability": "mergeable",
        "review_decision": "approved",
        "checks": (PullRequestCheck("CI / test", "passed"),),
        "unresolved_review_threads": 0,
        "review_threads_complete": True,
    }
    values.update(changes)
    return PullRequestFacts(**values)  # type: ignore[arg-type]


def _state(objective: str = UNTIL_MERGED, **changes: object) -> MonitorState:
    values: dict[str, object] = {
        "kind": GITHUB_PULL_REQUEST,
        "target": "github.com/owner/repo#123",
        "objective": objective,
        "created_ts": 1_000.0,
        # Long enough that a multi-hour hold below is bounded by the stall rule
        # under test, not by the default runtime budget.
        "budgets": MonitorBudgets(max_runtime_secs=7 * 86_400),
    }
    values.update(changes)
    return MonitorState(**values)  # type: ignore[arg-type]


def _tick(state: MonitorState, facts: PullRequestFacts, *, now: float) -> MonitorDecision:
    """One probe-and-decide step, with the driver's own bookkeeping after it.

    Mirrors what ``apply_monitor_probe`` writes beside the decision: the observed
    fingerprint and canonical every tick, and the wake fingerprint only on a wake.
    """
    result = build_pull_request_probe_result(facts, previous_observation=state.last_observation)
    decision = decide_monitor(state, result.observation, now=now).decision
    state.last_observation = dict(result.canonical)
    state.last_fingerprint = result.observation.fingerprint
    if decision is MonitorDecision.WAKE_ACTIONABLE:
        state.last_wake_fingerprint = result.observation.fingerprint
        state.coalesce_alerted.update(dict.fromkeys(state.coalesce_windows, now))
    return decision


class TestLabelNormalization:
    def test_names_are_sorted_and_deduplicated(self) -> None:
        assert normalize_pull_request_labels(["readiness: passed", "bug", "bug"]) == (
            "bug",
            "readiness: passed",
        )

    def test_control_characters_collapse_and_long_names_are_cut(self) -> None:
        labels = normalize_pull_request_labels(["a\nb\u2028c", "x" * 80])
        assert labels == ("a b c", "x" * MAX_PULL_REQUEST_LABEL_CHARS)

    def test_non_string_and_blank_entries_are_dropped(self) -> None:
        assert normalize_pull_request_labels([None, 7, "  ", "ok"]) == ("ok",)
        assert normalize_pull_request_labels("not-a-list") == ()

    def test_a_cut_list_says_so_instead_of_passing_for_the_whole_set(self) -> None:
        many = [f"label-{index:03d}" for index in range(MAX_PULL_REQUEST_LABELS + 5)]
        labels = normalize_pull_request_labels(many)
        assert len(labels) == MAX_PULL_REQUEST_LABELS
        assert PULL_REQUEST_LABELS_INCOMPLETE in labels
        assert PULL_REQUEST_LABELS_INCOMPLETE in normalize_pull_request_labels(
            ["a"], complete=False
        )

    def test_graphql_connection_is_read_and_absent_means_none(self) -> None:
        raw = {"totalCount": 2, "nodes": [{"name": "zeta"}, {"name": "alpha"}]}
        assert _normalize_labels(raw) == ("alpha", "zeta")
        assert _normalize_labels(None) == ()
        partial = {"totalCount": 3, "nodes": [{"name": "alpha"}]}
        assert PULL_REQUEST_LABELS_INCOMPLETE in _normalize_labels(partial)

    def test_rest_labels_translate_to_the_same_shape(self) -> None:
        node = _rest_primary_node(
            {
                "number": 123,
                "state": "open",
                "draft": False,
                "head": {"sha": _HEAD},
                "mergeable": True,
                "mergeable_state": "clean",
                "labels": [{"name": "readiness: passed", "color": "0e8a16"}],
            }
        )
        assert _normalize_labels(node["labels"]) == ("readiness: passed",)
        assert _rest_labels(None) is None


class TestCanonicalShape:
    def test_a_label_less_subject_keeps_the_pinned_canonical_shape(self) -> None:
        assert "labels" not in canonical_pull_request_facts(_facts())

    def test_labels_are_carried_only_when_present(self) -> None:
        canonical = canonical_pull_request_facts(_facts(labels=("bug", "readiness: passed")))
        assert canonical["labels"] == ["bug", "readiness: passed"]

    def test_the_public_projection_shows_labels_when_present(self) -> None:
        state = _state(
            last_observation=canonical_pull_request_facts(_facts(labels=("readiness: passed",)))
        )
        assert monitor_state_public_dict(state)["last_observation"]["labels"] == [
            "readiness: passed"
        ]
        bare = _state(last_observation=canonical_pull_request_facts(_facts()))
        assert "labels" not in monitor_state_public_dict(bare)["last_observation"]


class TestLabelConditions:
    def test_a_label_set_change_is_a_new_condition_key(self) -> None:
        before = canonical_pull_request_facts(_facts(labels=("readiness: action required",)))
        after = canonical_pull_request_facts(_facts(labels=("readiness: passed",)))
        before_keys = {
            c.key for c in pull_request_conditions(before) if c.key.startswith("labels:")
        }
        after_keys = {c.key for c in pull_request_conditions(after) if c.key.startswith("labels:")}
        assert before_keys and after_keys and before_keys != after_keys

    def test_no_labels_means_no_label_condition(self) -> None:
        keys = {c.key for c in pull_request_conditions(canonical_pull_request_facts(_facts()))}
        assert not any(key.startswith("labels:") for key in keys)

    def test_an_actionable_subject_wakes_again_when_its_labels_move(self) -> None:
        state = _state(objective=REVIEW_READY)
        red = (PullRequestCheck("CI / test", "failed"),)
        first = _tick(state, _facts(checks=red, labels=("readiness: pending",)), now=2_000.0)
        assert first is MonitorDecision.WAKE_ACTIONABLE
        # Same failure, new label set, past the coalescing floor: a new key wakes.
        second = _tick(state, _facts(checks=red, labels=("readiness: failed",)), now=2_400.0)
        assert second is MonitorDecision.WAKE_ACTIONABLE
        third = _tick(state, _facts(checks=red, labels=("readiness: failed",)), now=2_800.0)
        assert third is MonitorDecision.NO_CHANGE

    def test_the_wake_envelope_names_the_new_labels(self) -> None:
        envelope = format_monitor_wake(
            monitor_id="m1",
            kind=GITHUB_PULL_REQUEST,
            target="https://github.com/owner/repo/pull/123",
            objective=UNTIL_MERGED,
            fingerprint="f" * 64,
            reason_code="review_ready",
            canonical=canonical_pull_request_facts(_facts(labels=("bug", "readiness: passed"))),
        )
        assert "labels=[bug, readiness: passed]" in envelope


class TestUntilMergedObjective:
    def test_it_is_declared_by_the_github_kind_alone(self) -> None:
        assert kind_supports_objective(GITHUB_PULL_REQUEST, UNTIL_MERGED)
        assert not kind_supports_objective(GITLAB_MERGE_REQUEST, UNTIL_MERGED)

    def test_the_validator_accepts_it(self) -> None:
        args = validate_tool_args(
            {
                "kind": GITHUB_PULL_REQUEST,
                "target": "https://github.com/owner/repo/pull/123",
                "objective": UNTIL_MERGED,
                "max_runtime_secs": 3600,
            },
            MONITOR_WATCH_SCHEMA,
        )
        assert args["objective"] == UNTIL_MERGED

    def test_terminal_success_is_merge_only_under_the_hold(self) -> None:
        assert objective_holds_past_success(UNTIL_MERGED)
        assert not objective_holds_past_success(REVIEW_READY)
        assert objective_success_is_terminal(UNTIL_MERGED, "pull_request_merged")
        assert not objective_success_is_terminal(UNTIL_MERGED, "review_ready")
        assert objective_success_is_terminal(REVIEW_READY, "review_ready")

    def test_review_ready_still_ends_the_watch_on_readiness(self) -> None:
        state = _state(objective=REVIEW_READY)
        assert _tick(state, _facts(), now=2_000.0) is MonitorDecision.STOP_SUCCESS

    def test_readiness_wakes_once_and_then_holds_quietly(self) -> None:
        state = _state()
        assert _tick(state, _facts(), now=2_000.0) is MonitorDecision.WAKE_ACTIONABLE
        assert _tick(state, _facts(), now=2_300.0) is MonitorDecision.NO_CHANGE
        assert _tick(state, _facts(), now=2_600.0) is MonitorDecision.NO_CHANGE

    def test_a_label_change_on_a_held_subject_wakes(self) -> None:
        state = _state()
        _tick(state, _facts(labels=("readiness: pending",)), now=2_000.0)
        decision = _tick(state, _facts(labels=("readiness: passed",)), now=2_300.0)
        assert decision is MonitorDecision.WAKE_ACTIONABLE

    def test_a_new_comment_on_a_held_subject_wakes(self) -> None:
        state = _state()
        _tick(state, _facts(), now=2_000.0)
        decision = _tick(state, _facts(pr_comment_body_digest="a" * 64), now=2_300.0)
        assert decision is MonitorDecision.WAKE_ACTIONABLE

    def test_check_churn_and_a_mergeability_blip_do_not_wake_a_held_subject(self) -> None:
        state = _state()
        _tick(state, _facts(), now=2_000.0)
        more_checks = (
            PullRequestCheck("CI / test", "passed"),
            PullRequestCheck("CI / rerun", "passed"),
        )
        assert _tick(state, _facts(checks=more_checks), now=2_300.0) is MonitorDecision.NO_CHANGE
        # The base branch moved: GitHub recomputes mergeability for a tick.
        blip = _tick(state, _facts(checks=more_checks, mergeability="pending"), now=2_600.0)
        assert blip is MonitorDecision.RECORD_ONLY
        assert _tick(state, _facts(checks=more_checks), now=2_900.0) is MonitorDecision.NO_CHANGE

    def test_a_red_check_or_conflict_on_a_held_subject_wakes(self) -> None:
        state = _state()
        _tick(state, _facts(), now=2_000.0)
        red = _tick(state, _facts(checks=(PullRequestCheck("CI / test", "failed"),)), now=2_300.0)
        assert red is MonitorDecision.WAKE_ACTIONABLE
        held = _state()
        _tick(held, _facts(), now=2_000.0)
        assert (
            _tick(held, _facts(mergeability="conflicting"), now=2_300.0)
            is MonitorDecision.WAKE_ACTIONABLE
        )

    def test_returning_to_ready_after_an_actionable_wake_reports_readiness(self) -> None:
        state = _state()
        red = (PullRequestCheck("CI / test", "failed"),)
        assert _tick(state, _facts(checks=red), now=2_000.0) is MonitorDecision.WAKE_ACTIONABLE
        # A re-run turns the board green on the same head: the hold reports it once.
        assert _tick(state, _facts(), now=2_600.0) is MonitorDecision.WAKE_ACTIONABLE
        assert _tick(state, _facts(), now=2_900.0) is MonitorDecision.NO_CHANGE

    def test_a_new_head_on_a_held_subject_wakes(self) -> None:
        state = _state()
        _tick(state, _facts(), now=2_000.0)
        assert (
            _tick(state, _facts(head_revision=_OTHER_HEAD), now=2_600.0)
            is MonitorDecision.WAKE_ACTIONABLE
        )

    def test_merge_ends_the_hold_with_success(self) -> None:
        state = _state()
        _tick(state, _facts(), now=2_000.0)
        assert _tick(state, _facts(state="merged"), now=2_300.0) is MonitorDecision.STOP_SUCCESS

    def test_close_ends_the_hold_as_blocked(self) -> None:
        state = _state()
        _tick(state, _facts(), now=2_000.0)
        assert _tick(state, _facts(state="closed"), now=2_300.0) is MonitorDecision.STOP_BLOCKED

    def test_a_long_quiet_hold_is_not_retired_as_a_stall(self) -> None:
        """A ready PR waiting hours for a maintainer concludes the same every tick."""
        state = _state()
        now = 2_000.0
        _tick(state, _facts(), now=now)
        ticks = DEFAULT_MONITOR_STALL_TICKS * 4
        step = DEFAULT_MONITOR_STALL_MIN_SECS / DEFAULT_MONITOR_STALL_TICKS * 2
        for _ in range(ticks):
            now += step
            assert _tick(state, _facts(), now=now) is MonitorDecision.NO_CHANGE
        assert state.stall_streak == 0
        assert monitor_stall_reason(state, now=now) == ""

    def test_a_long_unresolved_thread_is_not_retired_as_a_stall_under_the_hold(self) -> None:
        state = _state()
        now = 2_000.0
        _tick(state, _facts(unresolved_review_threads=1), now=now)
        step = DEFAULT_MONITOR_STALL_MIN_SECS / DEFAULT_MONITOR_STALL_TICKS * 2
        for _ in range(DEFAULT_MONITOR_STALL_TICKS * 4):
            now += step
            decision = _tick(state, _facts(unresolved_review_threads=1), now=now)
            assert decision is not MonitorDecision.STOP_BLOCKED
        assert state.stall_streak == 0

    def test_the_held_observation_is_still_classified_success(self) -> None:
        result = build_pull_request_probe_result(_facts())
        assert result.observation.status is MonitorObservationStatus.SUCCESS
        assert result.observation.reason_code == "review_ready"
