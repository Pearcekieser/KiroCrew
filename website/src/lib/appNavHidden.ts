/**
 * Pin-persistence contract for the sidebar Apps group (Library launchpad).
 *
 * The Library page's launchpad grid lets the user choose which installed
 * apps appear ("pinned") in the sidebar. Persistence stores the HIDDEN set
 * — a JSON string array of app nav ids under `mc-app-nav-hidden` — not the
 * visible set, so a newly installed app defaults to pinned with no
 * migration: an id absent from the list is visible.
 *
 * Both writers/readers (LibraryPage tiles and the App.tsx sidebar filter)
 * MUST go through this module so the contract lives in one place. Writes
 * dispatch `mc:app-nav-hidden-changed` on window because same-tab
 * localStorage writes do not fire the `storage` event — the sidebar
 * subscribes to that event to re-render immediately when a tile's pin
 * badge is toggled.
 */
import { safeGetItem, safeSetItem } from '../utils/safeStorage'

/** localStorage key holding the JSON string array of HIDDEN app nav ids. */
export const APP_NAV_HIDDEN_KEY = 'mc-app-nav-hidden'

/** Window event dispatched after every persisted change to the hidden set. */
export const APP_NAV_HIDDEN_CHANGED_EVENT = 'mc:app-nav-hidden-changed'

/**
 * Read the hidden set. Malformed JSON, a non-array value, storage denial,
 * or an absent key all degrade to the empty set (everything visible) —
 * never throw. Non-string entries in a tampered array are dropped.
 */
export function readAppNavHidden(): Set<string> {
  const raw = safeGetItem(APP_NAV_HIDDEN_KEY)
  if (raw === null) return new Set()
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return new Set()
    return new Set(parsed.filter((id): id is string => typeof id === 'string'))
  } catch {
    return new Set()
  }
}

/** True when the given app nav id is hidden from the sidebar (unpinned). */
export function isAppNavHidden(id: string): boolean {
  return readAppNavHidden().has(id)
}

/**
 * Persist a new hidden state for one app id and notify same-tab listeners.
 * Idempotent: writing the already-stored state still dispatches the event
 * (harmless — listeners re-read and land on the same render).
 */
export function setAppNavHidden(id: string, hidden: boolean): void {
  const next = readAppNavHidden()
  if (hidden) next.add(id)
  else next.delete(id)
  writeAppNavHidden(next)
}

/**
 * Flip one app id's hidden state. Returns the NEW hidden value so callers
 * can update local UI state without a second read.
 */
export function toggleAppNavHidden(id: string): boolean {
  const next = readAppNavHidden()
  const nowHidden = !next.has(id)
  if (nowHidden) next.add(id)
  else next.delete(id)
  writeAppNavHidden(next)
  return nowHidden
}

/**
 * Subscribe to same-tab hidden-set changes. Returns an unsubscribe
 * function suitable for a useEffect cleanup. Listeners should re-read via
 * `readAppNavHidden()` — the event carries no payload by design (the
 * stored set is the single source of truth).
 */
export function subscribeAppNavHidden(listener: () => void): () => void {
  window.addEventListener(APP_NAV_HIDDEN_CHANGED_EVENT, listener)
  return () => window.removeEventListener(APP_NAV_HIDDEN_CHANGED_EVENT, listener)
}

function writeAppNavHidden(ids: Set<string>): void {
  safeSetItem(APP_NAV_HIDDEN_KEY, JSON.stringify([...ids].sort()))
  // Same-tab sync: localStorage writes only fire `storage` in OTHER tabs,
  // and mc-app-nav-order has no live propagation to piggyback on (Step 1
  // finding), so dispatch our own event after every write.
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new Event(APP_NAV_HIDDEN_CHANGED_EVENT))
  }
}
