import { Sparkles } from 'lucide-react'
import { findReport, sendErrorToChat, type ErrorReport } from '../utils/errorReport'
import { buildErrorPrompt } from '../utils/errorReport.prompt'

import { i18nT } from '../i18n/t'

/**
 * "Ask the agent" — turns a dead-end error message into a chat that already
 * knows what broke.
 *
 * This is an AI agent app: an error the user cannot fix themselves is usually
 * one the agent can, so no error surface should be a dead end. The button
 * carries the full structured context (route, endpoint, HTTP status, backend
 * `code`, raw body) into the composer, not just the sentence already on screen.
 *
 * Two ways to supply that context:
 *  - `report` — a structured {@link ErrorReport}, when the caller has one;
 *  - `message` — just the string, when it does not. The report is then recovered
 *    from the error journal by message match, which is what makes this droppable
 *    into the ~80 existing ad-hoc `setError(e.message)` sites unchanged.
 *
 * **Deliberately hook-free.** Its most important callers are ErrorBoundary
 * fallbacks, and a boundary is exactly where the store or router may be the
 * thing that threw — so requiring `<Provider>`/`<Router>` context would make the
 * button unavailable in the case it matters most. Navigation goes through the
 * `installSoftNavigate` seam in `utils/errorReport`, which degrades to a full
 * page load instead of throwing.
 */
export function askAgentPrompt(report: ErrorReport | { message: string }): string {
  return buildErrorPrompt(report, i18nT('components.askAgent.prompt_lead'))
}

/**
 * Hand off with a forced full page load — for the root ErrorBoundary, where a
 * soft navigation would just re-render the tree that threw.
 *
 * Takes the message and resolves the journal entry HERE, at call time, for the
 * same reason the button does: the boundary's `componentDidCatch` journals the
 * report only after React has already rendered this fallback.
 */
export function askAgentHard(message: string): void {
  const resolved = findReport(message) ?? { message }
  sendErrorToChat(askAgentPrompt(resolved), { hard: true })
}

export default function AskAgentButton({
  report,
  message,
  variant = 'link',
  hard = false,
  beforeHandoff,
  afterHandoff,
  className = '',
}: {
  report?: ErrorReport
  message?: string
  /** `link` for inline use next to an error line; `solid` for a primary action in a fallback card. */
  variant?: 'link' | 'solid'
  /** Force a full page load (crash fallbacks, where the live tree is suspect). */
  hard?: boolean
  /**
   * Runs immediately BEFORE the hand-off is staged, while this subtree is still
   * mounted — the only moment a caller can persist state the navigation would
   * destroy (a form the user half-filled, which on a save-failure banner is by
   * definition not saved anywhere else).
   *
   * **Return `false` to VETO the hand-off.** A caller that could not protect its
   * state says so, and the navigation is abandoned rather than proceeding to
   * destroy it. Returning nothing never vetoes.
   *
   * **A throw vetoes too.** This callback exists to persist, so a throwing
   * persist is a failed persist: swallowing it and navigating anyway destroys the
   * state the veto exists to protect. Only `afterHandoff` may throw harmlessly,
   * because by then the hand-off has already succeeded.
   */
  beforeHandoff?: () => boolean | void
  /**
   * Runs only once the hand-off has actually proceeded — for a caller that
   * DISMISSES something (a modal that would otherwise sit over the chat, an error
   * banner whose job is done).
   *
   * Deliberately not the same hook as {@link beforeHandoff}: the two have opposite
   * timing requirements. Persisting must happen before the navigation or there is
   * nothing left to persist; dismissing must happen after, or a staging failure
   * leaves the surface cleared with no navigation and no visible diagnostic — the
   * error erased with nothing shown in its place.
   */
  afterHandoff?: () => void
  className?: string
}) {
  // Render only needs to know whether there is anything to offer. The report is
  // resolved at CLICK time, not here, because of an ordering hazard in the
  // boundaries: React runs getDerivedStateFromError -> renders this fallback ->
  // and only THEN componentDidCatch, which is what journals the report. Resolving
  // during render would therefore capture the pre-journal state — a bare message
  // with no stack and no component context — and since componentDidCatch writes an
  // instance field rather than state, nothing re-renders to correct it.
  if (!report && !message) return null

  const onClick = () => {
    const resolved: ErrorReport | { message: string } | null =
      report ?? findReport(message) ?? (message ? { message } : null)
    if (!resolved) return
    // Persist first: `sendErrorToChat` navigates, and the navigation unmounts the
    // subtree this button was rendered in, so this is the caller's only usable
    // moment. An explicit `false` means it could NOT — abandon rather than
    // navigate away from state nothing is holding. A THROW is the same answer: a
    // callback whose job is persisting did not finish, so proceeding would
    // destroy exactly what the veto protects.
    try {
      if (beforeHandoff?.() === false) return
    } catch { return }
    // Dismiss only once the hand-off actually proceeded. Clearing a surface on a
    // staging failure would leave neither a navigation nor a visible error.
    if (!sendErrorToChat(askAgentPrompt(resolved), { hard })) return
    try { afterHandoff?.() } catch { /* dismissal is cosmetic; never throw here */ }
  }

  const base = 'inline-flex items-center gap-1 shrink-0 cursor-pointer transition-colors'
  const skin = variant === 'solid'
    ? 'px-4 py-1.5 rounded-lg text-[13px] font-medium bg-accent text-accent-fg border-none hover:opacity-90'
    // Danger-tinted, not muted grey: inside a red alert a grey link reads as
    // unrelated chrome. Underline marks it as the action in the banner.
    : 'text-[12px] font-medium text-danger/80 hover:text-danger bg-transparent border-none p-0 underline decoration-danger/30 hover:decoration-danger underline-offset-2'

  return (
    <button
      type="button"
      className={`${base} ${skin} ${className}`}
      title={i18nT('components.askAgent.open_a_chat_with_this_error_s_context_attached')}
      onClick={onClick}
    >
      <Sparkles size={13} aria-hidden="true" />
      {i18nT('components.askAgent.ask_the_agent')}
    </button>
  )
}
