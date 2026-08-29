/**
 * An INSTALLED app's art resolves against its own install directory, not the
 * blob proxy.
 *
 * The bytes are already on local disk, so `/apps/{name}/art/…` needs no network
 * and cannot 403 the way the proxy's SSRF allowlist can before its catalog
 * fetch has warmed — the failure that made a catalog-listed app's icon vanish
 * on a cold Library load, permanently for that paint, because an `<img>` does
 * not retry a 403.
 *
 * These pin the RULES (`installedArt` / `installedArtList`) and the ORDER the
 * consuming surfaces apply them in. The order is the half nothing else would
 * catch: every chain still ends in the blob-proxy resolver, so putting the new
 * resolver last would leave every assertion about "no proxy URL" passing on the
 * old behaviour.
 */
import { describe, it, expect } from 'vitest'

import { installedArt, installedArtList } from '../components/appstore/useHeroArt'

describe('installedArt', () => {
  it('routes a repo-relative path at the app own install directory', () => {
    expect(installedArt('assets/icon.webp', 'demo-app'))
      .toBe('/apps/demo-app/art/assets/icon.webp')
  })

  it('normalizes a leading ./ the way the backend does', () => {
    // The backend compares the request against the manifest's declared paths
    // with `./` stripped; emitting it here would never match.
    expect(installedArt('./assets/icon.webp', 'demo-app'))
      .toBe('/apps/demo-app/art/assets/icon.webp')
  })

  it('passes an absolute same-origin path through untouched', () => {
    // A built-in's /app-assets/… is already correct and is not repo-relative.
    expect(installedArt('/app-assets/dev-fleet/icon.svg', 'dev-fleet'))
      .toBe('/app-assets/dev-fleet/icon.svg')
  })

  it('refuses every cross-origin spelling a manifest could name', () => {
    for (const value of [
      'https://evil.example/x.png',
      '//evil.example/x.png',
      '/\\evil.example/x.png',
      '\\\\evil.example/x.png',
      '/\t/evil.example/x.png',
    ]) {
      expect(installedArt(value, 'demo-app')).toBe('')
    }
  })

  it('refuses a value that is not a non-empty string', () => {
    // An app.json declaring `"iconPath": {}` reaches here as an object; a bare
    // startsWith would throw and take the whole surface down.
    for (const value of [undefined, null, '', {}, [], 42, true]) {
      expect(installedArt(value, 'demo-app')).toBe('')
    }
  })

  it('answers empty without an app name rather than a rootless URL', () => {
    expect(installedArt('assets/icon.webp', '')).toBe('')
    expect(installedArt('assets/icon.webp', undefined)).toBe('')
  })

  it('encodes each segment but keeps the separators', () => {
    expect(installedArt('assets/my icon.webp', 'demo-app'))
      .toBe('/apps/demo-app/art/assets/my%20icon.webp')
  })
})

describe('installedArtList', () => {
  it('resolves every entry and drops the refused ones', () => {
    expect(installedArtList(
      ['assets/a.webp', 'https://evil.example/b.png', './assets/c.webp'], 'demo-app',
    )).toEqual(['/apps/demo-app/art/assets/a.webp', '/apps/demo-app/art/assets/c.webp'])
  })

  it('answers an empty list for a non-array', () => {
    // `screenshotsDark` is not coerced by the installed-app normalizer, so an
    // app.json declaring `{}` would reach a bare .map and throw.
    expect(installedArtList({}, 'demo-app')).toEqual([])
    expect(installedArtList(undefined, 'demo-app')).toEqual([])
  })

  it('is EMPTY, not falsy-guarding, when nothing resolves', () => {
    // The trap this pins: `installedArtList(...) || manifestArtList(...)` reads
    // naturally and is wrong, because [] is truthy in JS — an all-refused list
    // would short-circuit the blob-proxy fallback instead of deferring to it.
    // AppDetailPage therefore selects on .length, and this asserts the value it
    // has to select on.
    const empty = installedArtList(['https://evil.example/x.png'], 'demo-app')
    expect(empty).toEqual([])
    expect(Array.isArray(empty)).toBe(true)
    expect(!!empty).toBe(true)
  })
})
