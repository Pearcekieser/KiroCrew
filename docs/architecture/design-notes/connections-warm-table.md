# Connections warm-mint table

Cold mint (`kiro_crew.connections.mint`) spawns one kiro-cli process per provider for one
approval URL: ~7.5s per card. `kiro_crew.connections.warm` serves the whole gallery from one
process, and every rule below answers an observed failure.

**Placement.** All warm code is in `src/kiro_crew/connections/warm.py`; the dashboard handler
adds only endpoint wiring and a function-local `expire_dead_mints` import on the status path,
keeping the mint engine off the gateway's boot path.

**Scope boundary: the TABLE and the SPECS have landed; the PROCESS LIFECYCLE has not.**
Shipped now: the shared row shape (`shared`/`generation`/`activation`), the liveness registry
those stamps are read against, `expire_dead_mints()` on the status path, and the spec side --
the registry-derived universe, the plan and its servability test, the tool-alias key shape, the
spec files the plan writes, and the filesystem-drift guard covering their synchronous helpers.
Still deferred: everything that spawns, activates, parks or kills a process -- the spawn/respawn
rules, the reaper, and `warm_mint_all`, which is the only thing that would give the spec planner
a caller. The lifecycle slice lands those tests together with the code. Until it does, nothing
calls the planner and no row is ever `shared`, so the shipped `expire_dead_mints()` call is a
no-op scan whose predicate is nonetheless complete for the parked case, because a reader blind
to a parked process withdraws a URL that process can still redeem.

## Measured facts

- Activation costs a fixed ~5.18s whether the spec carries one remote server or six, and an
  initialized process mints in ~5.4s. ACP `initialize`, the expensive half, is paid once at
  spawn, so one activation warms every card.
- **A challenge is half per-process and half per-session.** The PKCE verifier is a value in
  process memory and coexists with its peers (six proven live); the loopback callback *server*
  is one of the session's MCP children, so `session/terminate` reaps it -- popping the URL and
  destroying the handle left a `redirect_uri` whose port accepted a bare connect, then reset
  every real exchange with zero bytes. So the session is *held*, and redeemability takes two
  questions: `generation_is_live` (the process holds the verifier) **and** `activation_is_live`
  (the session still answers the redirect). Process liveness alone passed the
  terminated-session case.

## Specs are read once at spawn

A spec written after spawn is invisible (`set_mode` answers "Mode not found") and a rewrite is
not honoured, so the whole set is written before spawn and any change needs a *new process*,
tracked by `_WarmSpecPlan.digest`. A respawn destroys every peer's in-flight consent listener,
so respawn frequency is the dominant design pressure:

- **The spec universe is registry-derived and blind to grant and cancel state.** Connect writes
  an MCP entry for the provider being connected, so a config-derived plan changed on every
  click, and a plan tracking "who needs a URL now" changed on every completed consent and every
  Cancel -- either retired a process holding other cards' listeners.
- **Digest equality is not the respawn test** -- it reads a set that *shrank* as one that
  changed. `_plan_is_servable` asks whether every entry the new plan needs is already resident
  with an identical authorization ask, re-activating on the same process when it is: a Connect
  costing 0.13s instead of 7.5s. An unservable change **parks** rather than kills, so the
  outgoing generation keeps serving the consents it holds until the reaper collects it, once
  its rows are gone or expired.

## Cut against the shipped engine

`mint.py` (PR #3154) is the reviewed engine and owns the row table, the row identity token,
grant detection, spec emission and the manifest sweep; warm imports all of it and adds only
what is genuinely per-process. Three adaptations:

- `_mint_holder_alive` is deliberately **not** reused -- it reads the row's own `client`, which
  a shared row does not own, so it answers False for every warm row. `_warm_row_alive` asks the
  generation/activation pair instead.
- Warm spec names are fixed (`kirocrew-mint-warm-*`), with no `-<pid>-<8hex>` suffix, keeping
  them out of the cold engine's manifest sweep. That shared prefix is a hazard in reverse -- a
  *cold* spec for a server named `warm-*` matches the warm glob -- so `_is_stale_warm_spec`
  refuses anything matching the cold name shape, and both patterns must share one **character
  class**: while warm accepted `[A-Za-z]` and cold only `[a-z]`, a mixed-case alias produced a
  live cold spec the warm sweep read as its own and unlinked.
- **A name is not ownership.** Those fixed names are predictable and they live in the user's own
  agents directory, next to the agents they hand-write, so a name says where a spec of ours
  would *go* and never that the file already there is one. Trusting the name shape alone was a
  defect in both directions: the write-time sweep unlinked a user's own agent spec sitting at
  such a path, and the write then clobbered one at a path the current plan wanted.
  `_warm_spec_is_foreign` proves ownership from the file's CONTENTS, and it takes two halves.
  The fields the spec body fixes (`model`, `includeMcpJson`, `prompt`, `allowedTools`) are read
  off `_mint_spec_body` so a change to the body cannot leave the module unable to recognise its
  own files -- but they are also **stock defaults** a hand-written or scaffolded agent plausibly
  carries, so on their own they still read a wholly user-authored spec as ours. The
  discriminating half is `_WARM_SPEC_SENTINEL`, stamped as the description prefix of every spec
  written here. `description` is the only field free enough to carry a marker while staying
  schema-legal: kiro-cli rejects an unknown spec key, and the agent-spec migration sweep strips
  bookkeeping keys. Requiring it orphans nothing, since no warm-spec writer has ever shipped --
  and a hypothetical unsentinelled file of ours would read as foreign, which means refused and
  left in place. It fails closed, because the mistakes are not symmetric: reading our own file
  as foreign leaves one stale spec as clutter, while reading a user's file as ours destroys it.
  A refusal is audited and skipped -- never raised -- so an occupied path costs one unwarmed
  provider, not a failed spawn.

## Tool-alias key shape

`resolve_tool_aliases` de-collides by registry **slug**, keying `@slug/tool`, while a warm spec
mounts under `mcp_server_alias(slug)`. Where the two differ a slug-keyed entry names a server
the spec never mounted, kiro-cli applies no rename, and the collision returns silently, so
`connections_tool_aliases` re-points keys at the mounted alias and leaves the resolver
authoritative over which tools collide. Every registry slug is slash-free today, so this is an
identity map holding the shape contract of the spec we write, not a live defect. Semantics are
#3260's -- **every** claimant is renamed, none keeps the bare name; an earlier draft asserted
the pre-#3260 rule and those assertions were not carried forward.

## Filesystem work never runs on the loop

Every flow reads the user's config, the shared agents directory, or kiro-cli's OAuth cache, any
of which can sit on a network mount where a stat is unbounded, so all of that work lives in
SYNCHRONOUS helpers and a coroutine reaches them through `asyncio.to_thread` -- enforced by a
fixed-point drift guard in `test/test_connections_warm.py` that reuses the mint engine's own
primitive sets so the two cannot drift apart. What the guard pins today is the exact set of
helpers doing filesystem work, so the lifecycle slice can neither call one from a coroutine nor
quietly drop the filesystem work the guard's coverage rests on without failing it.

## Seams and residuals

**Shared-mint expiry** is the lifecycle slice's, keyed on the fact that a minting process is
gone rather than on a cause, which is what covers a process that went away by a route no expiry
path anticipated. PR #5899 is a different cause and a different table: it owns
**Disconnect-driven grant revocation**, and the two meet only where a revoke should re-warm.
**Proactive refresh** attaches to the reaper the lifecycle slice introduces; **a
supervisor/watchdog** is absent, as are the accessors it would need. Two residuals:

- **A cancel between the claim and the activation leaks the claim.** The lifecycle entry point
  takes its claim outside any `try`/`finally` and the activation catches `Exception`, not
  `BaseException`, so a `CancelledError` in that window leaves rows `minting` with no watcher:
  nothing expires them, the pending check stays true, and the process is never retired.
  Unreachable while that entry point does not exist, and **must be closed before the lifecycle
  slice wires it up** -- along with replacing a shared row's state-string fence with a unique row
  token (issue #6110), and screening an approval URL for a credential *before* it is stored.
- **A hard gateway kill strands warm spec files.** They carry no manifest row, so the cold
  engine's aged-row sweep cannot see them. The next spawn's write-time sweep removes them, so
  the exposure is bounded, but it is not a clean teardown.
