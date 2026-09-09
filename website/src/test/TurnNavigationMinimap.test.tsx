import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import TurnNavigationMinimap, {
  buildTurnNavigationItems,
  markerPosition,
  pointerToTurnIndex,
  shortenTurnPreview,
  type TurnNavigationItem,
} from '../pages/chat/TurnNavigationMinimap'
import type { ChatMessage } from '../types'

const ITEMS: TurnNavigationItem[] = [
  { id: 'one', displayIndex: 1, prompt: 'First prompt', response: 'First response' },
  { id: 'two', displayIndex: 4, prompt: 'Second prompt', response: 'Second response' },
  { id: 'three', displayIndex: 7, prompt: 'Third prompt', response: '' },
]

function rect(top: number, bottom: number, left = 100, width = 900): DOMRect {
  return { top, bottom, left, right: left + width, width, height: bottom - top, x: left, y: top, toJSON: () => ({}) }
}

function buildScroller() {
  const scroller = document.createElement('div')
  Object.defineProperty(scroller, 'clientWidth', { configurable: true, value: 1100 })
  scroller.getBoundingClientRect = () => rect(0, 600, 0, 1100)
  const rowRects = [rect(80, 180), rect(240, 340), rect(700, 800)]
  ITEMS.forEach((item, index) => {
    const row = document.createElement('div')
    row.dataset.displayIndex = String(item.displayIndex)
    row.getBoundingClientRect = () => rowRects[index]
    scroller.append(row)
  })
  document.body.append(scroller)
  return scroller
}

describe('TurnNavigationMinimap', () => {
  it('builds one item per user turn with the final assistant response', () => {
    const messages = [
      { role: 'user', content: '  First\n prompt ', ts: '1' },
      { role: 'assistant', content: 'draft' },
      { role: 'assistant', content: ' Final\n response ' },
      { role: 'tool', content: 'ignored' },
      { role: 'user', content: 'Second prompt', ts: '2' },
    ] as ChatMessage[]
    expect(buildTurnNavigationItems(messages, new Map([[0, 2], [4, 8]]))).toEqual([
      { id: '1', displayIndex: 2, prompt: 'First prompt', response: 'Final response' },
      { id: '2', displayIndex: 8, prompt: 'Second prompt', response: '' },
    ])
  })

  it('maps marker and pointer positions proportionally', () => {
    expect(markerPosition(0, 5)).toBe(0)
    expect(markerPosition(2, 5)).toBe(0.5)
    expect(markerPosition(4, 5)).toBe(1)
    expect(pointerToTurnIndex(100, 100, 400, 5)).toBe(0)
    expect(pointerToTurnIndex(300, 100, 400, 5)).toBe(2)
    expect(pointerToTurnIndex(500, 100, 400, 5)).toBe(4)
  })

  it('shortens previews at a word boundary with three dots', () => {
    expect(shortenTurnPreview('short preview', 32)).toBe('short preview')
    expect(shortenTurnPreview('update the rfc to address the fable review findings', 35))
      .toBe('update the rfc to address the...')
    expect(shortenTurnPreview('abcdefghijklmnopqrstuvwxyz', 12)).toBe('abcdefghi...')
  })

  it('shows prompt and response preview, highlights visible turns, and navigates by pointer', async () => {
    const scroller = buildScroller()
    const onNavigate = vi.fn()
    render(<TurnNavigationMinimap items={ITEMS} scrollerRef={{ current: scroller }} onNavigate={onNavigate} />)

    const button = await screen.findByRole('button')
    button.getBoundingClientRect = () => rect(100, 300, 8, 40)
    await waitFor(() => {
      const markers = screen.getAllByTestId('turn-navigation-marker')
      expect(markers[0]).toHaveAttribute('data-in-view', 'true')
      expect(markers[1]).toHaveAttribute('data-in-view', 'true')
      expect(markers[2]).toHaveAttribute('data-in-view', 'false')
      expect(markers.map(marker => marker.style.width)).toEqual(['14px', '14px', '14px'])
    })

    fireEvent.mouseMove(button, { clientY: 200 })
    expect(screen.getByRole('tooltip')).toHaveTextContent('Second prompt')
    expect(screen.getByRole('tooltip')).toHaveTextContent('Second response')
    fireEvent.click(button)
    expect(onNavigate).toHaveBeenCalledWith(4)
  })

  it('uses one keyboard target for Arrow, Home, End, Enter, and Space navigation', async () => {
    const scroller = buildScroller()
    const onNavigate = vi.fn()
    render(<TurnNavigationMinimap items={ITEMS} scrollerRef={{ current: scroller }} onNavigate={onNavigate} />)
    const button = await screen.findByRole('button')
    button.getBoundingClientRect = () => rect(100, 300, 8, 40)

    fireEvent.focus(button)
    expect(screen.getByRole('tooltip')).toHaveTextContent('First prompt')
    fireEvent.keyDown(button, { key: 'End' })
    expect(screen.getByRole('tooltip')).toHaveTextContent('Third prompt')
    fireEvent.keyDown(button, { key: 'Enter' })
    expect(onNavigate).toHaveBeenLastCalledWith(7)
    fireEvent.keyDown(button, { key: 'Home' })
    fireEvent.keyDown(button, { key: ' ' })
    expect(onNavigate).toHaveBeenLastCalledWith(1)
  })

  it('does not render for one turn or without a safe left gutter', async () => {
    const scroller = buildScroller()
    const first = render(<TurnNavigationMinimap items={ITEMS.slice(0, 1)} scrollerRef={{ current: scroller }} onNavigate={vi.fn()} />)
    expect(screen.queryByTestId('turn-navigation-minimap')).toBeNull()
    first.unmount()

    Object.defineProperty(scroller, 'clientWidth', { configurable: true, value: 500 })
    render(<TurnNavigationMinimap items={ITEMS} scrollerRef={{ current: scroller }} onNavigate={vi.fn()} />)
    await new Promise(resolve => requestAnimationFrame(resolve))
    expect(screen.queryByTestId('turn-navigation-minimap')).toBeNull()
  })

  it('measures the constrained child of a full-width grouped-turn wrapper', async () => {
    const scroller = buildScroller()
    const first = scroller.querySelector<HTMLElement>('[data-display-index]')!
    first.getBoundingClientRect = () => rect(80, 180, 0, 1100)
    const constrained = document.createElement('div')
    constrained.style.maxWidth = 'var(--mc-content-width, 900px)'
    constrained.getBoundingClientRect = () => rect(80, 180, 100, 900)
    first.append(constrained)

    render(<TurnNavigationMinimap items={ITEMS} scrollerRef={{ current: scroller }} onNavigate={vi.fn()} />)
    expect(await screen.findByTestId('turn-navigation-minimap')).toBeInTheDocument()
  })

  it('keeps selection on the same turn across prepends and closes it when removed', async () => {
    const scroller = buildScroller()
    const onNavigate = vi.fn()
    const view = render(<TurnNavigationMinimap items={ITEMS} scrollerRef={{ current: scroller }} onNavigate={onNavigate} />)
    const button = await screen.findByRole('button')
    button.getBoundingClientRect = () => rect(100, 300, 8, 40)
    fireEvent.focus(button)
    fireEvent.keyDown(button, { key: 'ArrowDown' })
    expect(screen.getByRole('tooltip')).toHaveTextContent('Second prompt')

    const prepended = [{ id: 'zero', displayIndex: 0, prompt: 'Earlier prompt', response: '' }, ...ITEMS]
    view.rerender(<TurnNavigationMinimap items={prepended} scrollerRef={{ current: scroller }} onNavigate={onNavigate} />)
    expect(screen.getByRole('tooltip')).toHaveTextContent('Second prompt')

    view.rerender(<TurnNavigationMinimap items={prepended.filter(item => item.id !== 'two')} scrollerRef={{ current: scroller }} onNavigate={onNavigate} />)
    await waitFor(() => expect(screen.queryByRole('tooltip')).toBeNull())
    fireEvent.click(button)
    expect(onNavigate).toHaveBeenCalled()
  })

  it('does not mount the hover rail for coarse pointers', async () => {
    const original = window.matchMedia
    window.matchMedia = vi.fn().mockReturnValue({
      matches: true,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })
    const scroller = buildScroller()
    render(<TurnNavigationMinimap items={ITEMS} scrollerRef={{ current: scroller }} onNavigate={vi.fn()} />)
    await new Promise(resolve => requestAnimationFrame(resolve))
    expect(screen.queryByTestId('turn-navigation-minimap')).toBeNull()
    window.matchMedia = original
  })
})
