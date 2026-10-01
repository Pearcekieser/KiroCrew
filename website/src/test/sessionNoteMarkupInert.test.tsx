/**
 * A note an agent leaves with `session_set_note` is untrusted display text: it
 * lands in the target's transcript as a `reconcile-note` inject row, and both
 * inject renderers (ChatPage and the app-sdk registry) hand its content to
 * `MarkdownRenderer` with `softBreaks`. This pins that hostile markup in such a
 * note renders inert through that exact call: no script, no event handler, no
 * frame, no `javascript:` link.
 */
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'

const HOSTILE_NOTE = [
  'The base moved.',
  '<script>window.__zzq_pwned = 1</script>',
  '<img src="x" onerror="window.__zzq_pwned = 2">',
  '<iframe src="https://example.invalid/frame"></iframe>',
  '<a href="javascript:window.__zzq_pwned=3">click</a>',
  '<div onclick="window.__zzq_pwned = 4" style="position:fixed;inset:0">overlay</div>',
  '[md link](javascript:window.__zzq_pwned=5)',
].join('\n')

describe('a session_set_note note renders inert', () => {
  it('strips scripts, handlers, frames and javascript: links', () => {
    const { container } = render(<MarkdownRenderer content={HOSTILE_NOTE} softBreaks />)
    const html = container.innerHTML

    expect(container.querySelector('script')).toBeNull()
    expect(container.querySelector('iframe')).toBeNull()
    expect(container.querySelector('[onerror],[onclick]')).toBeNull()
    expect(html).not.toMatch(/\son(error|click)=/i)
    for (const a of Array.from(container.querySelectorAll('a'))) {
      expect(a.getAttribute('href') ?? '').not.toMatch(/^\s*javascript:/i)
    }
    // A stripped inline style is what keeps a note from drawing over the page.
    expect(html).not.toContain('position:fixed')
    // The prose itself still renders.
    expect(container.textContent).toContain('The base moved.')
    expect((window as unknown as { __zzq_pwned?: number }).__zzq_pwned).toBeUndefined()
  })
})
