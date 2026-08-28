/**
 * LibraryPage — the launchpad grid contract (PR3 App Store split, approved
 * mockup frame #a).
 *
 * The Library list is an icon GRID of LaunchpadTile, one tile per installed
 * app. The tile's pin badge toggles whether the app appears in the sidebar,
 * persisted through the `appNavHidden` module (`mc-app-nav-hidden` +
 * `mc:app-nav-hidden-changed`); the hover action bar carries the management
 * verbs the old card list offered. This file pins that surface:
 *
 *  - one tile per `installedApps` entry;
 *  - the pin toggle WRITES the hidden set and DISPATCHES the sync event
 *    (that event is the only same-tab path to the App.tsx sidebar filter);
 *  - ids absent from storage — and a malformed stored value — read as
 *    pinned, so a fresh install needs no migration;
 *  - a disabled app renders greyscale, carries NO pin badge, and its bar
 *    offers only Enable/Uninstall;
 *  - the hover-bar verbs dispatch to the page's hooks (open/enable/update/
 *    uninstall), not to per-tile logic;
 *  - search filters tiles.
 *
 * `AppsPageW3Coverage.test.tsx` owns the action FAILURE branches, the
 * uninstall dialog internals, and the updates hint row; the sidebar side of
 * the pin contract lives at App level (App.appNavHidden.test.tsx).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'
import {
  APP_NAV_HIDDEN_CHANGED_EVENT, APP_NAV_HIDDEN_KEY,
} from '../lib/appNavHidden'

// --- Mocks -----------------------------------------------------------------
const listApps = vi.fn()
const listRegistry = vi.fn()
const listRegistries = vi.fn()
const enableApp = vi.fn()
const disableApp = vi.fn()
const updateApp = vi.fn()
const uninstallApp = vi.fn()
const uninstallPreview = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    listApps: (...a: unknown[]) => listApps(...a),
    listRegistry: (...a: unknown[]) => listRegistry(...a),
    listRegistries: (...a: unknown[]) => listRegistries(...a),
    updateRegistries: vi.fn(),
    refreshRegistries: vi.fn(),
    enableApp: (...a: unknown[]) => enableApp(...a),
    disableApp: (...a: unknown[]) => disableApp(...a),
    updateApp: (...a: unknown[]) => updateApp(...a),
    uninstallApp: (...a: unknown[]) => uninstallApp(...a),
    uninstallPreview: (...a: unknown[]) => uninstallPreview(...a),
    installApp: vi.fn(),
    openApp: vi.fn(),
    trustApp: vi.fn(),
    untrustApp: vi.fn(),
    getApp: vi.fn(),
  },
}))

vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'dark' }) }))

import LibraryPage from '../pages/apps/LibraryPage'

/** Route probe for tile-face / hover-bar navigation. */
function RouteProbe() {
  const loc = useLocation()
  return <div data-testid="route-probe" data-path={loc.pathname} />
}

/** An installed, enabled third-party app with one UI page (nav id `app-<name>`). */
function installedApp(name: string, displayName: string, over: Record<string, unknown> = {}) {
  return {
    name, displayName, version: '1.0.0', enabled: true,
    installedAt: '2026-08-01T00:00:00Z', origin: 'registry', resources: 'gateway', lifecycle: 'gateway',
    manifest: {
      name, version: '1.0.0', displayName, description: `${displayName} does things.`,
      author: 'zezhexu', tags: [name],
      ui: { pages: [{ route: `/${name}-ui`, label: displayName, icon: 'Bot' }] },
    },
    ...over,
  }
}

const SECRETARY = installedApp('secretary', 'Secretary')
const RADAR = installedApp('oncall-radar', 'Oncall Radar')

function renderLibrary() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/apps/library']}>
        <Routes>
          <Route path="/apps/library" element={<LibraryPage />} />
          <Route path="*" element={<RouteProbe />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const tile = (name: string) => screen.findByTestId(`launchpad-tile-${name}`)

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  sessionStorage.clear()
  listApps.mockResolvedValue([SECRETARY, RADAR])
  listRegistry.mockResolvedValue({ apps: [] })
  listRegistries.mockResolvedValue({ registries: [] })
  enableApp.mockResolvedValue({ ok: true })
  disableApp.mockResolvedValue({ ok: true })
  updateApp.mockResolvedValue({ ok: true })
  uninstallApp.mockResolvedValue({ ok: true })
  uninstallPreview.mockResolvedValue({ dependencies: { removable: [], shared: [], userInstalled: [] } })
})

afterEach(() => {
  localStorage.clear()
})

describe('LibraryPage — launchpad grid', () => {
  it('renders one tile per installed app', async () => {
    renderLibrary()
    expect(await tile('secretary')).toBeInTheDocument()
    expect(await tile('oncall-radar')).toBeInTheDocument()
    expect(screen.getAllByTestId(/^launchpad-tile-/)).toHaveLength(2)
  })

  it('search filters tiles down to the matches', async () => {
    renderLibrary()
    await tile('secretary')
    fireEvent.change(screen.getByLabelText('Search library'), { target: { value: 'radar' } })
    await waitFor(() => expect(screen.queryByTestId('launchpad-tile-secretary')).toBeNull())
    expect(screen.getByTestId('launchpad-tile-oncall-radar')).toBeInTheDocument()
  })
})

describe('LibraryPage — pin badge and persistence', () => {
  it('an id absent from storage defaults to pinned', async () => {
    renderLibrary()
    const t = within(await tile('secretary'))
    const badge = t.getByRole('button', { name: 'Unpin Secretary from the sidebar' })
    expect(badge).toHaveAttribute('aria-pressed', 'true')
    expect(t.getByText('Pinned')).toBeInTheDocument()
  })

  it('a malformed stored value is treated as the empty set (everything pinned)', async () => {
    localStorage.setItem(APP_NAV_HIDDEN_KEY, '{broken json[')
    renderLibrary()
    const t = within(await tile('secretary'))
    expect(t.getByRole('button', { name: 'Unpin Secretary from the sidebar' }))
      .toHaveAttribute('aria-pressed', 'true')
  })

  it('toggling the pin badge writes mc-app-nav-hidden and dispatches the sync event', async () => {
    const synced = vi.fn()
    window.addEventListener(APP_NAV_HIDDEN_CHANGED_EVENT, synced)
    try {
      renderLibrary()
      const scope = within(await tile('secretary'))
      fireEvent.click(scope.getByRole('button', { name: 'Unpin Secretary from the sidebar' }))

      // Persistence: the sidebar nav id (`app-<name>` for an AppHost-routed
      // app) lands in the HIDDEN set, and same-tab listeners are notified —
      // the only path by which the App.tsx sidebar filter learns of it.
      expect(JSON.parse(localStorage.getItem(APP_NAV_HIDDEN_KEY)!)).toEqual(['app-secretary'])
      expect(synced).toHaveBeenCalled()

      // The tile repaints from the event: hollow badge, unpinned caption.
      const badge = await scope.findByRole('button', { name: 'Pin Secretary to the sidebar' })
      expect(badge).toHaveAttribute('aria-pressed', 'false')
      expect(scope.getByText('Not pinned')).toBeInTheDocument()

      // The other tile's pin state is untouched.
      expect(within(screen.getByTestId('launchpad-tile-oncall-radar'))
        .getByRole('button', { name: 'Unpin Oncall Radar from the sidebar' })).toBeInTheDocument()

      // Toggling back empties the stored set again.
      fireEvent.click(badge)
      expect(JSON.parse(localStorage.getItem(APP_NAV_HIDDEN_KEY)!)).toEqual([])
      await scope.findByRole('button', { name: 'Unpin Secretary from the sidebar' })
    } finally {
      window.removeEventListener(APP_NAV_HIDDEN_CHANGED_EVENT, synced)
    }
  })

  it('the hover bar Unpin action toggles the same persisted state as the badge', async () => {
    renderLibrary()
    const scope = within(await tile('secretary'))
    fireEvent.click(scope.getByRole('button', { name: 'Unpin' }))
    expect(JSON.parse(localStorage.getItem(APP_NAV_HIDDEN_KEY)!)).toEqual(['app-secretary'])
    expect(await scope.findByRole('button', { name: 'Pin' })).toBeInTheDocument()
  })
})

describe('LibraryPage — disabled tiles', () => {
  beforeEach(() => {
    listApps.mockResolvedValue([installedApp('secretary', 'Secretary', { enabled: false }), RADAR])
  })

  it('renders greyscale without a pin badge, and the bar offers Enable/Uninstall only', async () => {
    renderLibrary()
    const el = await tile('secretary')
    const scope = within(el)
    // Greyscale icon at reduced opacity (mockup frame #a's disabled state).
    expect(el.querySelector('.grayscale.opacity-45')).not.toBeNull()
    expect(scope.getByText('Disabled')).toBeInTheDocument()
    // No pin badge and no Pin/Unpin bar action: a disabled app is not in the
    // sidebar regardless of the stored pin, so a toggle would be a lie.
    expect(scope.queryByRole('button', { name: /the sidebar$/ })).toBeNull()
    expect(scope.queryByRole('button', { name: 'Unpin' })).toBeNull()
    expect(scope.queryByRole('button', { name: 'Pin' })).toBeNull()
    // Bar verbs: Enable and Uninstall, not Disable/Open.
    expect(scope.getByRole('button', { name: 'Enable' })).toBeInTheDocument()
    expect(scope.getByRole('button', { name: 'Uninstall' })).toBeInTheDocument()
    expect(scope.queryByRole('button', { name: 'Disable' })).toBeNull()
    expect(scope.queryByRole('button', { name: 'Open' })).toBeNull()
    // The enabled sibling keeps its badge — the suppression is per-tile.
    expect(within(screen.getByTestId('launchpad-tile-oncall-radar'))
      .getByRole('button', { name: 'Unpin Oncall Radar from the sidebar' })).toBeInTheDocument()
  })

  it('Enable on the bar dispatches to the enable hook', async () => {
    renderLibrary()
    fireEvent.click(within(await tile('secretary')).getByRole('button', { name: 'Enable' }))
    await waitFor(() => expect(enableApp).toHaveBeenCalledWith('secretary'))
  })
})

describe('LibraryPage — hover-bar action dispatch', () => {
  it('Open navigates to the app’s nav-target route', async () => {
    renderLibrary()
    fireEvent.click(within(await tile('secretary')).getByRole('button', { name: 'Open' }))
    expect(await screen.findByTestId('route-probe')).toHaveAttribute('data-path', '/apps/secretary')
  })

  it('Disable dispatches to the disable API', async () => {
    renderLibrary()
    fireEvent.click(within(await tile('secretary')).getByRole('button', { name: 'Disable' }))
    await waitFor(() => expect(disableApp).toHaveBeenCalledWith('secretary'))
  })

  it('Update goes through the shared useAppUpdates hook (in place for a path install)', async () => {
    listApps.mockResolvedValue([
      installedApp('secretary', 'Secretary', { origin: 'local', source: '/home/u/apps/secretary' }),
    ])
    listRegistry.mockResolvedValue({
      apps: [{
        name: 'secretary', displayName: 'Secretary', author: 'zezhexu', version: '1.1.0',
        description: 'x', tags: [], installed: true, updateAvailable: true, provenance: 'external',
      }],
    })
    renderLibrary()
    fireEvent.click(within(await tile('secretary')).getByRole('button', { name: 'Update' }))
    await waitFor(() => expect(updateApp).toHaveBeenCalledWith('secretary'))
  })

  it('Uninstall opens the confirmation dialog; confirming calls the uninstall API', async () => {
    renderLibrary()
    fireEvent.click(within(await tile('secretary')).getByRole('button', { name: 'Uninstall' }))
    // The page intercepts the verb into its confirmation dialog — nothing is
    // uninstalled until the dialog's own button confirms.
    const dialog = await screen.findByRole('dialog', { name: 'Confirm uninstall' })
    expect(uninstallApp).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Uninstall' }))
    await waitFor(() =>
      expect(uninstallApp).toHaveBeenCalledWith('secretary', true, false, []))
  })
})

describe('LaunchpadTile — the disabled gate is the tile’s own branch', () => {
  // Through LibraryPage, `pinnable` is already false for a disabled app
  // (appNavTarget returns null when !enabled), which MASKS the tile's own
  // `!disabled` guard on the pin badge. This direct render pins that guard
  // independently: even when a caller claims pinnable, a disabled app must
  // not offer a pin toggle (mutation-testing found the page-level tests
  // alone could not kill an inverted `!disabled`).
  it('hides the pin badge for a disabled app even when the caller passes pinnable', async () => {
    const { default: LaunchpadTile } = await import('../pages/apps/LaunchpadTile')
    render(
      <MemoryRouter>
        <LaunchpadTile
          app={installedApp('secretary', 'Secretary', { enabled: false }) as never}
          pinned
          pinnable
          actionLoading={null}
          onTogglePin={vi.fn()}
          onAction={vi.fn()}
          onOpen={vi.fn()}
          onDetail={vi.fn()}
        />
      </MemoryRouter>,
    )
    expect(screen.getByTestId('launchpad-tile-secretary')).toBeInTheDocument()
    // Both pin affordances stay suppressed: the top-right badge (aria-label
    // “… the sidebar”) and the hover bar’s Pin/Unpin verb.
    expect(screen.queryByRole('button', { name: /the sidebar$/ })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Unpin' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Pin' })).toBeNull()
  })
})
