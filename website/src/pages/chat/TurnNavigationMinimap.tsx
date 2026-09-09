import { createPortal } from 'react-dom'
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent, type MouseEvent, type RefObject } from 'react'
import type { ChatMessage } from '../../types'
import { i18nT } from '../../i18n/t'

const MIN_TURNS = 2
const MIN_GUTTER_PX = 40
const MIN_PANE_WIDTH_PX = 560
const DEFAULT_CONTENT_WIDTH_PX = 900
const MARKER_STEP_PX = 9
const PREVIEW_WIDTH_PX = 288
const PROMPT_PREVIEW_MAX_CHARS = 64
const RESPONSE_PREVIEW_MAX_CHARS = 112
const PREVIEW_GAP_PX = 10
const VIEWPORT_EDGE_PX = 12

export interface TurnNavigationItem {
  id: string
  displayIndex: number
  prompt: string
  response: string
}

export function compactTurnPreview(value: string): string {
  return value.replace(/\s+/g, ' ').trim()
}

export function shortenTurnPreview(value: string, maxChars: number): string {
  const compact = compactTurnPreview(value)
  if (compact.length <= maxChars) return compact
  const budget = Math.max(1, maxChars - 3)
  const candidate = compact.slice(0, budget + 1)
  const lastSpace = candidate.lastIndexOf(' ')
  const cut = lastSpace >= Math.floor(budget * 0.6) ? lastSpace : budget
  return `${candidate.slice(0, cut).trimEnd()}...`
}

export function buildTurnNavigationItems(
  messages: ChatMessage[],
  messageToDisplayIdx: ReadonlyMap<number, number>,
): TurnNavigationItem[] {
  const items: TurnNavigationItem[] = []
  for (let index = 0; index < messages.length; index++) {
    const prompt = messages[index]
    if (prompt.role !== 'user') continue
    const displayIndex = messageToDisplayIdx.get(index)
    if (displayIndex === undefined) continue

    let response = ''
    for (let next = index + 1; next < messages.length && messages[next].role !== 'user'; next++) {
      if (messages[next].role === 'assistant' && messages[next].content.trim()) {
        response = messages[next].content
      }
    }
    const meta = prompt.meta as Record<string, unknown> | undefined
    const explicitId = meta?.mid ?? meta?.clientTs ?? prompt.ts
    items.push({
      id: typeof explicitId === 'string' && explicitId ? explicitId : `turn-${index}`,
      displayIndex,
      prompt: compactTurnPreview(prompt.content),
      response: compactTurnPreview(response),
    })
  }
  return items
}

export function markerPosition(index: number, count: number): number {
  if (count <= 1) return 0
  return index / (count - 1)
}

export function pointerToTurnIndex(pointerY: number, top: number, height: number, count: number): number {
  if (count <= 1 || height <= 0) return 0
  const fraction = Math.min(1, Math.max(0, (pointerY - top) / height))
  return Math.min(count - 1, Math.max(0, Math.round(fraction * (count - 1))))
}

function constrainedContentRect(scroller: HTMLDivElement): DOMRect | null {
  const mountedRow = scroller.querySelector<HTMLElement>('[data-display-index]')
  const constrained = mountedRow?.matches('[style*="--mc-content-width"]')
    ? mountedRow
    : mountedRow?.querySelector<HTMLElement>('[style*="--mc-content-width"]')
  const rect = constrained?.getBoundingClientRect()
  if (rect && rect.width > 0) return rect

  const scrollerRect = scroller.getBoundingClientRect()
  const contentWidthValue = getComputedStyle(scroller.parentElement ?? scroller)
    .getPropertyValue('--mc-content-width')
  const configuredWidth = Number.parseFloat(contentWidthValue) || DEFAULT_CONTENT_WIDTH_PX
  const width = Math.min(configuredWidth, scroller.clientWidth)
  const left = scrollerRect.left + (scroller.clientWidth - width) / 2
  return { ...scrollerRect, left, right: left + width, width } as DOMRect
}

function hasSafeLeftGutter(scroller: HTMLDivElement): boolean {
  if (scroller.clientWidth < MIN_PANE_WIDTH_PX) return false
  const scrollerRect = scroller.getBoundingClientRect()
  const contentRect = constrainedContentRect(scroller)
  return !!contentRect && contentRect.left - scrollerRect.left >= MIN_GUTTER_PX
}

function markerWidth(index: number, selected: number | null): number {
  if (selected === null) return 14
  const distance = Math.abs(index - selected)
  if (distance === 0) return 26
  if (distance === 1) return 20
  if (distance === 2) return 16
  return 14
}

interface TurnNavigationMinimapProps {
  items: TurnNavigationItem[]
  scrollerRef: RefObject<HTMLDivElement | null>
  onNavigate: (displayIndex: number) => void
}

export default function TurnNavigationMinimap({
  items,
  scrollerRef,
  onNavigate,
}: TurnNavigationMinimapProps) {
  const [hasGutter, setHasGutter] = useState(false)
  const [coarsePointer, setCoarsePointer] = useState(false)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [previewTop, setPreviewTop] = useState(0)
  const [previewLeft, setPreviewLeft] = useState(0)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const markerRefs = useRef<Array<HTMLSpanElement | null>>([])
  const visibleMarkerIndexes = useRef(new Set<number>())
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const selected = useMemo(() => {
    if (selectedId === null) return null
    const index = items.findIndex(item => item.id === selectedId)
    return index >= 0 ? index : null
  }, [items, selectedId])
  const selectedRef = useRef<number | null>(selected)
  selectedRef.current = selected
  const displayIndexToMarker = useMemo(
    () => new Map(items.map((item, index) => [item.displayIndex, index])),
    [items],
  )
  const railHeight = useMemo(() => Math.max(24, (items.length - 1) * MARKER_STEP_PX), [items.length])
  const clearClose = useCallback(() => {
    if (closeTimer.current) clearTimeout(closeTimer.current)
    closeTimer.current = null
  }, [])
  const scheduleClose = useCallback(() => {
    clearClose()
    closeTimer.current = setTimeout(() => setSelectedId(null), 120)
  }, [clearClose])

  const placePreview = useCallback((index: number) => {
    const button = buttonRef.current
    if (!button || !items[index]) return
    const rect = button.getBoundingClientRect()
    const markerY = rect.top + markerPosition(index, items.length) * rect.height
    const estimatedHeight = items[index].response ? 92 : 56
    setPreviewTop(Math.min(
      window.innerHeight - estimatedHeight - VIEWPORT_EDGE_PX,
      Math.max(VIEWPORT_EDGE_PX, markerY - estimatedHeight / 2),
    ))
    setPreviewLeft(Math.min(
      window.innerWidth - PREVIEW_WIDTH_PX - VIEWPORT_EDGE_PX,
      rect.right + PREVIEW_GAP_PX,
    ))
  }, [items])

  const select = useCallback((index: number | null) => {
    clearClose()
    const item = index === null ? undefined : items[index]
    setSelectedId(item?.id ?? null)
    if (item && index !== null) placePreview(index)
  }, [clearClose, items, placePreview])

  useEffect(() => {
    const query = window.matchMedia?.('(pointer: coarse)')
    const update = () => setCoarsePointer(!!query?.matches)
    update()
    query?.addEventListener?.('change', update)
    return () => query?.removeEventListener?.('change', update)
  }, [])

  useEffect(() => {
    if (selectedId !== null && !items.some(item => item.id === selectedId)) setSelectedId(null)
  }, [items, selectedId])

  useEffect(() => {
    const scroller = scrollerRef.current
    if (!scroller || items.length < MIN_TURNS || coarsePointer) {
      setHasGutter(false)
      visibleMarkerIndexes.current.clear()
      return
    }
    let frame = 0
    let settleFrames = 6
    const measure = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => {
        setHasGutter(hasSafeLeftGutter(scroller))
        const viewport = scroller.getBoundingClientRect()
        const nextVisible = new Set<number>()
        const mountedRows = scroller.querySelectorAll<HTMLElement>('[data-display-index]')
        for (const row of mountedRows) {
          const displayIndex = Number(row.dataset.displayIndex)
          const markerIndex = displayIndexToMarker.get(displayIndex)
          if (markerIndex === undefined) continue
          const rect = row.getBoundingClientRect()
          if (rect.bottom > viewport.top && rect.top < viewport.bottom) nextVisible.add(markerIndex)
        }
        const changed = new Set([...visibleMarkerIndexes.current, ...nextVisible])
        let needsMarkerRetry = false
        for (const index of changed) {
          const wasVisible = visibleMarkerIndexes.current.has(index)
          const isVisible = nextVisible.has(index)
          const marker = markerRefs.current[index]
          if (!marker) {
            needsMarkerRetry = true
            nextVisible.delete(index)
            continue
          }
          const markerIsVisible = marker.dataset.inView === 'true'
          if (wasVisible === isVisible && markerIsVisible === isVisible) continue
          marker.dataset.inView = isVisible ? 'true' : 'false'
          marker.style.background = isVisible ? 'var(--accent)' : 'var(--border-strong)'
          marker.style.opacity = isVisible ? '1' : '0.9'
          marker.style.width = `${markerWidth(index, selectedRef.current)}px`
        }
        visibleMarkerIndexes.current = nextVisible
        if (selectedRef.current !== null) placePreview(selectedRef.current)
        if (needsMarkerRetry || settleFrames-- > 0) measure()
      })
    }
    measure()
    scroller.addEventListener('scroll', measure, { passive: true })
    const resizeObserver = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure)
    resizeObserver?.observe(scroller)
    const mutationObserver = typeof MutationObserver === 'undefined' ? null : new MutationObserver(measure)
    mutationObserver?.observe(scroller, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ['data-display-index'],
    })
    window.addEventListener('resize', measure)
    return () => {
      cancelAnimationFrame(frame)
      scroller.removeEventListener('scroll', measure)
      resizeObserver?.disconnect()
      mutationObserver?.disconnect()
      window.removeEventListener('resize', measure)
    }
  }, [coarsePointer, displayIndexToMarker, hasGutter, items.length, placePreview, scrollerRef])

  useEffect(() => {
    for (let index = 0; index < items.length; index++) {
      const marker = markerRefs.current[index]
      if (!marker) continue
      marker.style.width = `${markerWidth(index, selected)}px`
    }
  }, [items.length, selected])

  useEffect(() => () => clearClose(), [clearClose])

  if (items.length < MIN_TURNS || !hasGutter || coarsePointer) return null
  const active = selected === null ? null : items[selected]
  const label = selected !== null && active
    ? i18nT('pages.chatPage.turn_minimap_active', { current: selected + 1, count: items.length, prompt: active.prompt })
    : i18nT('pages.chatPage.turn_minimap')

  const onMouseMove = (event: MouseEvent<HTMLButtonElement>) => {
    const rect = event.currentTarget.getBoundingClientRect()
    select(pointerToTurnIndex(event.clientY, rect.top, rect.height, items.length))
  }

  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    const current = selected ?? 0
    let next = current
    if (event.key === 'ArrowDown') next = Math.min(items.length - 1, current + 1)
    else if (event.key === 'ArrowUp') next = Math.max(0, current - 1)
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = items.length - 1
    else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      onNavigate(items[current].displayIndex)
      return
    } else return
    event.preventDefault()
    select(next)
  }

  return (
    <nav
      data-testid="turn-navigation-minimap"
      aria-label={i18nT('pages.chatPage.turn_minimap_landmark')}
      className="absolute left-2 top-20 bottom-36 z-[3] hidden md:flex w-10 pointer-events-none items-center"
    >
      <button
        ref={buttonRef}
        type="button"
        aria-label={label}
        aria-describedby={active ? 'turn-navigation-preview' : undefined}
        className="relative w-10 cursor-pointer pointer-events-auto bg-transparent border-none p-0 focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg)] rounded"
        style={{ height: `min(calc(100% - 8px), ${railHeight}px)` }}
        onMouseMove={onMouseMove}
        onMouseLeave={scheduleClose}
        onFocus={() => select(selected ?? 0)}
        onBlur={scheduleClose}
        onClick={(event) => {
          const rect = event.currentTarget.getBoundingClientRect()
          const index = event.detail > 0
            ? pointerToTurnIndex(event.clientY, rect.top, rect.height, items.length)
            : (selected ?? 0)
          select(index)
          onNavigate(items[index].displayIndex)
        }}
        onKeyDown={onKeyDown}
      >
        {items.map((item, index) => (
          <span
            key={item.id}
            ref={element => { markerRefs.current[index] = element }}
            data-testid="turn-navigation-marker"
            data-target-display-index={item.displayIndex}
            data-in-view="false"
            aria-hidden
            className="absolute left-0 block h-[3px] rounded-full transition-[width,background-color] duration-150"
            style={{
              top: `${markerPosition(index, items.length) * 100}%`,
              width: markerWidth(index, selected),
              background: 'var(--muted-strong)',
              opacity: 0.72,
              transform: 'translateY(-50%)',
            }}
          />
        ))}
      </button>
      {active && createPortal(
        // The tooltip text is deliberately selectable. Hover handlers only keep
        // it open while the pointer crosses from the rail; all navigation stays
        // on the single keyboard-accessible button above.
        // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
        <div
          id="turn-navigation-preview"
          role="tooltip"
          data-testid="turn-navigation-preview"
          className="fixed z-[100] max-w-[calc(100vw-5rem)] rounded-lg border border-border bg-bg-elevated px-3 py-2 text-left shadow-xl pointer-events-auto select-text"
          style={{
            top: previewTop,
            left: previewLeft,
            width: PREVIEW_WIDTH_PX,
            borderLeftColor: 'var(--accent)',
            borderLeftWidth: 3,
          } as CSSProperties}
          onMouseEnter={clearClose}
          onMouseLeave={scheduleClose}
        >
          <div className="text-[12px] font-medium leading-[17px] text-text" title={active.prompt}>
            {shortenTurnPreview(active.prompt, PROMPT_PREVIEW_MAX_CHARS)}
          </div>
          {active.response && (
            <div className="mt-1.5 border-t border-border pt-1.5 text-[11px] leading-[16px] text-muted" title={active.response}>
              {shortenTurnPreview(active.response, RESPONSE_PREVIEW_MAX_CHARS)}
            </div>
          )}
        </div>,
        document.body,
      )}
    </nav>
  )
}
