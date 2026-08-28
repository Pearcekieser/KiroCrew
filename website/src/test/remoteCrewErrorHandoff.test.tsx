/**
 * Remote-crew failure → agent hand-off.
 *
 * The first-time-setup path is what these pin, in the two shapes it fails in:
 *  1. **Add** rejects the registration. The banner offers the hand-off, and the
 *     hand-off must not eat the form — hence the stash, and hence the ORDERING
 *     of `onHandoff` (before the navigation, while the subtree still exists).
 *  2. **Connect** fails. The evidence rides a 200 poll, so a surface has to
 *     journal it itself, and the report has to carry the diagnosis ladder — the
 *     part that says which link in the chain broke — not just the sentence shown.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ErrorNotice from '../components/ErrorNotice'
import type { InstanceTunnelStatus } from '../api/client'
import {
  instanceFormValuesFrom,
  clearInstanceFormDraft,
  peekInstanceFormDraft,
  stashInstanceFormDraft,
  isBlankInstanceForm,
  EMPTY_INSTANCE_FORM,
} from '../pages/settings/InstanceFormFields'
import { reportInstanceFailure, __resetInstanceFailuresForTests } from '../utils/instanceFailureReport'
import {
  consumeChatHandoff,
  installSoftNavigate,
  recentErrors,
  __resetErrorJournalForTests,
  __resetNavSeamForTests,
} from '../utils/errorReport'

const navigated: string[] = []

beforeEach(() => {
  __resetErrorJournalForTests()
  __resetNavSeamForTests()
  __resetInstanceFailuresForTests()
  navigated.length = 0
  sessionStorage.clear()
  installSoftNavigate(to => { navigated.push(to) })
})

afterEach(() => {
  __resetNavSeamForTests()
  vi.restoreAllMocks()
})

const brokenStatus = (over: Partial<InstanceTunnelStatus> = {}): InstanceTunnelStatus => ({
  instance_id: 'cd-1',
  state: 'error',
  error: 'tunnel failed',
  diagnosis: {
    code: 'remote_down',
    ok: false,
    reason: 'SSH works but the remote dashboard is not responding',
    probes: [
      { name: 'ssh', ok: true },
      { name: 'remote_dashboard', ok: false },
    ],
  },
  ...over,
})

describe('add-form draft stash', () => {
  it('round-trips values and clears on demand', () => {
    expect(peekInstanceFormDraft()).toBeNull()
    expect(stashInstanceFormDraft({ ...EMPTY_INSTANCE_FORM, name: 'Box', sshHost: 'h' })).toBe(true)
    expect(peekInstanceFormDraft()).toMatchObject({ name: 'Box', sshHost: 'h' })
    // Non-destructive: StrictMode mounts twice, so the second mount must still
    // find the draft the first one read.
    expect(peekInstanceFormDraft()).toMatchObject({ name: 'Box' })
    clearInstanceFormDraft()
    expect(peekInstanceFormDraft()).toBeNull()
  })

  it('survives the tab, not a clock — no TTL to lapse mid-conversation', () => {
    // sessionStorage already scopes the draft to the tab, which is the honest
    // lifetime. A time bound on top of it would only add a way to lose the fields
    // while the user is still in the agent conversation the hand-off opened.
    stashInstanceFormDraft({ ...EMPTY_INSTANCE_FORM, name: 'Kept' })
    vi.spyOn(Date, 'now').mockReturnValue(Date.now() + 6 * 3600_000)
    expect(peekInstanceFormDraft()).toMatchObject({ name: 'Kept' })
  })

  it('reports false when the values are not retrievable afterwards', () => {
    // The caller uses this verdict to decide whether to let a navigation unmount
    // the form, so it is the WRITE's own answer, never assumed from the call
    // having returned.
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('quota')
    })
    expect(stashInstanceFormDraft({ ...EMPTY_INSTANCE_FORM, name: 'Lost' })).toBe(false)
  })

  it('reports false when a STALE draft is still sitting in storage', () => {
    // Hand off once, come back, edit further, hand off again while storage has
    // started refusing writes. "Something is stored" is TRUE here — of the EARLIER
    // draft — so any presence-shaped verdict passes and the newer edits are lost.
    expect(stashInstanceFormDraft({ ...EMPTY_INSTANCE_FORM, name: 'First' })).toBe(true)
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('quota')
    })
    expect(stashInstanceFormDraft({ ...EMPTY_INSTANCE_FORM, name: 'Second' })).toBe(false)
  })

  it('rejects a malformed payload rather than half-restoring a form', () => {
    sessionStorage.setItem('kirocrew_instance_form_draft', 'not json')
    expect(peekInstanceFormDraft()).toBeNull()
  })

  it('is blank only when every field still equals its default', () => {
    // `method` and `remotePort` ship with values, so an untouched form is not
    // all-empty — comparing against emptiness would treat it as worth stashing
    // and leave a phantom draft that pre-fills the next add.
    expect(isBlankInstanceForm(EMPTY_INSTANCE_FORM)).toBe(true)
    expect(isBlankInstanceForm({ ...EMPTY_INSTANCE_FORM, sshHost: 'h' })).toBe(false)
  })
})

describe('instanceFormValuesFrom', () => {
  it('accepts a complete record', () => {
    expect(instanceFormValuesFrom({ ...EMPTY_INSTANCE_FORM, name: 'Box' })).toMatchObject({
      name: 'Box',
      method: 'ssh',
    })
  })

  it('rejects a partial record whole', () => {
    // A merged-over-defaults restore would look complete while carrying a field
    // the user never typed, so a missing key must reject the entire payload.
    const { sshHost: _dropped, ...partial } = EMPTY_INSTANCE_FORM
    expect(instanceFormValuesFrom(partial)).toBeNull()
  })

  it('rejects a non-string field and an unknown transport', () => {
    expect(instanceFormValuesFrom({ ...EMPTY_INSTANCE_FORM, remotePort: 5476 })).toBeNull()
    expect(instanceFormValuesFrom({ ...EMPTY_INSTANCE_FORM, method: 'telnet' })).toBeNull()
    expect(instanceFormValuesFrom(null)).toBeNull()
    expect(instanceFormValuesFrom([EMPTY_INSTANCE_FORM])).toBeNull()
  })
})

describe('ErrorNotice askAgent hand-off callbacks', () => {
  it('runs beforeHandoff BEFORE navigating, so a caller can persist its state', async () => {
    const order: string[] = []
    installSoftNavigate(to => { order.push(`navigate:${to}`) })
    render(
      <ErrorNotice
        message="name already in use"
        askAgent
        beforeHandoff={() => { order.push('persist') }}
      />,
    )
    await userEvent.click(screen.getByRole('button', { name: /agent/i }))
    // The navigation unmounts the subtree the caller's state lives in, so a
    // callback that ran afterwards could not save anything.
    expect(order).toEqual(['persist', 'navigate:/chat'])
  })

  it('runs afterHandoff only once the hand-off proceeded', async () => {
    const dismissed: string[] = []
    render(
      <ErrorNotice
        message="update failed"
        askAgent
        afterHandoff={() => { dismissed.push('cleared') }}
      />,
    )
    await userEvent.click(screen.getByRole('button', { name: /agent/i }))
    expect(dismissed).toEqual(['cleared'])
  })

  it('does NOT dismiss when staging failed, so the error is not erased for nothing', async () => {
    // The dismissing caller (App's update-error banner) clears the very message
    // being shown. Running it on a failed staging leaves no navigation AND no
    // visible diagnostic — the error erased with nothing in its place.
    const dismissed: string[] = []
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('quota')
    })
    render(
      <ErrorNotice
        message="update failed"
        askAgent
        afterHandoff={() => { dismissed.push('cleared') }}
      />,
    )
    await userEvent.click(screen.getByRole('button', { name: /agent/i }))
    expect(navigated).toEqual([])
    expect(dismissed).toEqual([])
  })

  it('treats a throw from beforeHandoff as a veto, not as something to swallow', async () => {
    // The callback's whole job is persisting, so a throw means it did not finish.
    // Swallowing it and navigating anyway destroys exactly the state the veto
    // exists to protect — the same silent loss, reached by a different route.
    render(
      <ErrorNotice
        message="name already in use"
        askAgent
        beforeHandoff={() => { throw new Error('stash exploded') }}
      />,
    )
    await userEvent.click(screen.getByRole('button', { name: /agent/i }))
    expect(navigated).toEqual([])
  })

  it('abandons the hand-off when the caller reports it could not persist', async () => {
    // Navigating would unmount the form the callback just failed to save. A
    // hand-off that costs the nine typed fields is worse than no hand-off.
    render(
      <ErrorNotice message="name already in use" askAgent beforeHandoff={() => false} />,
    )
    await userEvent.click(screen.getByRole('button', { name: /agent/i }))
    expect(navigated).toEqual([])
    expect(consumeChatHandoff()).toBeNull()
  })

  it('does not navigate when the prompt itself could not be staged', async () => {
    // Storage dead: the prompt never lands, so a navigation would unmount the
    // surface and deliver the user to an empty chat — losing context for nothing.
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('quota')
    })
    render(<ErrorNotice message="disk is full" askAgent />)
    await userEvent.click(screen.getByRole('button', { name: /agent/i }))
    expect(navigated).toEqual([])
  })
})

describe('reportInstanceFailure', () => {
  it('journals the ladder as an ordered chain with the verdict as the code', () => {
    const returned = reportInstanceFailure({
      id: 'cd-1',
      name: 'Box',
      transport: 'ssh',
      status: brokenStatus(),
      stage: 'connect',
      fallbackMessage: '',
    })
    expect(returned?.message).toBe('tunnel failed')
    const [report] = recentErrors()
    // The object handed to the button IS the journalled report, not a lookup of it.
    expect(returned).toBe(report)
    expect(report.source).toBe('system')
    expect(report.code).toBe('remote_down')
    // The chain is the actionable part: ssh ok + remote dashboard failed names a
    // different repair than ssh failed does.
    expect(report.detail).toContain('probes: ssh=ok -> remote_dashboard=FAILED')
    expect(report.detail).toContain('transport: ssh')
  })

  it('records once per distinct failure, so one down crew cannot flush the journal', () => {
    const args = {
      id: 'cd-1',
      name: 'Box',
      transport: 'ssh',
      status: brokenStatus(),
      stage: 'connect' as const,
      fallbackMessage: '',
    }
    reportInstanceFailure(args)
    reportInstanceFailure(args)
    reportInstanceFailure(args)
    expect(recentErrors()).toHaveLength(1)
    // A different verdict is a different failure and is reported again.
    reportInstanceFailure({
      ...args,
      status: brokenStatus({
        error: '',
        diagnosis: {
          code: 'ssh_unreachable',
          ok: false,
          reason: "Can't SSH to the host",
          probes: [{ name: 'ssh', ok: false }],
        },
      }),
    })
    expect(recentErrors()).toHaveLength(2)
    expect(recentErrors()[0].code).toBe('ssh_unreachable')
  })

  it('reports the watchdog case, which carries no backend error string', () => {
    // The tunnel says connected while the embedded pane never loaded — the one
    // failure with no self-evident cause, so it must not be the one with no report.
    const returned = reportInstanceFailure({
      id: 'cd-1',
      name: 'Box',
      transport: 'ssm',
      status: { instance_id: 'cd-1', state: 'connected' },
      stage: 'pane_load',
      fallbackMessage: 'The pane failed to load',
    })
    expect(returned?.message).toBe('The pane failed to load')
    expect(recentErrors()[0].detail).toContain('stage: pane_load')
  })

  it('binds the prompt to THIS crew even when two crews fail identically', () => {
    // Two stock installs whose hosts are both unreachable produce byte-identical
    // prose. A message-text lookup resolves both to whichever was journalled last,
    // so the prompt would carry the other crew's name, transport and probes.
    const shared = brokenStatus({ error: 'ssh: Could not resolve hostname' })
    const first = reportInstanceFailure({
      id: 'cd-1', name: 'Box One', transport: 'ssh', status: shared,
      stage: 'connect', fallbackMessage: '',
    })
    const second = reportInstanceFailure({
      id: 'cd-2', name: 'Box Two', transport: 'ssm', status: shared,
      stage: 'connect', fallbackMessage: '',
    })
    expect(first?.message).toBe(second?.message)
    expect(first?.detail).toContain('Box One (cd-1)')
    expect(second?.detail).toContain('Box Two (cd-2)')
    // Identity lives in the returned object, not in a lookup key.
    expect(first?.id).not.toBe(second?.id)
  })

  it('hands back the suppressed report on a de-dup hit, and survives eviction', () => {
    // Declining to journal a duplicate must not mean withholding the diagnostic:
    // the caller would otherwise be left with no report once the first one has
    // aged out of the 20-deep journal.
    const args = {
      id: 'cd-1', name: 'Box', transport: 'ssh', status: brokenStatus(),
      stage: 'connect' as const, fallbackMessage: '',
    }
    const first = reportInstanceFailure(args)
    __resetErrorJournalForTests() // stand in for the entry aging out
    const again = reportInstanceFailure(args)
    expect(again).toBe(first)
    expect(again?.code).toBe('remote_down')
  })

  it('never labels a failure with a stale healthy verdict', () => {
    // The stored diagnosis is the last ladder RUN, so an `ok` left over from
    // before the failure must not become the text of the failure.
    const status: InstanceTunnelStatus = {
      instance_id: 'cd-1',
      state: 'error',
      error: '',
      diagnosis: { code: 'ok', ok: true, reason: 'All checks passed', probes: [] },
    }
    const report = reportInstanceFailure({
      id: 'cd-1', name: 'Box', transport: 'ssh', status,
      stage: 'pane_load', fallbackMessage: 'The pane failed to load',
    })
    expect(report?.message).toBe('The pane failed to load')
    expect(report?.code).toBeUndefined()
  })

  it('gives each stage its own de-dup entry, so two surfaces do not ping-pong the journal', () => {
    // The Settings row always reports `connect` while the viewport reports either
    // stage depending on its pane watchdog, both for the same crew. On a shared
    // per-id entry each write reads the other's signature, mismatches, and
    // journals again — so one persistently-down crew floods a 20-deep journal on
    // every poll, which is the exact eviction de-duplication exists to prevent.
    const base = {
      id: 'cd-1', name: 'Box', transport: 'ssh', status: brokenStatus(),
      fallbackMessage: '',
    }
    reportInstanceFailure({ ...base, stage: 'connect' })
    reportInstanceFailure({ ...base, stage: 'pane_load' })
    expect(recentErrors()).toHaveLength(2)
    // Three more alternating rounds must add nothing: each stage is a repeat of
    // its OWN last report, not of the other stage's.
    for (let i = 0; i < 3; i++) {
      reportInstanceFailure({ ...base, stage: 'connect' })
      reportInstanceFailure({ ...base, stage: 'pane_load' })
    }
    expect(recentErrors()).toHaveLength(2)
  })

  it('re-reports when only the probe ladder changed, never handing back a stale one', () => {
    // The probes name WHICH link is broken, and they move while the verdict code
    // and the message stay byte-identical. A signature that omits them treats the
    // new state as a repeat and returns the cached report, so the prompt blames
    // the wrong link.
    const statusWith = (probes: { name: string; ok: boolean }[]): InstanceTunnelStatus => ({
      instance_id: 'cd-1',
      state: 'error',
      error: 'Remote dashboard did not answer',
      diagnosis: { code: 'remote_down', ok: false, reason: 'Remote dashboard down', probes },
    })
    const base = { id: 'cd-1', name: 'Box', transport: 'ssh', stage: 'connect' as const, fallbackMessage: '' }
    const first = reportInstanceFailure({
      ...base,
      status: statusWith([{ name: 'ssh', ok: true }, { name: 'remote_dashboard', ok: false }]),
    })
    const second = reportInstanceFailure({
      ...base,
      status: statusWith([{ name: 'ssh', ok: false }, { name: 'remote_dashboard', ok: false }]),
    })
    expect(second).not.toBe(first)
    expect(second?.detail).toContain('ssh=FAILED')
    expect(recentErrors()).toHaveLength(2)
  })

  it('clears the signature on recovery, so an identical later failure is reported again', () => {
    // The de-dup map is what suppresses a repeat. If recovery never clears it, the
    // same failure recurring after the first report has aged out of the journal
    // reaches the button with no report behind it — no code, no probes.
    const args = {
      id: 'cd-1',
      name: 'Box',
      transport: 'ssh',
      status: brokenStatus(),
      stage: 'connect' as const,
      fallbackMessage: '',
    }
    reportInstanceFailure(args)
    expect(recentErrors()).toHaveLength(1)
    // Recovered: callers pass the healthy status rather than skipping the call.
    reportInstanceFailure({ ...args, status: { instance_id: 'cd-1', state: 'connected' } })
    __resetErrorJournalForTests() // stand in for the entry aging out of the journal
    reportInstanceFailure(args)
    expect(recentErrors()).toHaveLength(1)
    expect(recentErrors()[0].code).toBe('remote_down')
  })

  it('records nothing when there is no failure to describe', () => {
    expect(
      reportInstanceFailure({
        id: 'cd-1',
        name: 'Box',
        transport: 'ssh',
        status: { instance_id: 'cd-1', state: 'connected' },
        stage: 'connect',
        fallbackMessage: '',
      }),
    ).toBeNull()
    expect(recentErrors()).toHaveLength(0)
  })
})
