import { useState, useEffect, useRef } from 'react'

/** Responsive column count from the container width (~300px target column). */
/** The page column's own horizontal padding at narrow widths (`px-4`, one side).
 *  Only used to seed the column estimate below; the real width is measured. */
const PAGE_GUTTER = 16

export function useColumnCount(minColWidth = 300): readonly [React.RefObject<HTMLDivElement>, number] {
  const ref = useRef<HTMLDivElement>(null)
  // Seeded from the viewport instead of a constant because the page reads this
  // count to decide WHO OWNS THE SCROLL AXIS: a wrong first value hands the axis
  // over and takes it back a frame later, which reads as a jump. This is only an
  // estimate (it assumes the narrow gutter); the ResizeObserver corrects it
  // against the real element on mount either way.
  const [cols, setCols] = useState(() =>
    typeof window === 'undefined'
      ? 2
      : Math.max(1, Math.floor((window.innerWidth - PAGE_GUTTER * 2) / minColWidth)),
  )
  useEffect(() => {
    const el = ref.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const measure = () => setCols(Math.max(1, Math.floor(el.clientWidth / minColWidth)))
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [minColWidth])
  return [ref, cols] as const
}
