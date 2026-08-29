/**
 * Chat sidebar row memo boundary:
 * each session row is a memo() component that subscribes to its OWN slot's
 * live signals (status line, goal loop, queued sub-agents, workflow runs)
 * slot-scoped. The contract this file pins has two halves, and each half
 * kills a distinct regression:
 *
 *  1. A slotStatusDetail write for ONE slot re-renders only that slot's row —
 *     and that row's visible status text updates. Hoisting the subscription
 *     back to the sidebar shell fails this either way: as a whole-map shell
 *     read it re-renders every row (probe counts explode); as a shell read
 *     behind the memo with unchanged props it re-renders none (the status
 *     text never appears).
 *  2. A shell re-render with unchanged row props re-renders NO rows. Removing
 *     the memo() wrapper fails this: every shell render re-executes all 200+
 *     row bodies, which is the whole-sidebar jank the boundary exists to stop.
 *
 * Render counts are unobservable from the DOM, so the rows report each body
 * execution through the exported sessionRowRenderProbe test seam.
 */
import React from 'react'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import type { RootState } from '../store'
import { ThemeProvider } from '../hooks/useTheme'

// Render framer-motion elements as plain DOM (jsdom can't run projection).
// UNLIKE the sibling sidebar tests' copy of this mock, the made components are
// cached per tag: a Proxy `get` that mints a fresh forwardRef per access gives
// `motion.div` a new element TYPE on every render, which remounts the whole
// subtree under any mocked motion ancestor — remounts execute row bodies
// without ever consulting memo's props comparator, so an uncached mock makes
// the render-count assertions here meaningless.
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  type MockProps = Record<string, unknown> & { children?: React.ReactNode }
  const make = (tag: string) =>
    React.forwardRef((props: MockProps, ref: React.Ref<unknown>) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children') continue
        if (k === 'layoutId') { clean['data-layout-id'] = props[k]; continue }
        if (FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children)
    })
  const cache = new Map<string, unknown>()
  const motion = new Proxy({}, {
    get: (_t, tag: string) => {
      if (!cache.has(tag)) cache.set(tag, make(tag))
      return cache.get(tag)
    },
  })
  return {
    motion,
    AnimatePresence: ({ children }: MockProps) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: MockProps) => React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
// Legacy single-lane list (no tag columns) keeps the rows flat + easy to query.
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: () => vi.fn().mockResolvedValue([]),
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

import ChatSidebar, { sessionRowRenderProbe } from '../pages/ChatSidebar'
import { setSlotStatusDetail } from '../store/chatSlice'

const slot = (key: string, over: Record<string, unknown> = {}) => ({
  key, title: `Session ${key}`, running: false, ...over,
})

// Hoisted so a shell re-render hands rows the SAME references — the boundary
// under test compares props, and a fresh [] per render would be a test bug,
// not a memo bug (ChatPage memoizes these on the real surface).
const EMPTY_UNREAD: string[] = []
const EMPTY_HISTORY: never[] = []
const EMPTY_AGENTS: never[] = []

function renderSidebar() {
  const slots = [slot('k-a'), slot('k-b', { running: true }), slot('k-c')]
  // Partial slice fixtures — the reducers backfill everything else on the
  // first dispatch, so the casts narrow through unknown instead of `any`.
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {},
      subagentQueued: {}, goalLoops: {}, workflowRuns: {},
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  const sidebar = (historyHasMore: boolean) => (
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots} activeSlot={null} unreadSlots={EMPTY_UNREAD}
              history={EMPTY_HISTORY} historyHasMore={historyHasMore} defaultAgent="" installedAgents={EMPTY_AGENTS}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>
  )
  const view = render(sidebar(false))
  return { store, view, sidebar }
}

/** Split harness: `wrap` mounts the providers ONCE; `sidebarBelowProviders`
 *  is the provider-free sidebar element a stateful child can re-render
 *  without touching the provider tree (the production shape). */
function renderSidebarParts() {
  const slots = [slot('k-a'), slot('k-b', { running: true }), slot('k-c')]
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {},
      subagentQueued: {}, goalLoops: {}, workflowRuns: {},
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  const sidebarBelowProviders = (historyHasMore: boolean) => (
    <ChatSidebar
      slots={slots} activeSlot={null} unreadSlots={EMPTY_UNREAD}
      history={EMPTY_HISTORY} historyHasMore={historyHasMore} defaultAgent="" installedAgents={EMPTY_AGENTS}
    />
  )
  const wrap = (children: React.ReactElement) => (
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>{children}</MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>
  )
  return { store, sidebarBelowProviders, wrap }
}

const counts: Record<string, number> = {}
beforeEach(() => {
  localStorage.clear()
  for (const k of Object.keys(counts)) delete counts[k]
  sessionRowRenderProbe.current = (key) => { counts[key] = (counts[key] || 0) + 1 }
})
afterEach(() => {
  sessionRowRenderProbe.current = null
  vi.clearAllMocks()
})

describe('chat sidebar — session row memo boundary', () => {
  it('a one-slot slotStatusDetail write re-renders that row only, and its status text updates', () => {
    const { store, view } = renderSidebar()
    // Mount renders every row at least once; the boundary claim is about
    // what happens AFTER, so start counting from a settled tree.
    expect(counts['k-a']).toBeGreaterThan(0)
    for (const k of Object.keys(counts)) delete counts[k]

    act(() => {
      // A server-supplied status kind passes its text through verbatim
      // (toolStatusLabel), so the assertion is independent of the
      // simplifiedToolNames preference.
      store.dispatch(setSlotStatusDetail({
        slot: 'k-b', kind: 'status', text: 'Poking the build', ts: 1,
      }))
    })

    // The subscription lives inside the row: the running row shows the new
    // status line (kills "memo swallowed the update")…
    expect(view.getByText('Poking the build')).toBeTruthy()
    // …and only that row's body re-executed (kills "subscription re-hoisted
    // to the shell", which re-renders every row per event).
    expect(counts['k-b']).toBeGreaterThan(0)
    expect(counts['k-a']).toBeUndefined()
    expect(counts['k-c']).toBeUndefined()
  })

  it('a shell re-render with unchanged row props re-renders no rows', () => {
    // The prop change must originate BELOW the providers: view.rerender()
    // re-renders the whole wrapper tree, and ThemeProvider mints a fresh
    // context value per render — a context update bypasses memo entirely and
    // re-runs every row, which is a test-harness artifact, not the production
    // shape (a sidebar shell re-render never re-renders the app-root
    // providers). A stateful harness component between the providers and the
    // sidebar reproduces the real case.
    const setters: Array<(v: boolean) => void> = []
    function Harness({ sidebar }: { sidebar: (h: boolean) => React.ReactElement }) {
      const [hasMore, setHasMore] = React.useState(false)
      setters.push(setHasMore)
      return sidebar(hasMore)
    }
    const { sidebarBelowProviders, wrap } = renderSidebarParts()
    render(wrap(<Harness sidebar={sidebarBelowProviders} />))
    for (const k of Object.keys(counts)) delete counts[k]

    // historyHasMore only affects the Older Sessions pane — no row prop moves.
    // The shell re-renders (its props changed); every row must bail out.
    act(() => { setters[setters.length - 1](true) })

    expect(counts).toEqual({})
  })
})

describe('selectSidebarWorkflowActive — hostile session keys', () => {
  it('a "__proto__" session key becomes an own property, never Object.prototype', async () => {
    const { selectSidebarWorkflowActive } = await import('../store/chatSlice')
    const state = {
      chat: {
        workflowRuns: {
          r1: { run_id: 'r1', status: 'running', sessionKey: '__proto__', name: 'evil', phase: 'x' },
        },
      },
    } as never
    const active = selectSidebarWorkflowActive(state)
    // The entry exists as an OWN property of the accumulator…
    expect(Object.prototype.hasOwnProperty.call(active, '__proto__')).toBe(true)
    expect((active as Record<string, { count: number }>)['__proto__'].count).toBe(1)
    // …and the global prototype was not touched.
    expect(({} as { count?: number }).count).toBeUndefined()
  })
})
