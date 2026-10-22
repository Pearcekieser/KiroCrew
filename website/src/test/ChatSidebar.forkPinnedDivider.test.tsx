/**
 * A fork of a pinned session must leave ONE pinned-section divider per lane.
 *
 * A fork of a pinned session is itself pinned, and it lands at the end of the
 * pinned run, which is where the pointer usually rests: on the first unpinned
 * row, just under the source. The hover hold kept that unpinned row at the
 * pixel offset it was found at, which seated it ABOVE the newly pinned fork.
 * The lane then read pinned, unpinned, pinned, unpinned, and the sidebar drew
 * the divider twice.
 *
 * Locks the contract:
 *  (1) While the pointer rests on the first unpinned row, the fork joins the
 *      pinned run and the lane draws one divider (root lane and folder lane).
 *  (2) After the pointer leaves, a folder still draws exactly one divider, so
 *      no divider is left in the DOM.
 *  (3) The hold still works inside a section: an unpinned held row keeps its
 *      seat among unpinned rows that re-sort around it.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { ChatFolder, ChatSlot } from '../types'

// Render framer-motion elements as plain DOM (jsdom can't run projection).
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef<HTMLElement, Record<string, unknown>>((props, ref) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
    })
  const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })
  return {
    motion,
    AnimatePresence: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const mocks = vi.hoisted(() => ({ folders: [] as unknown[] }))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, p: string) => {
      if (p === 'chatFolders') return vi.fn().mockImplementation(() => Promise.resolve(mocks.folders))
      return vi.fn().mockResolvedValue([])
    },
  }),
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})

import ChatSidebar from '../pages/ChatSidebar'
import type { RootState } from '../store'

const PIN = new Date(2026, 0, 15, 12, 0, 0)
const MIN = 60_000
const ago = (ms: number) => new Date(PIN.getTime() - ms).toISOString()

/** A row in folder `fid` ('' for the root lane). Pinned rows carry a stored
 *  rank, as on a gateway where the person has reordered pins; the fork has none
 *  and so follows them, at the end of the pinned run. */
const row = (key: string, fid: string, lastMs: number, pin?: { rank?: number }): ChatSlot => ({
  key, title: key, messages: 1, running: false, mode: '', created: '', last_ts: ago(lastMs),
  pinned: !!pin, ...(pin?.rank != null ? { pin_rank: pin.rank } : {}),
  ...(fid ? { folder_id: fid } : {}),
} as unknown as ChatSlot)

function renderSidebar(slots: ChatSlot[], folders: ChatFolder[] = []) {
  mocks.folders = folders
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' }, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {} } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], folders)
  const tree = (s: ChatSlot[]) => (
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={s} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>
  )
  const { rerender } = render(tree(slots))
  return { rerender: (s: ChatSlot[]) => rerender(tree(s)) }
}

/** Session rows and pinned dividers in document order: '|' is a divider. */
const shape = () =>
  Array.from(document.querySelectorAll('[data-session-row], [data-pinned-divider]'))
    .map(el => (el.hasAttribute('data-pinned-divider') ? '|' : el.getAttribute('data-session-row')))

const hover = (key: string) => {
  const el = document.querySelector(`[data-session-row="${key}"]`)
  if (!el) throw new Error(`no rendered row for ${key}`)
  fireEvent.pointerOver(el, { pointerType: 'mouse', bubbles: true })
}

const leaveSidebar = () => {
  const root = document.querySelector('.sidebar-inner')
  if (!root) throw new Error('no sidebar root')
  fireEvent.pointerLeave(root, { pointerType: 'mouse' })
}

beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('mc-session-stale-collapse-ms', '0')
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(PIN)
})
afterEach(() => {
  vi.clearAllMocks()
  vi.useRealTimers()
})

describe('ChatSidebar – a fork of a pinned session keeps one pinned divider', () => {
  const FOLDERS: ChatFolder[] = [{ id: 'f1', name: 'Alpha', order: 0 } as ChatFolder]

  const world = (fid: string) => [
    row('p0', fid, 10 * MIN, { rank: 0 }),
    row('s0', fid, 20 * MIN, { rank: 1 }),
    row('u1', fid, 30 * MIN),
    row('u0', fid, 40 * MIN),
  ]
  const fork = (fid: string) => row('fork', fid, MIN, {})

  it('root lane: the fork joins the pinned run while the pointer rests below it', () => {
    const { rerender } = renderSidebar(world(''), FOLDERS)
    expect(shape()).toEqual(['p0', 's0', '|', 'u1', 'u0'])

    hover('u1')
    rerender([...world(''), fork('')])

    expect(shape()).toEqual(['p0', 's0', 'fork', '|', 'u1', 'u0'])
  })

  it('folder lane: the fork joins the pinned run while the pointer rests below it', () => {
    const { rerender } = renderSidebar(world('f1'), FOLDERS)
    expect(shape()).toEqual(['p0', 's0', '|', 'u1', 'u0'])

    hover('u1')
    rerender([...world('f1'), fork('f1')])
    expect(shape()).toEqual(['p0', 's0', 'fork', '|', 'u1', 'u0'])
  })

  it('folder lane: no divider is left behind once the pointer leaves', () => {
    const { rerender } = renderSidebar(world('f1'), FOLDERS)
    hover('u1')
    rerender([...world('f1'), fork('f1')])
    leaveSidebar()
    expect(shape()).toEqual(['p0', 's0', 'fork', '|', 'u1', 'u0'])
  })

  it('still holds an unpinned row among unpinned rows that re-sort around it', () => {
    const { rerender } = renderSidebar(world('f1'), FOLDERS)
    hover('u1')
    // u0 becomes the newest unpinned row, so the sort wants it above u1.
    rerender([...world('f1').filter(s => s.key !== 'u0'), row('u0', 'f1', MIN)])
    expect(shape()).toEqual(['p0', 's0', '|', 'u1', 'u0'])
    leaveSidebar()
    expect(shape()).toEqual(['p0', 's0', '|', 'u0', 'u1'])
  })
})
