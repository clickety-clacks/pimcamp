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

## Revision 3 (2026-09-08): guided Google sign-in setup

The Google screen’s “no OAuth client” state used to be a dead end: a warning, a dashed “Administrator
setup” box with a callback address, and no way forward inside the UI. This revision replaces it with a
one-time, guided **Set up Google sign-in** flow that registers *this installation* with Google. It is
deliberately separate from choosing a mailbox: the account draft (address, name, reconnect id) is kept
throughout and the user returns to the ordinary Google sign-in screen when the registration is saved.

**Shape of the flow.** One screen key, `google-setup`, with five parts held in `state.googleSetup.step`:

| Part | `step` | Title | What happens |
| --- | --- | --- | --- |
| Intro | `intro` | Set up Google sign-in for this installation | Why this is needed, in plain words; the three parts as an overview; what you need (any Google account that can open Google Cloud console). Warning if Ortie is missing. Primary “Start”. |
| 1 | `project` | Create a Google Cloud project and turn on Gmail | Two numbered steps with “Open …” links to Google’s project-creation and Gmail API pages. |
| 2 | `platform` | Describe the app and create its client | Branding, Audience (External vs Internal, Testing consequences), Data access (`https://mail.google.com/`), Create a **Desktop app** client and download its JSON. |
| 3 | `import` | Bring the client file to this installation | Native file picker only. Preflight of the file’s shape, then one send to the installation. Storage-missing, invalid-file, save-failed and unconfirmed states. |
| Done | `saved` | Google sign-in is set up on this installation | Summary: where it was saved, public client ID, “client secret in protected storage”, “Google account: not connected yet”. Primary “Continue to Google sign-in”. |

A compact part indicator (`.stepper`: three bars with labels, current label only under 480px) sits under
the lede on parts 1–3 and shows all parts complete on the Done screen. The shell rail still shows the
five account steps with “Settings” current and the eyebrow “Set up Google sign-in”, so the user can see
this is a detour inside adding an account, not a different product.

**Why the copy avoids OAuth vocabulary.** Users are told they are “registering this copy of Pimcamp
with Google”, that a project is “the container Google uses for this registration”, that the Gmail API
switch “lets the registration be used for mail”, and that the download is “the client file”. The
words users will see on Google’s pages (Branding, Audience, Data access, Clients, Desktop app,
Download JSON, Test users, External/Internal) are used verbatim so they can match them. “OAuth”,
“scope”, “redirect URI” and “consent screen” do not appear as terms to understand.

**Honesty rules baked into the copy.**

- Personal @gmail.com addresses use **External**; **Internal** only for a Workspace organization the
  user administers and every mailbox belongs to. Organization restrictions are to be raised with the
  administrator, never worked around.
- External apps start in **Testing**: each mailbox must be a test user, and Google ends mail access
  after 7 days, requiring another sign-in. Publishing for longer-lived access is
  Google’s process; the UI does not promise unattended long-term access.
- Registration happens on Google’s official pages, opened in a new tab by plain links styled as
  buttons (`rel="noopener noreferrer"`, visually hidden “(Google, new tab)”). Nothing Google-hosted is
  embedded, and no Google login happens inside the setup UI.
- A saved registration is described as *saved*, never as a connected account. The Done screen states
  “Google account: Not connected yet” and hands off to the ordinary Google screen, where “Continue with
  Google” still needs a click. `beginOAuth` is never called automatically.
- In the preview, the Done screen and any error state carry the purple Demo notice.

**Secret handling.** The file is read only when the user presses Save. Its text lives in a local
variable for the duration of one `configureGoogleApplication` call and is nulled immediately after
the request is issued; nothing from it is rendered, logged, put in a URL, kept in `state`, or written
to browser storage. On success the whole part is replaced by a loading state at once, which destroys
the file input; on Back or Cancel the screen is replaced, likewise destroying it. After a *failed*
attempt the chosen file stays in the native input so the user can retry without re-picking. The only
value the UI ever shows afterwards is the public client ID that the installation reports. Fixtures
hold no client secret or sample client file; `DEMO_CLIENT_ID` is a public-identifier-shaped string.

**Error and edge states.**

- *Status check failed*: “Couldn’t check this installation” with the service’s message, Back and Try
  again. Nothing is changed.
- *Google helper missing* (`helperAvailable: false`): a warning notice on the intro and on part 3
  with “Check again”. The Google parts can still be done; Save re-checks status first and refuses
  to send the file while Ortie is unavailable. Protected storage is checked separately when saving.
  The notice is updated in place so a chosen file is not lost.
- *Invalid file*: preflight messages distinguish “not a client file”, “Web application client, needs
  Desktop app”, and “missing client details”; files over 32 KB and unreadable files are rejected before
  any send. Messages are linked to the field with `aria-describedby`, the input gets `aria-invalid`,
  focus moves to it, and the assertive region announces it.
- *Save failed* (service threw): “Saving could not be confirmed” with the service’s user-facing message
  verbatim, a “Check what was saved” button, and Save available for retry.
- *Unconfirmed* (no answer within 45 s, or an answer without `configured: true`): “Saving could not be
  confirmed”. The original request keeps running; if it later succeeds and this attempt is still
  current, the screen moves to Done. “Check what was saved” asks the installation instead of guessing.
- *Already configured* when entering the flow (someone else finished): the flow opens on Done with
  “Already saved” and refreshes the installation facts.
- *Refresh failed after save*: Done still shows, with a notice telling the user to choose Check again on
  the Google screen. The installation shape is only ever updated from `beginSetup`.
- *No endless spinner*: save, status checks and installation refresh are bounded by a 45-second
  timeout. Navigation is hidden during saving; after an unconfirmed outcome it becomes available
  again, retaining the account draft.

**Fallback.** If the live service lacks `googleApplicationStatus` / `configureGoogleApplication`, the
Google screen keeps the plain-language explanation and “Check again”, adds a sentence that guided setup
is not available from this version, and omits the primary button.

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
  head aligns with the title, not the icon. (The dashed administrator box this revision added was
  replaced by the guided setup flow in revision 3.)
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
| 5 | Google | `google` | ready, consent in progress (cancellable), cancelled (by user / at Google), expired, organization policy denied, OAuth client missing (explains the one-time registration; primary “Set up Google sign-in” or “Continue setting up…”), authorized matching account, authorized different account (explicit choice) |
| 5a | Google sign-in setup | `google-setup` | intro, part 1 project, part 2 app and client, part 3 client file (storage not ready, file rejected, saving, save failed, unconfirmed), saved (incl. already configured, refresh failed), status check failed |
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
  A missing OAuth client explains the one-time registration and leads into the guided
  `google-setup` flow (see Revision 3); “Check again” and “Use IMAP & SMTP instead” remain.
- **Google sign-in setup** keeps `state.draft` untouched throughout. Back moves one part back (from the
  intro, back to the Google screen); Cancel returns to the Google screen from any part; both destroy
  the file input. The part reached is remembered in `state.googleSetup.step`, so returning later says
  “Continue setting up Google sign-in” and resumes there; the installation’s status is re-read on every
  entry. Announcements say “<title>. Google sign-in setup, part N of 3.”
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
| `googleApplicationStatus()` | Whether this installation is registered with Google and whether the Ortie executable is available (not a vault-readiness check) | `{ configured: boolean, helperAvailable: boolean, clientId?: string }`. `clientId` is Google’s public identifier; no secret is ever returned. Only explicit `helperAvailable: true` means available; missing/empty `clientId` means “not reported”. |
| `configureGoogleApplication({ credentialsJson })` | Validate a Desktop app client file, store its secret in the protected vault, persist the public configuration | `{ configured: true, clientId: string }`. Anything else (a throw, or a result without `configured: true`) is treated as *not confirmed*: the UI shows a retry and a “Check what was saved” action and never claims success. Saving connects no account. |

Both are implemented by `createDemoService` (outcome chosen in the demo panel; the demo validates the
file’s shape with the same `inspectClientJson` preflight and keeps only the public client ID). After a
confirmed save the presentation calls `service.beginSetup(draft.reconnectId || undefined)` to refresh
`state.installation` (existing shape, `oauthClientConfigured`), which also lets the live service cancel
an expired setup attempt. `beginOAuth` is never called as part of this.

Presentation additions in this revision (the parent’s test may rely on them): template ids
`tpl-google-setup`, `tpl-gsetup-intro`, `tpl-gsetup-project`, `tpl-gsetup-platform`,
`tpl-gsetup-import`, `tpl-gsetup-saved`; slots `eyebrow`, `title`, `lede`, `stepper`, `demo-notice`,
`body`, `actions`, `storage`, `keep`, `file-meta`, `file-name`, `save-error`, `save-error-title`,
`save-error-text`, `summary`, `refresh-note`; `data-action` values `setup-back`, `setup-cancel`,
`setup-next`, `setup-save`, `setup-continue-google`, `recheck-storage`, `check-app-status`; form id
`gsetup-import-form`; field `f-client-file` with `f-client-file-hint` / `f-client-file-error`;
classes `.screen__eyebrow`, `.stepper*`, `.gsetup*`, `.guide*`, `.filepick__meta`, `.notice--top`,
`.notice__actions`; icons `i-file`, `i-cloud`, `i-id`; `state.googleSetup` = `{ step, app, statusError,
submitting, error, token, refreshFailed, alreadyConfigured }`. Removed: the `admin` slot, the
`callback` slot, the `.admin*` classes and the `callbackUrl()` helper (a Desktop app client needs no
registered redirect address). Demo settings gained `googleHelperAvailable` and `googleSetupOutcome`
(`saved` | `rejected` | `save-failed` | `unconfirmed`). `PimcampOnboardingPreview` is unchanged.

Expectations for the real implementation, from the spec:

- Config paths follow XDG conventions on the installation; the UI never sees or shows them.
- Credentials go to the existing protected storage; the UI passes them once, in a request body, and
  discards them after commit. Tokens are refreshed by the OAuth helper, not by the UI.
- `beginOAuth` should open the provider URL returned by the backend and wait on a backend-owned status
  endpoint; the callback port is internal to the backend and is not shown in the UI.
- `configureGoogleApplication` should reject anything that is not a Desktop app client (`installed`
  block) with a user-facing message, store the secret only in the protected vault, and never echo file
  contents back. Its `clientId` is displayed on the Done screen as-is.
- `commitAccount` must fail without side effects when the account exists and this setup is not a
  reconnect of that account id.
- Error messages sent to the UI should already be user-facing; the UI displays `message` verbatim and
  never shows tracebacks or internal identifiers.

**View model** (`state`): `screen`, `accounts`, `installation`, `draft` (nonsecret form state plus
in-memory passwords), `oauth` (`status`, `identity`, `controller`), `connect` (`rows`, `running`,
`cancellable`, `saved`, `accountId`), `googleSetup` (`step`, `app`, `statusError`, `submitting`,
`error`, `token`, `refreshFailed`, `alreadyConfigured`; never file contents). Renderers read from
`state` and templates; they never call the service directly except through the handlers in the
navigation section.

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
- Installation host and remote flag, whether a Google OAuth client is configured, and whether the
  Google sign-in helper is available.
- Simulated outcomes for saving the Google registration (saved / rejected / fails / never answers),
  Google authorization, incoming and outgoing sign-in, saving, and mail watching. To exercise the file
  picker in the preview, use an `installed` object containing a Google-shaped `client_id` and a
  nonempty `client_secret` (at most 4096 characters, no NUL). A file with a `web` block is rejected.
  No sample client file ships.
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
`google-progress`, `google-missing`, `google-mismatch`, `google-setup-intro`, `google-setup-platform`,
`google-setup-import`, `google-setup-import-invalid`, `google-setup-storage-missing`,
`google-setup-save-failed`, `google-setup-saved`, `review-imap`, `connect-incoming-failed`,
`connect-observe-unavailable`, `done`, `done-partial`, each at 1280 and 375px, light and dark.

Gallery states added in revision 3 (all existing names are unchanged): `google-setup-intro`,
`google-setup-storage-missing`, `google-setup-unavailable`, `google-setup-project`,
`google-setup-platform`, `google-setup-import`, `google-setup-import-invalid`, `google-setup-saving`,
`google-setup-save-failed`, `google-setup-saved`. Each renders its heading synchronously with a
pre-answered status, so the gallery walk needs no service call.

**Status of revision 2.** The authoring session could not launch Chromium, Node or Python (command
approval was unavailable), so the browser review and screenshots were not run here. What was verified:
every id, template, slot, action, field and class contract listed above is present in `index.html`;
the two byte-exact strings the setup server rewrites are unchanged; the stylesheet’s braces balance and
every selector the renderers rely on has a rule. Subsequent integration-owned
browser verification passed; see `../../docs/onboarding-integration.md` for the
separate layout, keyboard, and live-transport fixture evidence.

**Status of revision 3.** Designed directly with Fable 5.1, then integrated and tested separately.
Chromium passed 210 layout checks (35 states, three widths, two themes) and 23 interaction checks,
including duplicate submission, timeout recovery, late success and ignored stale responses.
Desktop/light and narrow/dark screenshots were also visually inspected. The real local HTTP boundary
passed a separate 10-check browser fixture journey; this is not real Google consent. Isolated Secret
Service persistence/restart and Ortie 2.2.0 request construction passed without contacting Google.
The guide follows the linked official Google documentation; a human-authenticated walk through the
Google console and real mailbox consent remain unverified.

## Known limits

- `light-dark()`, `color-mix()` and `:has()` need a current browser (Chromium 123+, Firefox 120+,
  Safari 17.5+). `text-wrap: balance/pretty` degrades to normal wrapping.
- The preview Google flow is simulated with a timer; the live flow opens a provider window and polls status.
- The Google setup guide describes Google’s console as of the Google Auth Platform layout (Branding,
  Audience, Data access, Clients). If Google renames pages, the copy and the “Open …” links in
  `tpl-gsetup-project` / `tpl-gsetup-platform` are the only places to update.
- `::file-selector-button` styling needs Chromium 89+, Firefox 82+, Safari 14.1+; older browsers show
  the native button, which is still usable.
- Account check on the hub uses fixture outcomes in preview and lower authentication checks in live mode.
- The `.disclosure` component is styled but unused: the current form has no advanced options section.
  It is kept so a future “Advanced options” block can use progressive disclosure without new CSS.
