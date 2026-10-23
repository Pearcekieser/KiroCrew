/**
 * A create the user moved away from leaves the user where they are.
 *
 *   - "New session with token" stages its token only in the new session's
 *     composer. A user who opened session B while the create ran keeps B's
 *     composer as it was.
 *   - Alt+Shift+N brings the user to the new chat from another page, but not
 *     after they went to another page while the create ran, and not by an
 *     extra `/chat` push when they are already on the chat page.
 *   - Two Alt+Shift+N presses, or two Shift+clicks on the embed "+", during one
 *     create make one session.
 *   - An embed Shift+click create the user switched tabs away from adds its tab
 *     behind the one the user picked.
 *
 * The create runs through the real `createSlot` thunk with the API held
 * pending, so the switched-away decision is the reducer's own.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, renderHook, screen, fireEvent, act, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import type { ChatSlot } from '../types'

const navigateSpy = vi.hoisted(() => vi.fn())
vi.mock('react-router-dom', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useNavigate: () => navigateSpy,
}))

const mocks = vi.hoisted(() => ({ createChatSlot: vi.fn() }))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (target, prop: string) => (prop in target ? target[prop] : vi.fn().mockResolvedValue([])),
  }),
}))

// The shell hook's unrelated collaborators: agent lists, instance panes and
// the palette trigger read the network or the provider context.
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: '' }) }))
vi.mock('../hooks/useInstanceShortcuts', () => ({ useInstanceShortcuts: () => {} }))
vi.mock('../hooks/useAutoConnectInstances', () => ({ useAutoConnectInstances: () => {} }))
vi.mock('../hooks/useCommandPalette', () => ({ useCommandPalette: () => ({ open: false, setOpen: () => {} }) }))
vi.mock('../providers/context', () => ({ useProvider: () => ({}) }))

import { setActiveSlot } from '../store/chatSlice'
import { usePaletteActions } from '../components/commandPalette/paletteActions'
import { useShellKeyboard } from '../shell/shortcuts/shellKeyboard'
import EmbedTabStrip from '../components/EmbedTabStrip'

const ORIGIN = 'chat-a'
const OTHER = 'chat-b'
const NEW = 'chat-new'

function deferred<T>() {
  let resolve: (v: T) => void = () => {}
  const promise = new Promise<T>(r => { resolve = r })
  return { promise, resolve }
}

function makeStore(activeSlot: string | null = ORIGIN) {
  const defaults = createTestStore().getState()
  const slots = [
    { key: ORIGIN, title: 'A', messages: 1, running: false },
    { key: OTHER, title: 'B', messages: 1, running: false },
  ] as ChatSlot[]
  return createTestStore({
    dashboard: { ...defaults.dashboard, slots, slotsLoaded: true },
    chat: { ...defaults.chat, activeSlot },
  })
}

function wrapper(store: ReturnType<typeof makeStore>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <MemoryRouter>{children}</MemoryRouter>
      </Provider>
    </QueryClientProvider>
  )
}

/** Put the address bar on `path`, which `createChatForView` reads. */
const goTo = (path: string) => window.history.pushState(null, '', path)

let held: ReturnType<typeof deferred<unknown>>

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  navigateSpy.mockClear()
  held = deferred<unknown>()
  mocks.createChatSlot.mockReset().mockReturnValue(held.promise)
})

afterEach(() => {
  goTo('/')
})

describe('New session with token', () => {
  function harness() {
    const store = makeStore(null)
    goTo('/settings')
    const hook = renderHook(() => usePaletteActions(), { wrapper: wrapper(store) })
    return { store, ...hook }
  }

  it('keeps the token out of a session the user opened during the create', async () => {
    const { store, result } = harness()
    act(() => result.current.newSessionWithToken('$zzq-skill'))
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    act(() => { store.dispatch(setActiveSlot(OTHER)) })
    await act(async () => { held.resolve({ key: NEW }) })
    expect(store.getState().chat.activeSlot).toBe(OTHER)
    expect(store.getState().chat.pendingInput).toBeNull()
    expect(navigateSpy).not.toHaveBeenCalled()
  })

  it('stages the token when the chat page rewrites its own slug for the new session', async () => {
    const store = makeStore(ORIGIN)
    goTo(`/chat/slug-a?sid=${ORIGIN}`)
    const { result } = renderHook(() => usePaletteActions(), { wrapper: wrapper(store) })
    act(() => result.current.newSessionWithToken('@zzq-prompt'))
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    // The URL sync names the new session before the create's caller resumes.
    goTo(`/chat/new-session?sid=${NEW}`)
    await act(async () => { held.resolve({ key: NEW }) })
    await waitFor(() => expect(store.getState().chat.pendingInput).toBe('@zzq-prompt'))
    // Already on the chat page: no extra `/chat` entry for Back to land on.
    expect(navigateSpy).not.toHaveBeenCalled()
  })

  it('stages the token and opens the chat when the user stayed', async () => {
    const { store, result } = harness()
    act(() => result.current.newSessionWithToken('$zzq-skill'))
    await act(async () => { held.resolve({ key: NEW }) })
    await waitFor(() => expect(store.getState().chat.pendingInput).toBe('$zzq-skill'))
    expect(store.getState().chat.activeSlot).toBe(NEW)
    expect(navigateSpy).toHaveBeenCalledWith('/chat')
  })
})

describe('Alt+Shift+N', () => {
  function harness() {
    const store = makeStore(ORIGIN)
    renderHook(() => useShellKeyboard({
      toggleFocusMode: () => {}, toggleNav: () => {}, terminalEnabled: false, isPopout: false,
      isEmbed: false, terminalPoppedOut: false, activeSlotProject: undefined,
    }), { wrapper: wrapper(store) })
    return store
  }
  const press = () => fireEvent.keyDown(document, { code: 'KeyN', altKey: true, shiftKey: true })

  it('leaves a user on Settings who went there during the create', async () => {
    goTo(`/chat?sid=${ORIGIN}`)
    harness()
    press()
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    goTo('/settings')
    await act(async () => { held.resolve({ key: NEW }) })
    expect(navigateSpy).not.toHaveBeenCalled()
  })

  it('brings a user who pressed it on Settings to the new chat', async () => {
    goTo('/settings')
    const store = harness()
    press()
    await act(async () => { held.resolve({ key: NEW }) })
    await waitFor(() => expect(navigateSpy).toHaveBeenCalledWith('/chat'))
    expect(store.getState().chat.activeSlot).toBe(NEW)
  })

  it('adds no history entry of its own on the chat page', async () => {
    goTo(`/chat?sid=${ORIGIN}`)
    const store = harness()
    press()
    await act(async () => { held.resolve({ key: NEW }) })
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW))
    expect(navigateSpy).not.toHaveBeenCalled()
  })

  it('makes one session for two presses during one create', async () => {
    goTo(`/chat?sid=${ORIGIN}`)
    harness()
    press()
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    press()
    await act(async () => { held.resolve({ key: NEW }) })
    expect(mocks.createChatSlot).toHaveBeenCalledTimes(1)
    // Once the create settles, the next press creates again.
    mocks.createChatSlot.mockResolvedValue({ key: 'chat-new-2' })
    press()
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(2))
  })
})

describe('embed tab strip Shift+click "+"', () => {
  function mount() {
    const store = makeStore(ORIGIN)
    goTo(`/embed/chat/${ORIGIN}`)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <MemoryRouter initialEntries={[`/embed/chat/${ORIGIN}?sid=${ORIGIN}`]}>
            <EmbedTabStrip />
          </MemoryRouter>
        </QueryClientProvider>
      </Provider>,
    )
    navigateSpy.mockClear()
    return store
  }
  const shiftPlus = () => fireEvent.click(screen.getByLabelText('New tab'), { shiftKey: true })

  it('makes one session for two Shift+clicks during one create', async () => {
    mount()
    shiftPlus()
    shiftPlus()
    await act(async () => { held.resolve({ key: NEW }) })
    expect(mocks.createChatSlot).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(navigateSpy).toHaveBeenCalledWith(`/embed/chat/${NEW}?sid=${NEW}`))
  })

  it('keeps the tab a user switched to during the create', async () => {
    const store = mount()
    shiftPlus()
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    // A tab switch moves the address bar and the embedded chat with it.
    goTo(`/embed/chat/${OTHER}`)
    act(() => { store.dispatch(setActiveSlot(OTHER)) })
    navigateSpy.mockClear()
    await act(async () => { held.resolve({ key: NEW }) })
    expect(navigateSpy).not.toHaveBeenCalledWith(`/embed/chat/${NEW}?sid=${NEW}`)
    expect(store.getState().chat.activeSlot).toBe(OTHER)
    expect(JSON.parse(sessionStorage.getItem('kirocrew-embed-tabs') ?? '[]')).toContain(NEW)
  })
})
