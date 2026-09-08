# Pimcamp account onboarding — design notes

Design record for the account onboarding UI described in `../../docs/onboarding-ui-spec.md`.
Three presentation files, no build step, no frameworks, no remote assets:

| File | Role | Owner |
| --- | --- | --- |
| `index.html` | Static shell, inline icon sprite, one `<template>` per screen and per repeating row | design |
| `styles.css` | Design tokens, layout, components, light/dark, reduced motion, forced colors, print | design |
| `DESIGN.md` | This file: decisions, states, integration seams, verification | design |
| `app.js` | View model, navigation, screen renderers, and the **demo service** behind the action boundary | integration |
| `live-service.js` | Real transport, injected by the setup server in live mode | integration |

Open `index.html` directly in a browser for the preview. Everything is sample data; the banner under the
header and the purple “Demo” tags say so wherever simulated results appear. Nothing contacts a server, and
nothing except the theme preference is written to browser storage. The setup server serves the same
`index.html` with `data-mode="live"` on `<html>` and `live-service.js` ahead of `app.js`; `app.js` then hides
the demo banner and demo controls and swaps the demo service for the real one. Those two byte-exact hooks,
`<html lang="en"` and `<script src="app.js" defer></script>`, must stay as they are.

Deep links for review: `index.html#demo=<state>&theme=<system|light|dark>&latency=<ms>`.
The `<state>` keys are the values in the “Jump to a state” list under Demo controls, for example
`#demo=google-mismatch&theme=dark&latency=0`.

## Revision 2 (2026-09-08): what changed and why

The first revision placed the title and lede straight on the page background and turned every group of
fields into its own shadowed card. That read as loose pieces rather than one setup flow, and the nested
cards on Review, Connect and Done stacked border on border. This revision keeps the same tokens, copy,
screen order and every JavaScript contract, and changes the composition:

- **One sheet.** The stage is a single elevated surface (`.stage`) holding the whole screen. Sections
  inside it are separated by hairlines and a small icon tile, never by nested shadowed cards. Lists
  (accounts, summary, checks, capabilities) are bordered containers with row dividers and no shadow.
- **Sheet width.** The stage column is 640px with 40px padding, so the form itself stays at the
  specified 560px. Rail 212px, gap 48px, total 900px inside the 920px content width.
- **Footer actions.** Every screen’s `.actions` row sits under a hairline, so Back and the primary action
  are always in the same place. Primary stays right-aligned; Back stays left. An empty actions row
  collapses so no stray line appears while checks run.
- **Progress rail.** Desktop: 28px numbered markers with a thin connector, the current step in the
  accent with a soft ring, completed steps tinted with a check. Narrow (≤860px): the rail becomes a
  slim bar strip above the sheet, one segment per step; ≤480px shows only the current step’s label under
  the bars. The list markup from `app.js` is unchanged; only CSS differs.
- **Password reveal inside the control.** The eye button now overlays the right edge of the password
  field instead of sitting beside it. DOM order is unchanged (input, then button), so tab order and the
  `aria-pressed` contract are untouched. A pressed state tints the button with the accent.
- **Section headers on the IMAP screen.** “Incoming mail (IMAP)” and “Outgoing mail (SMTP)” gained an icon
  tile and a one-line description (“Where Pimcamp reads your mail.” / “Where Pimcamp sends from. It only
  sends when you ask it to.”). The preset picker sits in its own tinted block above them so it reads as a
  shortcut rather than a required field.
- **Choice cards.** Stronger border, 48px icon tile, arrow that nudges on hover, and a filled tile plus
  soft ring when a card is `aria-current` (the previously chosen method).
- **Google panel.** The state panel is a tinted inset (`surface-2`) inside the white sheet; the identity
  card inside it is white, so nesting reads as depth rather than repetition. Follow-up text under the
  head aligns with the title, not the icon. The administrator section is a dashed inset box, clearly
  separate from the mailbox flow.
- **Type scale.** Titles 28px (32px on the empty state, 22px under 480px), weight 650, tighter tracking.
  `text-wrap: balance` on headings and `text-wrap: pretty` on paragraphs where the browser supports it.
- **Colors.** Success green darkened slightly (`#1b6d3d`) so the “Passed”/“Ready” pills meet 4.5:1 on
  their tinted background. All other tokens are as before, with slightly warmer page and sheet tones.
- **Brand mark.** A filled rounded square with an envelope, in the accent color. It reads at 26px in the
  top bar and stays a neutral mark, not a provider logo.

## Screen inventory

| # | Screen | `state.screen` | States covered |
| --- | --- | --- | --- |
| 1 | Accounts / welcome | `accounts` | loading, empty (“Connect your email”), list with Connected / Needs reconnect, per-row Checking… |
| 2 | Choose connection | `choose` | Google / Gmail, IMAP & SMTP; previous choice marked with `aria-current` |
| 3 | Account details | `details` | valid, field errors (format, duplicate address, name length) |
| 4 | IMAP & SMTP | `imap` | defaults (993/TLS, 465/TLS), STARTTLS port follow, editable presets, shared vs separate outgoing login, password reveal, field errors |
| 5 | Google | `google` | ready, consent in progress (cancellable), cancelled (by user / at Google), expired, organization policy denied, OAuth client missing (with separated administrator section), authorized matching account, authorized different account (explicit choice) |
| 6 | Review and connect | `review` | IMAP summary, Google summary, remote installation host row, reconnect note. No secrets rendered. |
| 7 | Connection results | `connect` | four rows × waiting / checking / passed / failed / unavailable; cancel while checking; retry failed only; per-row repair actions; save failed with nothing changed |
| 8 | Success | `done` | full success, partial (mail watching unavailable or failed) with a repair action, back / add another |

The progress rail shows five steps (Connection, Details, Settings, Review, Connect). On the accounts
screen the rail column holds a short “What you’ll need” note instead of steps, so the two-column shell
stays consistent.

## Visual system

**Tone.** A restrained utility: warm paper background, one near-white sheet, one accent, hairlines,
generous spacing, and a single soft elevation shadow on the sheet. No decorative dashboard elements.

**Tokens** live at the top of `styles.css`. Colors are declared once with `light-dark()`; the
`html[data-theme]` attribute switches `color-scheme` between `light`, `dark`, and `light dark` (system).
The theme picker persists only the preference string in `localStorage`.

| Token | Light | Dark | Use |
| --- | --- | --- | --- |
| `--bg` | `#f4f1ea` | `#151311` | page |
| `--surface` | `#fffefb` | `#1f1c19` | sheet, inputs, list containers |
| `--surface-2` | `#f8f5ef` | `#26221e` | insets: Google panel, preset block, summary section rows |
| `--text` / `--text-muted` | `#1f1c18` / `#635c52` | `#ede8df` / `#aaa295` | body / secondary |
| `--border` / `--border-strong` | `#e6e0d5` / `#cdc5b8` | `#332e28` / `#4a443b` | hairlines / controls |
| `--accent` | `#0f766e` | `#22a69a` | primary buttons, focus ring, current step, chosen card |
| `--success` `--danger` `--warning` | `#1b6d3d` `#b3261e` `#8f5406` | `#5fcb8a` `#f4948c` `#e9ae52` | status only, always paired with text or an icon |
| `--demo` | `#5b3fc4` | `#bfaef7` | demo markers, deliberately outside the product palette |

Primary actions use white text in the light theme and a dark teal text in the dark theme. Status pills use
tinted backgrounds with distinct text and a leading dot; check rows and capability rows add an icon.
Contrast was checked by hand against the token values (all text on its own tint ≥ 4.5:1); a full
accessibility audit remains separate from the layout review.

**Type.** System sans stack (`ui-sans-serif, system-ui, …`). Sizes 12/14/16/18/22/28/32px. Titles use
weight 650 and −0.015em tracking where the system font supports it.

**Layout.** `.shell` is a 920px grid: a 212px sticky rail and a 640px sheet with 40px padding. Under
860px it collapses to one centered column (max 640px) and the rail becomes the bar strip. Under 600px the
sheet padding drops to 20px and the page padding to 12px; under 360px the sheet padding is 16px. Minimum
width 320px: the two-column port/security grid stacks under 480px, the choice arrow hides under 400px,
and the preset “Fill in” button wraps under its select under 400px. The demo panel is a side sheet; from
1320px up the page reserves a 380px column for it so it never covers the form.

**Icons.** One inline SVG sprite (`<symbol>`s at the top of `index.html`), 1.7–2px strokes, always
placed next to visible text. The Google option uses a neutral ring-and-bar mark, not the Google logo.
The brand mark’s envelope strokes use `--on-accent` so it stays legible in both themes.

**Motion.** 160ms ease for screen entry, hover lift on choice cards, control state changes, and the
check spinner. `prefers-reduced-motion` disables animation and transforms; spinners become a static
partial ring and the visible “Checking…” text carries the meaning.

## Interaction and error states (as implemented in `app.js`)

- **Back preserves state.** The draft object survives navigation. Passwords stay in memory for the
  current setup only and are never rendered, logged, or stored.
- **Validation** runs on submit; a field’s error clears as soon as it changes. Errors are linked with
  `aria-describedby`, marked with `aria-invalid`, the first invalid field receives focus, and the count
  plus first message is announced via the assertive live region. Copy says what to do, for example
  “Leave out the “imaps://” part. Enter only the server name.”
- **Ports follow security** only while the port still holds the previous default, so a custom port is
  never overwritten.
- **Presets** fill fields and show a note explaining what was filled; nothing is locked.
- **Google** shows each outcome as its own state with one clear next action. A mismatched identity is
  never resolved silently: the user picks “Connect <authorized> instead” or “Try again with <entered>”.
  A missing OAuth client shows a separated “Administrator setup” section with the callback address.
- **Connection results** run incoming sign-in, outgoing sign-in, save, then mail watching. Nothing is
  written unless both sign-ins pass. Cancel is available while checking and hidden during the save.
  Retry re-runs only rows that are not `passed`. Failed rows carry a repair button that returns to the
  right field. Copy states explicitly that sign-in is not delivery and that no mail is sent.
- **Success** is only shown after a successful save. Partial success keeps the unavailable capability
  visible with a plain-language explanation and a repair action.
- **Duplicate submission** is prevented by state (`connect.running`, `oauth.status`) rather than by
  disabling navigation; Back and the brand link stay usable except while a save is in flight.
- **Live regions.** `#live-status` (polite) announces screen changes, step numbers, and check results;
  `#live-alert` (assertive) announces validation failures and failed checks.
- **Keyboard walkthrough.** Tab order follows reading order; screen titles receive focus on navigation;
  choice cards, password reveal, and demo panel are native buttons and inputs; `Esc` closes the demo
  panel; focus returns to the toggle.

## Suggestions for the integration owner (not implemented here)

These are observations from the layout pass. None changes a contract; each is a small `app.js` or
service change if wanted.

1. **Loading with cancel.** `tpl-loading` has a text slot only. The spec asks loading states to offer
   cancellation when safe; “Preparing setup…” and “Loading accounts…” could render a ghost “Cancel”
   into the sheet via a second slot, styled with the existing `.btn--ghost`.
2. **Chosen method on the choice screen.** The chosen card is already marked with `aria-current`. If a
   visible “Selected” tag is wanted, append a `.tag` into `.choice__body` from the renderer; CSS for
   `.tag` is ready.
3. **Mobile step count.** The narrow bar strip shows only the current step’s label. If a “Step 2 of 5”
   line is wanted, the renderer can put it in the rail eyebrow slot (`data-slot="eyebrow"`) instead of
   generating it from CSS counters, which screen readers would double-announce.
4. **Password hint order.** The app-password hint sits under the password input because it is long.
   If reveal-button hit area is a concern on touch, `--control-h` can rise to 44px without other changes.
5. **Group descriptions.** The new `.group__desc` lines are static copy. If the outgoing group should
   mention the entered address, the renderer can set its text; the class is presentation-only.
6. **`.notice[data-slot="notice"]` on the connect screen** is never filled today. It is styled and ready
   for a screen-level message, for example when the service throws unexpectedly.

## Integration seams

`app.js` is split so that the presentation can move into the Pimcamp repository as-is and only the
service object is swapped.

**Action boundary** (`createDemoService` → the real onboarding service). Every method returns plain
data and receives an `AbortSignal` where cancellation is meaningful.

| Method | Purpose | Returns |
| --- | --- | --- |
| `listAccounts()` | Existing accounts for the hub | `[{ id, address, name, method, status: 'connected'|'needs-reconnect'|'unknown', note, incoming?, outgoing? }]` (no secrets) |
| `beginSetup(accountId?)` | Start a setup, learn installation facts | `{ setupId, installation: { host, remote, oauthClientConfigured } }` |
| `validateFields(step, draft, ctx)` | Field validation (`details`, `imap`) | `{ [fieldId]: message }` |
| `beginOAuth({ email }, signal)` | Start consent; resolves when the callback lands or the flow ends | `{ status: 'authorized'|'cancelled'|'expired'|'denied-policy', identity?, byUser? }` |
| `checkIncoming(draft, signal)` / `checkOutgoing(draft, signal)` | Authenticate only; never fetch or send | `{ ok, code?: 'auth'|'unreachable', message }` |
| `commitAccount(draft, signal)` | Write account-scoped config and credential reference, additive | `{ ok, accountId?, message }` |
| `observationReadiness(accountId, signal)` | Ask the observation adapter | `{ state: 'passed'|'unavailable'|'failed', message }` |
| `checkAccount(id)` | Re-check an existing account | updated account record |

Expectations for the real implementation, from the spec:

- Config paths follow XDG conventions on the installation; the UI never sees or shows them.
- Credentials go to the existing protected storage; the UI passes them once, in a request body, and
  discards them after commit. Tokens are refreshed by the OAuth helper, not by the UI.
- `beginOAuth` should open the provider URL returned by the backend and wait on a backend-owned status
  endpoint; the callback port is internal to the backend. The admin section’s callback text should come
  from the backend rather than `location.origin` once integrated.
- `commitAccount` must fail without side effects when the account exists and this setup is not a
  reconnect of that account id.
- Error messages sent to the UI should already be user-facing; the UI displays `message` verbatim and
  never shows tracebacks or internal identifiers.

**View model** (`state`): `screen`, `accounts`, `installation`, `draft` (nonsecret form state plus
in-memory passwords), `oauth` (`status`, `identity`, `controller`), `connect` (`rows`, `running`,
`cancellable`, `saved`, `accountId`). Renderers read from `state` and templates; they never call the
service directly except through the handlers in the navigation section.

**Templates and hooks.** Every screen and repeating row is a `<template>` with `data-slot` hooks. The
contract consumed by `app.js` and `tests/browser_onboarding.mjs` is: template ids `tpl-*`; slot names;
`data-action` values (`go-accounts`, `toggle-demo`, `demo-reset`, `add-account`, `check-account`,
`reconnect-account`, `choose-method` with `data-method`, `back`, `apply-preset`, `toggle-password` with
`data-target`, `connect`); field ids `f-*` with matching `f-*-error` and `f-*-hint`; `#theme-picker`
radios; `#demo-panel`, `#demo-gallery`, `[data-demo]` controls; the classes `.screen__title`,
`.screen__lede`, `.demo-banner`, `.btn--primary`, `.check-row[data-key][data-state]`,
`.account[data-id]`, `.panel__title`, `.panel__text`, `.notice--inline`, `.field__error`, plus the
classes the renderers create (`step*`, `panel*`, `identity*`, `notice*`, `check-row__actions`, `help`,
`spinner`, `summary__row--section`, `btn--*`). All are unchanged in this revision.

## Demo controls

The side sheet (top-right button) drives the demo service:

- Existing-accounts fixture (none / connected / needs reconnect / both).
- Installation host and remote flag, and whether a Google OAuth client is configured.
- Simulated outcomes for Google authorization, incoming and outgoing sign-in, saving, and mail watching.
- Simulated latency per operation.
- A “Jump to a state” list that sets up any screen with sample values, for screenshots and review.

Fixture accounts are tagged “Demo”; accounts you add during a session are tagged “Preview only” and
disappear on reload. Results screens carry a “These results are simulated” notice whenever the demo
service is in use, so a fixture can never pass for a live check. In live mode the banner, the toggle
button, and the preview console object are all absent.

## Verification and screenshots

Run the layout and interaction review (needs Chromium and Node, no downloads):

```sh
node tests/browser_onboarding.mjs ui/onboarding
PIMCAMP_REVIEW_SCREENSHOTS=1 node tests/browser_onboarding.mjs ui/onboarding
```

It walks every gallery state at 1280, 375 and 320px in light and dark, fails on horizontal overflow,
and runs scripted and keyboard interaction journeys (validation focus, custom port preserved, password
redaction, rejected credentials not saved, duplicate submit prevented, partial watcher status).

Headless screenshots of any single state:

```sh
chromium --headless=new --disable-gpu --hide-scrollbars --virtual-time-budget=4000 \
  --window-size=1280,1000 --screenshot=imap-light.png \
  "file://$PWD/ui/onboarding/index.html#demo=imap&theme=light&latency=0"
chromium --headless=new --disable-gpu --hide-scrollbars --virtual-time-budget=4000 \
  --window-size=375,1200 --screenshot=imap-dark-375.png \
  "file://$PWD/ui/onboarding/index.html#demo=imap&theme=dark&latency=0"
```

Suggested set: `accounts-empty`, `accounts-list`, `choose`, `details-errors`, `imap`, `imap-errors`,
`google-progress`, `google-missing`, `google-mismatch`, `review-imap`, `connect-incoming-failed`,
`connect-observe-unavailable`, `done`, `done-partial`, each at 1280 and 375px, light and dark.

**Status of this revision.** The authoring session could not launch Chromium, Node or Python (command
approval was unavailable), so the browser review and screenshots were not run here. What was verified:
every id, template, slot, action, field and class contract listed above is present in `index.html`;
the two byte-exact strings the setup server rewrites are unchanged; the stylesheet’s braces balance and
every selector the renderers rely on has a rule. Subsequent integration-owned
browser verification passed; see `../../docs/onboarding-integration.md` for the
separate layout, keyboard, and live-transport fixture evidence.

## Known limits

- `light-dark()`, `color-mix()` and `:has()` need a current browser (Chromium 123+, Firefox 120+,
  Safari 17.5+). `text-wrap: balance/pretty` degrades to normal wrapping.
- The preview Google flow is simulated with a timer; the live flow opens a provider window and polls status.
- Account check on the hub uses fixture outcomes in preview and lower authentication checks in live mode.
- The `.disclosure` component is styled but unused: the current form has no advanced options section.
  It is kept so a future “Advanced options” block can use progressive disclosure without new CSS.
