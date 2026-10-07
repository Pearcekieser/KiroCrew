---
title: macOS chat jumps use Control only -- retire the Option+digit preference
status: in-progress
author: Pearcekieser, with kirocrew-worker
created: 2026-10-07
last-audited: 2026-10-07
audited-at: 5b2aee24e4
doc-pr: null
implementation-prs: [17598]
tracking-issues: [17590]
supersedes: []
superseded-by: []
---

# RFC: macOS chat jumps use Control only

- Status: in-progress. The reporter, Pearce Kieser, asked for this on 2026-10-07 in issue [#17590](https://github.com/kirodotdev/KiroCrew/issues/17590). The implementation is [#17598](https://github.com/kirodotdev/KiroCrew/pull/17598). It removes a user-facing switch, and its First Principles lane reads an RFC's status off the base branch. So this document lands on its own first, as GOVERNANCE.md asks, and #17598 rebases onto it.
- Measured at `5b2aee24e4`.

## 1. Problem

On macOS the sidebar chat jumps (jump to chat 1 to 9, and the letters for chats 10 and up) run on Control+digit by default. Settings → Shortcuts and the shortcuts reference both carry a "Use ⌃ Ctrl (not ⌥ Option) for chat 1–9" switch. It writes `mc-mac-ctrl-digits` to localStorage, and `getCtrlDigitsEnabled()` in `website/src/hooks/useKeyboardShortcuts.ts` reads it on every load.

A browser that stored `0` jumps on Option+digit. Option+digit types a character on many Mac layouts: UK ⌥3 is `#` and ⌥2 is `€`. In that state Control+digit does nothing, and the reference rows and held-modifier row badges all show ⌥. The registry comment in `website/src/lib/shortcutRegistry.ts` already names that conflict as the reason Control is the default.

## 2. Decision

On macOS the chat-jump modifier is Control, with no preference.

- The handler and `useDigitModifierHeld` pick the Control branch on macOS and the Alt branch elsewhere from `IS_MAC`.
- Both shortcut surfaces drop the switch. `useShortcutPrefs` keeps only the enable/disable state.
- The stored `mc-mac-ctrl-digits` value is no longer read. A stored `0` moves that browser to Control+digit.
- Windows and Linux keep Alt+digit. Ctrl+digit there is the browser's tab switch and the desktop shell's remote-crew switch (`useInstanceShortcuts`).

## 3. Alternatives considered

Flipping the switch's default changes nothing for a browser that already stored `0`, which is the case the report describes. Keeping the switch and adding a migration that rewrites `0` to `1` keeps a control whose only other setting is the broken one.

Some Macs give Control+digit to the system first: Mission Control's "Switch to Desktop N" and third-party window managers. On those the page never sees the chord, so chat jumps by number do nothing. The retired Option mode was no fallback there, because Option+digit types a character on most layouts. This RFC accepts that loss on purpose. Those users keep ⌥←/⌥→ and ⌘[/⌘] to step between chats, the sidebar, and the command palette's session switcher. Making the jump chord rebindable is tracked in [#4608](https://github.com/kirodotdev/KiroCrew/issues/4608).

A notice for people who chose Option was considered and left out. The reference and Settings rows render ⌃ for every chat jump, and the row badges appear only while Control is held, so the working chord is shown where people look for it. A new notice string would ship in every locale for a state no new install can reach.

## 4. What is removed

| Removed | Reader on main | After |
|---|---|---|
| `getCtrlDigitsEnabled`, `MAC_CTRL_DIGITS_KEY` | `useKeyboardShortcuts.ts`, `ShortcutsModal.tsx` | `IS_MAC` decides |
| The switch in `ShortcutsModal.tsx` and `pages/settings/ShortcutsPanel.tsx` | the person | gone; rows show ⌃ |
| Settings registry entry `shortcuts.use-ctrl-not-option-for-chat-1-9` | command-palette search | regenerated without it |
| Four catalog strings for the switch | the two surfaces | removed from every locale |
| Nightly GUI feature `settings-shortcuts-ctrl-for-chat-tabs` | `gui-user-test.yml` | rewritten to drive Ctrl+digit on `/chat` |

`mc-mac-ctrl-digits` is not in the `uiPrefs.ts` backup allowlist and has no backend reader.
