import type { AppDispatch, RootState } from '../store'
import { createSlot } from '../store/chatSlice'
import type { ChatSlot } from '../types'
import { appPathname } from './basePath'

type CreateSlotArg = Parameters<typeof createSlot>[0]

/** A create that resolved, and whether the user is now looking at it. */
export interface CreatedChatView {
  slot: ChatSlot
  /** The create switched the chat to its new session, and the user is still
   *  on the page where they asked for it. Only then may the caller show the
   *  session or put anything in its composer. */
  shown: boolean
  /** The create switched the chat to its new session, whatever page the user
   *  is on. A surface that tracks its own "where the user is", such as the
   *  embed tab strip, combines this with that instead of `shown`. */
  opened: boolean
  /** The app route the user was on when the create started. */
  from: string
}

/** Whether a route is the chat page, which follows `activeSlot` on its own. */
export function isChatRoute(pathname: string): boolean {
  return pathname === '/chat' || pathname.startsWith('/chat/')
}

/** The page a route shows. Every `/chat/<slug>` is the one chat page: its
 *  slug follows the active session, so the create itself rewrites it. */
function pageOf(pathname: string): string {
  return isChatRoute(pathname) ? '/chat' : pathname
}

/**
 * Run a foreground `createSlot` and report whether its session took the view.
 *
 * A create can take seconds. If the user opened another session meanwhile,
 * `createSlot.fulfilled` lists the new session but leaves the view alone
 * (`forkMayOpen`), and `lastCreatedActivation` then names some other request
 * or none. If the user went to another page, the store may still switch the
 * chat, but the user is no longer where they asked. In either case `shown` is
 * false, so the caller neither navigates nor stages composer input that would
 * land in the session the user moved to.
 *
 * The route is read from `window.location` rather than the router: the caller
 * is often a palette or command bar that has unmounted by the time the create
 * settles, and the router commits a navigation inside a transition, so its
 * rendered location can lag the address bar.
 */
export async function createChatForView(
  dispatch: AppDispatch,
  getState: () => Pick<RootState, 'chat'>,
  arg: CreateSlotArg,
): Promise<CreatedChatView> {
  const from = appPathname()
  const pending = dispatch(createSlot(arg))
  const slot = await pending.unwrap()
  const { lastCreatedActivation, activeSlot } = getState().chat
  const opened = !!slot?.key && lastCreatedActivation?.requestId === pending.requestId && activeSlot === slot.key
  return { slot, opened, shown: opened && pageOf(appPathname()) === pageOf(from), from }
}

/**
 * Bring the user to a create that took the view. The chat page already shows
 * the new session and writes its `?sid=`, so a create started there navigates
 * nowhere: an extra push of `/chat` would add a history entry that Back lands
 * on instead of the session the user came from. Returns `shown`.
 */
export function showCreatedChat(view: CreatedChatView, navigate: (route: string) => void): boolean {
  if (view.shown && !isChatRoute(view.from)) navigate('/chat')
  return view.shown
}
