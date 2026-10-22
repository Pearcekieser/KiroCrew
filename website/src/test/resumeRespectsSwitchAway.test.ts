/**
 * A resume from Older sessions (or any other resume entry point) that answers
 * after the user moved must leave the user where they are, the rule creates
 * and forks follow (`forkMayOpen`): another session opened meanwhile, or a
 * return to the origin, is a choice the late answer must not undo. The resumed
 * session is still listed as an open tab and leaves Older sessions.
 *
 * Two resumes in a row: the later click wins, so an earlier resume answering
 * after the later one does not take the view back. A repeat of the resume on
 * its way sends nothing. While a resume is pending the chat page knows one is
 * on its way (`pendingResumeKey`), as it knows of a pending create or fork.
 *
 * The real `resumeFromHistory` thunk runs against a deferred
 * `api.resumeChatSlot`, one per history key.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import type { ChatSlot } from '../types'

const mocks = vi.hoisted(() => ({ resumeChatSlot: vi.fn() }))
vi.mock('../api/client', async () => {
  const { ApiError } = await import('../api/apiError')
  return {
    SEARCH_MIN_CHARS: 2,
    ApiError,
    api: new Proxy(mocks as Record<string, unknown>, {
      get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue([])),
    }),
  }
})

import { store } from '../store'
import { sseSlots } from '../store/dashboardSlice'
import { dropActiveSlot, resumeFromHistory, setActiveSlot, switchSlot } from '../store/chatSlice'

const ORIGIN = 'chat-resume-origin'
const OTHER = 'chat-resume-other'
const R1 = 'chat-resume-r1'
const R2 = 'chat-resume-r2'

// One deferred answer per history key; `land(key)` resolves it.
const pending = new Map<string, (v: unknown) => void>()
function land(key: string) {
  const resolve = pending.get(key)
  if (!resolve) throw new Error(`no resume pending for ${key}`)
  pending.delete(key)
  resolve({ ok: true, key, mode: 'orchestrator', messages: [] })
}

const activeSlot = () => store.getState().chat.activeSlot
const listed = (key: string) => store.getState().dashboard.slots.some(s => s.key === key)
const inHistory = (key: string) => store.getState().chat.history.some(s => s.key === key)
const resume = (key: string) => store.dispatch(resumeFromHistory({ key, title: key }))

beforeEach(() => {
  mocks.resumeChatSlot.mockImplementation((key: string) => new Promise(r => { pending.set(key, r) }))
  const slot = (key: string): ChatSlot => ({ key, title: key, messages: 2, running: false, folder_id: '' })
  store.dispatch(sseSlots([slot(ORIGIN), slot(OTHER)]))
  store.dispatch(setActiveSlot(ORIGIN))
  store.dispatch({ type: 'chat/fetchHistory/fulfilled', payload: { sessions: [R1, R2].map(key => ({ key, title: key })), hasMore: false, offset: 0, append: false } })
})
afterEach(() => {
  vi.clearAllMocks()
  pending.clear()
  store.dispatch(setActiveSlot(null))
  store.dispatch(sseSlots([]))
})

describe('resumeFromHistory resolving after the user moved', () => {
  it('leaves the user on the session they opened while the resume ran', async () => {
    const resumed = resume(R1)
    await vi.waitFor(() => expect(mocks.resumeChatSlot).toHaveBeenCalled())
    store.dispatch(setActiveSlot(OTHER))
    land(R1)
    await resumed

    expect(activeSlot()).toBe(OTHER)
    expect(listed(R1)).toBe(true)
    expect(inHistory(R1)).toBe(false)
  })

  it('leaves the user on the origin when they went away and came back while the resume ran', async () => {
    const resumed = resume(R1)
    await vi.waitFor(() => expect(mocks.resumeChatSlot).toHaveBeenCalled())
    store.dispatch(setActiveSlot(OTHER))
    store.dispatch(setActiveSlot(ORIGIN))
    land(R1)
    await resumed

    expect(activeSlot()).toBe(ORIGIN)
    expect(listed(R1)).toBe(true)
  })

  it('opens the later of two resumes when the earlier one answers last', async () => {
    const first = resume(R1)
    const second = resume(R2)
    await vi.waitFor(() => expect(mocks.resumeChatSlot).toHaveBeenCalledTimes(2))
    land(R2)
    await second
    expect(activeSlot()).toBe(R2)
    land(R1)
    await first

    expect(activeSlot()).toBe(R2)
    expect(listed(R1)).toBe(true)
    expect(inHistory(R1)).toBe(false)
  })

  it('sends one resume for a repeated click on the row on its way', async () => {
    const resumed = resume(R1)
    void resume(R1)
    await vi.waitFor(() => expect(mocks.resumeChatSlot).toHaveBeenCalled())
    expect(mocks.resumeChatSlot).toHaveBeenCalledTimes(1)
    expect(store.getState().chat.pendingResumeKey).toBe(R1)
    land(R1)
    await resumed

    expect(activeSlot()).toBe(R1)
    expect(store.getState().chat.pendingResumeKey).toBeNull()
  })

  it('clears the pending resume when it fails', async () => {
    mocks.resumeChatSlot.mockRejectedValueOnce(new Error('boom'))
    await resume(R1)

    expect(store.getState().chat.pendingResumeKey).toBeNull()
    expect(activeSlot()).toBe(ORIGIN)
  })

  // The chat page lost the origin and moved the view itself: no user choice,
  // so the resumed session still opens.
  it('opens the session when the page itself dropped the origin and auto-selected another session', async () => {
    const resumed = resume(R1)
    await vi.waitFor(() => expect(mocks.resumeChatSlot).toHaveBeenCalled())
    store.dispatch(dropActiveSlot())
    void store.dispatch(switchSlot({ key: OTHER, automatic: true }))
    land(R1)
    await resumed

    expect(activeSlot()).toBe(R1)
  })

  it('opens the session when the user stayed on the origin', async () => {
    const resumed = resume(R1)
    await vi.waitFor(() => expect(mocks.resumeChatSlot).toHaveBeenCalled())
    store.dispatch(setActiveSlot(ORIGIN))
    land(R1)
    await resumed

    expect(activeSlot()).toBe(R1)
    expect(inHistory(R1)).toBe(false)
  })
})
