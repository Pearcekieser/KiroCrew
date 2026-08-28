/**
 * appNavHidden — the pin-persistence contract for the sidebar Apps group
 * (PR3 Library launchpad).
 *
 * The module owns the `mc-app-nav-hidden` localStorage key (a JSON string
 * array of HIDDEN app nav ids) and the same-tab sync event
 * `mc:app-nav-hidden-changed`. Both the LibraryPage tiles and the App.tsx
 * sidebar filter read/write through it, so the contract pinned here is what
 * keeps the two surfaces agreeing:
 *
 *  - an id ABSENT from storage is visible (pinned) — new installs need no
 *    migration;
 *  - malformed or tampered storage degrades to "everything visible", never
 *    a throw;
 *  - every persisted change dispatches the sync event (same-tab
 *    localStorage writes do not fire `storage`).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import {
  APP_NAV_HIDDEN_CHANGED_EVENT,
  APP_NAV_HIDDEN_KEY,
  isAppNavHidden,
  readAppNavHidden,
  setAppNavHidden,
  subscribeAppNavHidden,
  toggleAppNavHidden,
} from './appNavHidden'

beforeEach(() => {
  localStorage.clear()
})

describe('readAppNavHidden — defaults and malformed storage', () => {
  it('returns the empty set when the key is absent (everything pinned)', () => {
    expect(readAppNavHidden().size).toBe(0)
    expect(isAppNavHidden('app-secretary')).toBe(false)
  })

  it('treats malformed JSON as the empty set instead of throwing', () => {
    localStorage.setItem(APP_NAV_HIDDEN_KEY, '{not json[')
    expect(readAppNavHidden().size).toBe(0)
    expect(isAppNavHidden('app-secretary')).toBe(false)
  })

  it('treats a non-array JSON value as the empty set', () => {
    localStorage.setItem(APP_NAV_HIDDEN_KEY, '{"app-secretary": true}')
    expect(readAppNavHidden().size).toBe(0)
  })

  it('drops non-string entries from a tampered array, keeping the strings', () => {
    localStorage.setItem(APP_NAV_HIDDEN_KEY, '["app-secretary", 7, null, {"x":1}]')
    expect([...readAppNavHidden()]).toEqual(['app-secretary'])
  })
})

describe('writes — persistence and the same-tab sync event', () => {
  it('setAppNavHidden persists the id and dispatches the change event', () => {
    const listener = vi.fn()
    window.addEventListener(APP_NAV_HIDDEN_CHANGED_EVENT, listener)
    try {
      setAppNavHidden('app-secretary', true)
      expect(JSON.parse(localStorage.getItem(APP_NAV_HIDDEN_KEY)!)).toEqual(['app-secretary'])
      expect(listener).toHaveBeenCalledTimes(1)

      setAppNavHidden('app-secretary', false)
      expect(JSON.parse(localStorage.getItem(APP_NAV_HIDDEN_KEY)!)).toEqual([])
      expect(listener).toHaveBeenCalledTimes(2)
    } finally {
      window.removeEventListener(APP_NAV_HIDDEN_CHANGED_EVENT, listener)
    }
  })

  it('toggleAppNavHidden round-trips and reports the NEW hidden state', () => {
    expect(toggleAppNavHidden('app-secretary')).toBe(true)
    expect(isAppNavHidden('app-secretary')).toBe(true)
    expect(toggleAppNavHidden('app-secretary')).toBe(false)
    expect(isAppNavHidden('app-secretary')).toBe(false)
  })

  it('stores a stable sorted array so repeated writes are byte-identical', () => {
    setAppNavHidden('zeta', true)
    setAppNavHidden('alpha', true)
    expect(JSON.parse(localStorage.getItem(APP_NAV_HIDDEN_KEY)!)).toEqual(['alpha', 'zeta'])
  })

  it('recovers a malformed stored value on the next write', () => {
    localStorage.setItem(APP_NAV_HIDDEN_KEY, 'garbage')
    setAppNavHidden('app-secretary', true)
    expect(JSON.parse(localStorage.getItem(APP_NAV_HIDDEN_KEY)!)).toEqual(['app-secretary'])
  })
})

describe('subscribeAppNavHidden', () => {
  it('notifies on writes and stops after unsubscribe', () => {
    const listener = vi.fn()
    const unsubscribe = subscribeAppNavHidden(listener)
    setAppNavHidden('app-secretary', true)
    expect(listener).toHaveBeenCalledTimes(1)

    unsubscribe()
    setAppNavHidden('app-secretary', false)
    expect(listener).toHaveBeenCalledTimes(1)
  })
})
