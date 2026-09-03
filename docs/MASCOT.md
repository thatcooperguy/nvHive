# Mascot

The WebUI ships a small sprite-sheet guide in the bottom-right corner. It
mirrors what the AI Wizard is doing so a first-time user sees "thinking",
"running a tool", "needs your OK", "done" or "something broke" without
reading a status line. Click it for **Ask the Wizard** / **Hide mascot**; a
hidden mascot leaves a small green hexagon in the same corner to bring it
back (`localStorage["nvh.mascot.hidden"]`).

The shipped art is a neutral placeholder — a hexagon-headed "hive spirit" in
nvHive green. Any likeness of a real person needs sign-off before it lands
here; when it does, it is a file swap.

## Files

| Path | Role |
|---|---|
| `web/public/mascot/manifest.json` | The contract: sheet URL, frame size, columns, per-state frames and timing. The single source — `web/lib/mascot.ts` imports it at build time |
| `web/public/mascot/sheet.png` | The sprite sheet (committed; the web build never regenerates it) |
| `web/public/mascot/generate_sheet.py` | Stdlib-only Python that renders the placeholder sheet |
| `web/lib/mascot.ts` | Manifest types + validation, `deriveMascotState(event)`, the pub/sub store (`useMascotStore`, `setMascotState`, `sayMascotTip`, `mascotTipProbeDue`) |
| `web/components/Mascot.tsx` | The sprite, speech bubble and menu; mounted by `LayoutShell` on every page including `/setup` |

## Manifest

```json
{
  "sheet": "/mascot/sheet.png",
  "frameWidth": 64,
  "frameHeight": 64,
  "columns": 4,
  "fps": 8,
  "states": {
    "idle":     { "frames": [0, 1, 2, 3],     "loop": true,  "fps": 3 },
    "thinking": { "frames": [4, 5, 6, 7],     "loop": true,  "fps": 5 },
    "working":  { "frames": [8, 9, 10, 11],   "loop": true },
    "asking":   { "frames": [12, 13, 14, 15], "loop": true,  "fps": 4 },
    "happy":    { "frames": [16, 17, 18, 19], "loop": false, "next": "idle", "holdMs": 1500 },
    "error":    { "frames": [20, 21, 22, 23], "loop": false, "next": "idle", "holdMs": 3000, "fps": 12 },
    "sleeping": { "frames": [24, 25, 26, 27], "loop": true,  "fps": 2 }
  }
}
```

- Frame index = `row * columns + column`. A state's frames must be contiguous
  and sit in one row (the animation is a CSS `steps()` walk along a strip);
  anything else degrades to a static first frame.
- `loop: false` plays once and holds the last frame; `next` + `holdMs` advance
  to another state after that many milliseconds (default: one pass).
- Per-state `fps` overrides the sheet-wide `fps`.
- All seven states are required. Extra keys are ignored.

There is one copy of this file. `web/lib/mascot.ts` imports it at build time
(`resolveJsonModule`) and validates it with `isMascotManifest`; a manifest
that fails validation logs an error and renders frame 0 of every state
instead of breaking the page. The component also re-fetches
`/mascot/manifest.json` at mount, so a manifest or PNG edit shows up on
reload without a rebuild — a bad runtime file leaves the bundled one in place.

## Replacing the art

1. Draw a sheet with the same grid: 4 columns × 7 rows of 64×64 frames, rows
   in the order above, transparent background. Different frame sizes or
   column counts are fine — update `frameWidth`, `frameHeight`, `columns` and
   the `frames` arrays to match.
2. Drop it in `web/public/mascot/` (or anywhere under `web/public`) and point
   `sheet` at it. Edit per-state `fps` / `holdMs` to taste.
3. Reload. No rebuild is needed for a manifest or PNG change.

To regenerate the placeholder: `python web/public/mascot/generate_sheet.py`.

## Behaviour

`deriveMascotState` reads the event *payload*, not just its type: the backend
emits `confirm_required` immediately before `done`, so a type-only mapping
would flip asking → happy in the same flush and the mascot would idle while
confirm cards were still waiting.

| Wizard event / UI action | State |
|---|---|
| `iteration` | thinking |
| `tool_call`, `tool_result` | working |
| `confirm_required` | asking |
| `confirm_required` carrying a `privileged` (sudo) card | asking, and the bubble says "the Wizard needs your approval for a privileged change" (`mascotAskingLabel` / `MASCOT_ASKING_PRIVILEGED_LABEL`) — once per tool per session; any other class, known or not, keeps the plain confirmation wording |
| `done` with non-empty `tool_calls` (confirm cards pending) | asking — stays until every card is run or skipped; a privileged card the kill switch has turned off starts settled (`disabled`, Skip only) and does not count |
| `done` with no pending calls | happy → idle after 1.5 s |
| user clicks **Run** on a card | working → happy / error (or back to asking while sibling cards still wait) |
| user clicks **Skip** on a card | idle (or asking while sibling cards still wait) |
| `error` (or a failed stream) | error → idle after 3 s; when confirm cards are still pending (the server emits `confirm_required` right before `error` on a later-iteration failure) error → asking after 3 s. A deliberate refusal (`fallback_reason: profile_local_only_provider_unavailable`) is an answer, not a failure: it ends like `done`, never in the error strip |
| typing in the composer | idle — but only from happy / error / sleeping. Typing never interrupts thinking / working (a stream is in flight) or asking (confirm cards are pending); it only postpones sleep there (`mascotStateOnTyping`) |
| 90 s with nothing happening | sleeping (click or type to wake) |

`done.deferred_tool_calls` — auto-class calls the server skipped because Depth
was 1 or the profile's cost ceiling fired — do not move the mascot. WizardChat
lists them under the answer as muted `not run: <tool> — <reason>` lines and
never executes them. Whitelist refusals (`tool_result` with
`result.not_allowed = true`) likewise never ran: they appear as muted
`not allowed for <specialist>: <tool>` lines, are excluded from the
"Wizard used N tools" count and from Sources, and do not move the mascot
beyond the `working` the `tool_result` event itself implies.

Tips (`sayMascotTip(text, { id, ttlMs })`) show once per `id` per browser
session; the id is burned only when the bubble actually renders, so a hidden
mascot never consumes a tip. Today: a welcome on `/setup`, and the most severe
open finding from `/v1/wizard/diagnostics` when the Wizard page loads. That
diagnostics probe is a full workspace scan (~360 ms), so WizardChat runs it
only while the mascot is visible, at most once per session
(`sessionStorage["nvh.mascot.diagtip.probed"]`, set after the probe completes
and the tip has had its chance to render), and shares the fetch with the
`?issue=<id>` deep link when both need it.

The sprite is `role="img"` with a state-describing label; the bubble is
`role="status"`; the menu is `role="menu"`, rendered after its trigger, takes
focus on open, and closes on Escape (focus returns to the sprite), on focus
leaving the widget, or on a pointer-down outside it. Arrow keys / Home / End
move between items. `prefers-reduced-motion` freezes the animation on the
state's first frame.

## Layering

The mascot is `position: fixed` at `right: 12px; bottom: 120px` with
`z-index: 39` (`MASCOT_Z_INDEX` in `Mascot.tsx`). That is above normal page
content and **below every overlay that must be able to cover it**: the chat
page's mobile sidebar backdrop (`z-40`) and drawer (`z-50`), CreateAgentModal
/ the providers modal / the Sidebar context menu / toasts / the top bar (all
`z-50`), ApiHealthBanner (`z-60`), SystemConsole (`z-65`), DebugReportButton
(`z-80`) and its report (`z-110`). An open modal or drawer therefore dims and
click-blocks the sprite, bubble and menu instead of leaving them floating on
top of it. The 120px bottom offset — not the z-index — is what keeps the
sprite clear of the chat and Wizard composers' Send buttons. If you add a new
full-screen overlay, give it `z-50` or higher.
