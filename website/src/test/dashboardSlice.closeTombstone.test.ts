/**
 * Close tombstone: a session being closed must not flicker back into the
 * sidebar when an authoritative slot list that predates the server-side pop
 * lands while (or just after) the DELETE is in flight (#11224).
 *
 * The thunk is not run here -- these drive the reducer with the exact action
 * sequence `deleteSlot` emits (`pending` -> `removeSlotOptimistic` -> await ->
 * `fulfilled` | `rejected`) interleaved with `sseSlots` / `fetchSlots.fulfilled`
 * frames, which is the only thing the contract is about.
 */
import reducer, {
  sseSlots,
  fetchSlots,
  addSlotOptimistic,
  removeSlotOptimistic,
  sseSubagentStatus,
} from '../store/dashboardSlice'
import type { ChatSlot } from '../types'

vi.mock('../api/client', () => ({
  api: { chatSlots: vi.fn(), chatMode: vi.fn() },
}))

const row = (key: string, extra: Partial<ChatSlot> = {}): ChatSlot => ({
  key, title: key, messages: 1, running: false, pending_approval: false, waiting_for_input: false, last_activity_ts: undefined, ...extra,
})
const A = row('chat-a')
const B = row('chat-b')
const C = row('chat-c')

const pending = (key: string) => ({ type: 'chat/deleteSlot/pending', meta: { arg: key, requestId: 'r', requestStatus: 'pending' } })
const fulfilled = (key: string) => ({ type: 'chat/deleteSlot/fulfilled', meta: { arg: key, requestId: 'r', requestStatus: 'fulfilled' }, payload: key })
const rejected = (key: string) => ({ type: 'chat/deleteSlot/rejected', meta: { arg: key, requestId: 'r', requestStatus: 'rejected' }, error: { message: 'save failed' } })
const httpReply = (slots: ChatSlot[]) => ({ type: fetchSlots.fulfilled.type, payload: slots, meta: { requestId: 'h', requestStatus: 'fulfilled' } })

const keys = (s: { slots: ChatSlot[] }) => s.slots.map(x => x.key)

/** A live tab: the first snapshot has landed, so later frames are authoritative. */
function live() {
  return reducer(reducer(undefined, { type: '@@INIT' }), sseSlots([A, B, C]))
}

/** Arm a close of B the way the thunk does, up to (not including) the await. */
function closing(state = live()) {
  state = reducer(state, pending('chat-b'))
  return reducer(state, removeSlotOptimistic('chat-b'))
}

describe('dashboardSlice close tombstone', () => {
  it('starts with no closing keys', () => {
    expect(reducer(undefined, { type: '@@INIT' }).closingSlots).toEqual({})
  })

  it('holds the closing row out of a live frame that still lists it (the flicker)', () => {
    let s = closing()
    expect(keys(s)).toEqual(['chat-a', 'chat-c'])
    // Server has not popped yet: a coalesced push re-serializes the full registry.
    s = reducer(s, sseSlots([A, B, C]))
    expect(keys(s)).toEqual(['chat-a', 'chat-c'])
    // ...and a stale HTTP reply assembled before the click says the same.
    s = reducer(s, httpReply([A, B, C]))
    expect(keys(s)).toEqual(['chat-a', 'chat-c'])
  })

  it('does not delay the rest of the frame: other rows, generation, unread drain', () => {
    let s = closing()
    const gen = s.slotsGeneration
    s = reducer(s, sseSlots([A, B, { ...C, title: 'renamed' }]))
    expect(s.slots.find(x => x.key === 'chat-c')?.title).toBe('renamed')
    expect(s.slotsGeneration).toBe(gen + 1)
  })

  it('releases the hold on the first authoritative frame that omits the key', () => {
    let s = closing()
    s = reducer(s, sseSlots([A, C]))
    expect(s.closingSlots).toEqual({})
    // A same-key session recreated LATER is ordinary membership again.
    s = reducer(s, sseSlots([A, C, B]))
    expect(keys(s)).toEqual(['chat-a', 'chat-c', 'chat-b'])
  })

  it('keeps holding after the 200 while straggler frames still list the key', () => {
    let s = closing()
    s = reducer(s, fulfilled('chat-b'))
    // The response can beat a coalesced frame serialized before the pop.
    s = reducer(s, sseSlots([A, B, C]))
    expect(keys(s)).toEqual(['chat-a', 'chat-c'])
    s = reducer(s, sseSlots([A, C]))
    expect(s.closingSlots).toEqual({})
  })

  it('yields to membership after a bounded number of post-confirmation frames', () => {
    let s = closing()
    s = reducer(s, fulfilled('chat-b'))
    for (let i = 0; i < 3; i++) {
      s = reducer(s, sseSlots([A, B, C]))
      expect(keys(s)).toEqual(['chat-a', 'chat-c'])
    }
    // A server that still lists a slot it confirmed closed has kept it alive;
    // hiding it forever would lose the user a session. Show it.
    s = reducer(s, sseSlots([A, B, C]))
    expect(keys(s)).toEqual(['chat-a', 'chat-b', 'chat-c'])
    expect(s.closingSlots).toEqual({})
  })

  it('holds unboundedly while the request is still in flight', () => {
    let s = closing()
    for (let i = 0; i < 20; i++) s = reducer(s, sseSlots([A, B, C]))
    expect(keys(s)).toEqual(['chat-a', 'chat-c'])
  })

  it('releases on rejection so the refetch can put the row back', () => {
    let s = closing()
    s = reducer(s, rejected('chat-b'))
    expect(s.closingSlots).toEqual({})
    s = reducer(s, httpReply([A, B, C]))
    expect(keys(s)).toEqual(['chat-a', 'chat-b', 'chat-c'])
  })

  it('is superseded by a same-key optimistic add (resume / fork) and by createSlot', () => {
    let s = closing()
    s = reducer(s, addSlotOptimistic(B))
    expect(s.closingSlots).toEqual({})
    expect(keys(s)).toContain('chat-b')

    s = closing()
    s = reducer(s, { type: 'chat/createSlot/fulfilled', payload: B, meta: { requestId: 'c', requestStatus: 'fulfilled', arg: {} } })
    expect(s.closingSlots).toEqual({})
    expect(keys(s)).toContain('chat-b')
  })

  it('tracks concurrent closes independently', () => {
    let s = closing()
    s = reducer(s, pending('chat-c'))
    s = reducer(s, removeSlotOptimistic('chat-c'))
    s = reducer(s, sseSlots([A, B, C]))
    expect(keys(s)).toEqual(['chat-a'])
    s = reducer(s, sseSlots([A, B]))
    expect(keys(s)).toEqual(['chat-a'])
    expect(Object.keys(s.closingSlots)).toEqual(['chat-b'])
  })

  it('does not evict a held slot\'s subagent state before the server confirms', () => {
    // Existing invariant: only an authoritative list that OMITS the key may
    // tear down sub-agent state, because a failed close leaves the slot live.
    let s = live()
    s = reducer(s, sseSubagentStatus({ slot: 'chat-b', running: 1 } as never))
    s = closing(s)
    s = reducer(s, sseSlots([A, B, C]))
    expect(s.subagentRunning['chat-b']).toBeDefined()
    s = reducer(s, sseSlots([A, C]))
    expect(s.subagentRunning['chat-b']).toBeUndefined()
  })

  it('keeps row identity for untouched rows while a hold filters the frame', () => {
    let s = closing()
    const a0 = s.slots[0]
    s = reducer(s, sseSlots([A, B, C]))
    expect(s.slots[0]).toBe(a0)
  })
})
