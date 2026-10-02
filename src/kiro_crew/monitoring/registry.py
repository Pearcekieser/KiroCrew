"""The catalogue of monitored kinds, each registered as DATA.

The four boundaries that gate a kind read this table instead of naming a kind
themselves: an enum in the tool schema, a frozenset in the validator, an ``if`` in
the HTTP handler, and the ``if`` on the persistence-only path. Without it, adding a
kind means finding all four, every one of them a place to forget. Delivery reads it
too, for a different reason -- see the describer property below -- so the table has
five readers, four that gate and one that renders.

The gating claim is scoped to those four deliberately. Other spellings of a kind or
an objective survive elsewhere -- a reason code in the GitHub observation, the irq
subsystem's own dispatch -- and this module does not reach them.

This module holds one entry per kind and the boundaries ask it. Three properties are
worth separating carefully, because collapsing them is how a new kind acquires a
claim nobody checked:

**Objectives are declared BY THE KIND, not drawn from a shared vocabulary.** There
is no global objective enum here. ``review_ready`` means "the pull request is ready
for a human to look at", which is not a sentence about a calendar or a ticket, and a
shared list would make every new kind edit a common set -- the same defect as a
dispatch branch, moved to a different file.

**A capability is something a kind ANSWERS FOR, not something the registry polices.**
``supports_shadow`` says the persistence-only path is implemented for this kind. That
is a statement about what code exists, not about what a caller may ask for, so it
belongs to the kind. Modelled as an allowlist instead, a kind added later would
silently inherit a claim about a path it has never run.

**A kind describes ITSELF when it wakes someone.** ``subject_noun`` and
``wake_fields`` are the noun and the ordered canonical fields the wake envelope
renders for this kind, held as strings so delivery reads them without the registry
importing a provider. Both are required, so every registered kind carries them; an
unregistered kind has no entry to read and delivery falls closed to a neutral
envelope rather than borrowing another kind's shape, the same posture as
``supports_shadow``.

Deliberately DATA only: no probe factories and no imports of any kind's
implementation. ``kiro_crew.validation`` imports this module, and it is imported by
almost everything, so pulling a provider's transitive dependencies in here would
make a validator drag a GitHub client behind it.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The structured monitor a caller arms through ``monitor_watch``. Its subject is a
#: public GitHub pull request and its objective is review readiness.
GITHUB_PULL_REQUEST = "github_pull_request"
GITLAB_MERGE_REQUEST = "gitlab_merge_request"
AZURE_DEVOPS_PULL_REQUEST = "azure_devops_pull_request"
BITBUCKET_PULL_REQUEST = "bitbucket_pull_request"

PULL_REQUEST_MONITOR_KINDS = frozenset(
    {
        GITHUB_PULL_REQUEST,
        GITLAB_MERGE_REQUEST,
        AZURE_DEVOPS_PULL_REQUEST,
        BITBUCKET_PULL_REQUEST,
    }
)

#: The observation-gated babysit watch. Armed only INTERNALLY, from a loop's own
#: message text via ``probes.targets.infer``, never by a caller naming it, which is
#: why it is registered but not publicly armable.
#: Spelled here as data rather than imported: ``probes/__init__.py`` owns the same
#: string for the irq subsystem, and the two packages have no import edge in either
#: direction today. Introducing one for a constant would couple them ahead of the
#: porting work that unifies the vocabularies. ``test_monitor_kind_registry`` asserts
#: the two spellings agree, so a drift fails loudly instead of silently.
GH_PR = "gh-pr"

#: A public GitHub Actions workflow run. A genuinely different subject from a pull
#: request: its terminal states are success / failure / cancelled / timed_out, not
#: merged or closed. Registered but NOT publicly armable, because a workflow run
#: cannot be inferred from a message by URL the way a pull request can, so a caller
#: has no way to name one -- see the note on ``publicly_armable`` below for whether
#: that is a property of the subject or a limit still to lift.
GITHUB_WORKFLOW_RUN = "github_workflow_run"

REVIEW_READY = "review_ready"

#: The HOLD objective: watch a pull request until it is merged or closed. It reads
#: the same probe and the same conditions as ``review_ready``; what differs is
#: what review readiness MEANS. Under ``review_ready`` it is the goal, so it ends
#: the watch. Under ``until_merged`` it is a state the pull request is held in
#: while it waits for a maintainer, so the watch stays armed through it and wakes
#: on the next change instead -- a label, a comment, a red check, a conflict.
#: Declared by the GitHub kind alone, because only that adapter observes labels
#: and PR-level comments, which are most of what a held pull request changes.
UNTIL_MERGED = "until_merged"

#: For each HOLDING objective, the success reason codes that still end it. An
#: objective absent from this map is not a holding one, and every success ends it,
#: which is what ``review_ready`` has always done. A reason code rather than a new
#: observation status, because the classifier is shared by every pull-request kind
#: and already names the two kinds of success apart (``pull_request_merged`` and
#: ``review_ready``); a fifth status would have to be taught to every reader of the
#: enum to say what this one map says.
_HOLDING_TERMINAL_SUCCESS_REASONS: dict[str, frozenset[str]] = {
    UNTIL_MERGED: frozenset({"pull_request_merged"}),
}

#: What a workflow run's completion IS. Declared by the workflow-run kind alone, not
#: drawn from a shared vocabulary: ``review_ready`` means "a human can look at this
#: pull request now", which is not a sentence about a run. A run's objective is that
#: it REACHED a conclusion, whatever that conclusion turns out to be.
RUN_COMPLETE = "run_complete"


@dataclass(frozen=True)
class MonitorKind:
    """One monitored kind and what it answers for."""

    name: str
    #: The objectives THIS kind supports. Not a slice of a shared enum.
    objectives: frozenset[str]
    #: Whether a caller may name this kind when arming a monitor. A kind derived
    #: internally from a loop's message is registered without being requestable.
    publicly_armable: bool
    #: Whether the persistence-only probe path is implemented for this kind.
    supports_shadow: bool
    #: The common noun the wake envelope uses for this subject -- "pull request"
    #: for a review kind, "workflow run" for a run kind. Held here so a woken
    #: agent is told what it is looking at by the kind, not by a word the delivery
    #: code hardcodes for one subject and misapplies to every other.
    subject_noun: str
    #: The canonical field names the wake envelope renders as ``name=value``, in
    #: order. A CLAIM about the dict this kind's probe emits: a name absent from
    #: the probe's canonical is silently skipped, so a test pins each kind's list
    #: against the keys its provider actually produces. The special name
    #: ``checks`` renders the failed/pending/unknown bucket counts a review
    #: subject carries, so a run kind that has no such buckets simply omits it.
    wake_fields: tuple[str, ...]


_KINDS: dict[str, MonitorKind] = {
    GITHUB_PULL_REQUEST: MonitorKind(
        name=GITHUB_PULL_REQUEST,
        objectives=frozenset({REVIEW_READY, UNTIL_MERGED}),
        publicly_armable=True,
        supports_shadow=True,
        subject_noun="pull request",
        wake_fields=(
            "checks",
            "blocking_review",
            "mergeability",
            "review_decision",
            "state",
            "labels",
        ),
    ),
    GITLAB_MERGE_REQUEST: MonitorKind(
        name=GITLAB_MERGE_REQUEST,
        objectives=frozenset({REVIEW_READY}),
        publicly_armable=True,
        supports_shadow=True,
        subject_noun="merge request",
        wake_fields=("checks", "blocking_review", "mergeability", "review_decision", "state"),
    ),
    AZURE_DEVOPS_PULL_REQUEST: MonitorKind(
        name=AZURE_DEVOPS_PULL_REQUEST,
        objectives=frozenset({REVIEW_READY}),
        publicly_armable=True,
        supports_shadow=True,
        subject_noun="pull request",
        wake_fields=("checks", "blocking_review", "mergeability", "review_decision", "state"),
    ),
    BITBUCKET_PULL_REQUEST: MonitorKind(
        name=BITBUCKET_PULL_REQUEST,
        objectives=frozenset({REVIEW_READY}),
        publicly_armable=True,
        supports_shadow=True,
        subject_noun="pull request",
        wake_fields=("checks", "blocking_review", "mergeability", "review_decision", "state"),
    ),
    GH_PR: MonitorKind(
        name=GH_PR,
        objectives=frozenset({REVIEW_READY}),
        publicly_armable=False,
        supports_shadow=False,
        subject_noun="pull request",
        wake_fields=("checks", "blocking_review", "mergeability", "review_decision", "state"),
    ),
    GITHUB_WORKFLOW_RUN: MonitorKind(
        name=GITHUB_WORKFLOW_RUN,
        objectives=frozenset({RUN_COMPLETE}),
        publicly_armable=False,
        supports_shadow=True,
        subject_noun="workflow run",
        wake_fields=("status", "conclusion", "workflow_name", "event"),
    ),
}


def monitor_kind(name: object) -> MonitorKind | None:
    """The entry for *name*, or None when nothing is registered under it."""
    if not isinstance(name, str):
        return None
    return _KINDS.get(name)


def publicly_armable_kinds() -> frozenset[str]:
    """The kinds a caller may name. This is what an entry allowlist should expose."""
    return frozenset(entry.name for entry in _KINDS.values() if entry.publicly_armable)


def publicly_armable_objectives() -> frozenset[str]:
    """Every objective reachable through a publicly armable kind.

    A union, because a JSON-Schema ``enum`` and a validator's ``allowed`` set are
    both flat: neither can express "this objective only with that kind". The pairing
    is enforced by :func:`kind_supports_objective` at the boundary that knows both,
    so the flat set is a first filter and never the whole check.
    """
    return frozenset(
        objective
        for entry in _KINDS.values()
        if entry.publicly_armable
        for objective in entry.objectives
    )


def kind_supports_objective(kind: object, objective: object) -> bool:
    """Whether *kind* is registered AND declares *objective*."""
    entry = monitor_kind(kind)
    return entry is not None and isinstance(objective, str) and objective in entry.objectives


def objective_holds_past_success(objective: object) -> bool:
    """Whether *objective* keeps its watch armed through a non-terminal success."""
    return isinstance(objective, str) and objective in _HOLDING_TERMINAL_SUCCESS_REASONS


def objective_success_is_terminal(objective: object, reason_code: object) -> bool:
    """Whether a SUCCESS observation under *reason_code* ends a watch on *objective*.

    True for every success under an objective that does not hold, so ``review_ready``
    keeps its long-standing meaning; under a holding objective only the reason codes
    it declares terminal end the watch.
    """
    terminal = (
        _HOLDING_TERMINAL_SUCCESS_REASONS.get(objective) if isinstance(objective, str) else None
    )
    if terminal is None:
        return True
    return isinstance(reason_code, str) and reason_code in terminal


def kind_supports_shadow(kind: object) -> bool:
    """Whether the persistence-only probe path is implemented for *kind*.

    An unregistered kind answers False: a path nobody registered is a path nobody
    implemented.
    """
    entry = monitor_kind(kind)
    return entry is not None and entry.supports_shadow
