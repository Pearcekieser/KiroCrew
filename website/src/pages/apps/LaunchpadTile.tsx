/**
 * LaunchpadTile — one app in the Library's launchpad grid (PR3 App Store
 * split, approved mockup frame #a).
 *
 * The tile is the app's identity (the shared AppIconTile through the SAME
 * icon chain UpdatesList/InstalledAppCard use, so an app wears one mark on
 * every surface), the name and a status caption sit below it, and the pin
 * badge at the tile's top right toggles whether the app appears in the
 * sidebar — filled check = pinned, hollow plus = unpinned. Hovering (or
 * focusing anything inside) the tile reveals a floating action bar under it:
 * Unpin/Pin, Open, Disable/Enable, Uninstall, and Update when one is
 * pending. Disabled apps render greyscale at reduced opacity, hide the pin
 * badge (an app absent from the sidebar has nothing to pin), and offer only
 * Enable/Uninstall.
 *
 * Presentational on purpose: pin persistence, enable/disable/uninstall/
 * update behavior, and navigation are the PAGE's hooks (`useAppActions`,
 * `useAppUpdates`, the appNavHidden module) — this component only renders
 * the state it is handed and reports intent through callbacks, so it cannot
 * fork behavior from the card list it replaces.
 *
 * Keyboard: the tile face is a real button (opens the app when openable,
 * else its detail page), and the action bar reveals on `group-focus-within`
 * — the repo's hover-reveal pattern (ChatSidebar rows, MarkdownRenderer copy
 * button) — so every action is reachable by Tab without a pointer. The
 * hidden bar is `pointer-events-none` until revealed so it cannot steal
 * clicks from the grid row below it (it floats OVER the neighbouring cell,
 * unlike the sidebar bars that overlay their own row).
 */
import {
  ArrowUp, Check, ExternalLink, Pin, PinOff, Plus, Power, PowerOff, Trash2,
} from 'lucide-react'
import AppIconTile from '../../components/appstore/AppIconTile'
import { appDisplayName } from '../../components/appstore/appManifest'
import { manifestArt } from '../../components/appstore/useHeroArt'
import { i18nT } from '../../i18n/t'
import type { LibraryApp } from './useAppsData'

/** Management verbs the tile can request — same shape InstalledAppCard used. */
export type LaunchpadAction = 'enable' | 'disable' | 'uninstall' | 'update'

/** One action-bar button: quiet text row, lucide glyph, no chrome until hover. */
function BarBtn({
  onClick, disabled, danger, accent, title, children,
}: {
  onClick: () => void
  disabled?: boolean
  danger?: boolean
  accent?: boolean
  title?: string
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      className={`flex items-center gap-1 text-[11px] px-2 py-1 rounded-md whitespace-nowrap transition-colors bg-transparent border-0 cursor-pointer disabled:opacity-40 disabled:cursor-default ${
        danger
          ? 'text-danger hover:bg-danger/10'
          : accent
            ? 'text-accent bg-accent-subtle font-semibold hover:opacity-80'
            : 'text-text hover:bg-bg-hover'
      }`}
      onClick={onClick}
      disabled={disabled}
      title={title}
    >
      {children}
    </button>
  )
}

export default function LaunchpadTile({
  app,
  pinned,
  pinnable = true,
  actionLoading,
  onTogglePin,
  onAction,
  onOpen,
  onDetail,
}: {
  app: LibraryApp
  /** True when the app's page(s) show in the sidebar (id NOT in mc-app-nav-hidden). */
  pinned: boolean
  /**
   * False when the app has no sidebar destination at all (`appNavTarget`
   * returned null — enabled but no UI page). A pin control there would
   * promise a sidebar row the app cannot have, so the badge, the bar's
   * Pin/Unpin button, and the pinned/unpinned caption are all suppressed.
   */
  pinnable?: boolean
  /** `${name}:${action}` while that action is in flight, else null — InstalledAppCard's contract. */
  actionLoading: string | null
  /** Flip this app's sidebar visibility (page persists via the appNavHidden module). */
  onTogglePin: (name: string) => void
  /** Management verb dispatch — the page owns the hooks behind it. */
  onAction: (name: string, action: LaunchpadAction) => void
  /** Navigate to the app's UI (page decides route vs openCommand). */
  onOpen: () => void
  /** Navigate to the app's detail page. */
  onDetail: () => void
}) {
  const m = app.manifest
  const name = app.name
  const display = appDisplayName(app)
  // Icon resolution — the InstalledAppCard/UpdatesList chain verbatim: a page
  // icon glyph, else the manifest's icon through the resolver that refuses
  // external hosts, else the name-hashed gradient inside AppIconTile.
  const artRepo = m?.repo || app.sourceUrl || ''
  const iconUrl = manifestArt(m?.iconUrl, artRepo) || manifestArt(m?.iconPath, artRepo)
  const iconUrlDark = manifestArt(m?.iconUrlDark, artRepo) || manifestArt(m?.iconPathDark, artRepo)
  const pageIcon = m?.ui?.pages?.[0]?.icon || ''

  const disabled = !app.enabled
  const hasUI = !!(m?.ui?.entry) || (m?.ui?.pages?.length || 0) > 0
  const openable = app.enabled && (hasUI || !!m?.openCommand)
  const canUninstall = app.lifecycle !== 'locked'

  // Status caption under the name: disabled wins (a disabled app is not in
  // the sidebar regardless of the stored pin), else the pin state. An
  // enabled app with no sidebar destination has no pin state to caption.
  const caption = disabled
    ? i18nT('pages.libraryPage.tile_disabled')
    : !pinnable
      ? null
      : pinned
        ? i18nT('pages.libraryPage.tile_pinned')
        : i18nT('pages.libraryPage.tile_unpinned')

  const pinLabel = pinned
    ? i18nT('pages.libraryPage.unpin_from_sidebar', { name: display })
    : i18nT('pages.libraryPage.pin_to_sidebar', { name: display })

  return (
    <div
      className="group relative flex flex-col items-center gap-2 rounded-xl px-1.5 pt-3.5 pb-2.5 transition-colors hover:bg-bg-hover focus-within:bg-bg-hover hover:z-10 focus-within:z-10"
      data-testid={`launchpad-tile-${name}`}
    >
      {/* Pin badge — top right, an aria-pressed toggle. Hidden while the app
          is disabled (disabling already removes it from the sidebar) or has
          no sidebar destination: a pin toggle in either state would promise
          something it cannot deliver. */}
      {!disabled && pinnable && (
        <button
          type="button"
          aria-pressed={pinned}
          aria-label={pinLabel}
          title={pinLabel}
          onClick={() => onTogglePin(name)}
          className={`absolute top-2 right-2.5 w-[18px] h-[18px] rounded-full flex items-center justify-center cursor-pointer transition-colors ${
            pinned
              ? 'bg-accent text-accent-fg border-0'
              : 'bg-bg-elevated text-muted border border-border-strong hover:text-text'
          }`}
        >
          {pinned ? <Check size={11} strokeWidth={3} aria-hidden /> : <Plus size={11} aria-hidden />}
        </button>
      )}

      {/* Tile face — a real button so the tile itself is keyboard focusable:
          opens the app when it can open, else its detail page. The icon is the
          shared AppIconTile with the mockup's 58px / rounded-15px geometry
          (important-override on the tile's own rounded-lg). */}
      <button
        type="button"
        onClick={openable ? onOpen : onDetail}
        aria-label={display}
        className="bg-transparent border-0 p-0 cursor-pointer flex flex-col items-center gap-2 min-w-0 max-w-full"
      >
        <AppIconTile
          name={name}
          icon={pageIcon}
          iconUrl={iconUrl}
          iconUrlDark={iconUrlDark}
          className={`w-[58px] h-[58px] !rounded-[15px] shadow-md ${disabled ? 'grayscale opacity-45' : ''}`}
        />
        <span className="text-[12px] font-semibold text-text-strong text-center leading-tight max-w-full truncate">
          {display}
        </span>
      </button>
      {caption && <span className="text-[10.5px] text-muted leading-none">{caption}</span>}

      {/* Hover action bar — floats under the tile, revealed by hover OR any
          focus inside the tile (repo hover-reveal pattern). pointer-events is
          gated with the reveal so the invisible bar cannot intercept clicks
          meant for the grid row beneath it; keyboard focus ignores
          pointer-events, so Tab still reaches the buttons and reveals the bar. */}
      <div
        role="toolbar"
        aria-label={i18nT('pages.libraryPage.tile_actions', { name: display })}
        className="absolute left-1/2 -translate-x-1/2 -bottom-7 z-20 flex gap-0.5 p-0.5 rounded-lg bg-bg-elevated border border-border-strong shadow-lg opacity-0 pointer-events-none transition-opacity group-hover:opacity-100 group-hover:pointer-events-auto group-focus-within:opacity-100 group-focus-within:pointer-events-auto"
      >
        {!disabled && pinnable && (
          <BarBtn accent={pinned} onClick={() => onTogglePin(name)}>
            {pinned
              ? <><PinOff size={11} aria-hidden /> {i18nT('pages.libraryPage.unpin')}</>
              : <><Pin size={11} aria-hidden /> {i18nT('pages.libraryPage.pin')}</>}
          </BarBtn>
        )}
        {openable && (
          <BarBtn onClick={onOpen}>
            <ExternalLink size={11} aria-hidden /> {i18nT('components.appstore.installedAppCard.open')}
          </BarBtn>
        )}
        {app.updateAvailable && !disabled && (
          <BarBtn
            onClick={() => onAction(name, 'update')}
            disabled={actionLoading === `${name}:update`}
            title={i18nT('components.appstore.installedAppCard.update_to', { version: app._newVersion || app.version })}
          >
            <ArrowUp size={11} aria-hidden /> {i18nT('components.appstore.installedAppCard.update')}
          </BarBtn>
        )}
        {disabled ? (
          <BarBtn
            onClick={() => onAction(name, 'enable')}
            disabled={actionLoading === `${name}:enable`}
          >
            <Power size={11} aria-hidden /> {i18nT('components.appstore.installedAppCard.enable')}
          </BarBtn>
        ) : (
          <BarBtn
            onClick={() => onAction(name, 'disable')}
            disabled={actionLoading === `${name}:disable`}
          >
            <PowerOff size={11} aria-hidden /> {i18nT('components.appstore.installedAppCard.disable')}
          </BarBtn>
        )}
        {canUninstall && (
          <BarBtn
            danger
            onClick={() => onAction(name, 'uninstall')}
            disabled={actionLoading === `${name}:uninstall`}
          >
            <Trash2 size={11} aria-hidden /> {i18nT('components.appstore.installedAppCard.uninstall')}
          </BarBtn>
        )}
      </div>
    </div>
  )
}
