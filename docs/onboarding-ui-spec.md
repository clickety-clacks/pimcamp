# Pimcamp account onboarding UI

Status: implementation brief, 2026-09-04. Owner: Mike's direct Codex session.
Design implementer requested by Mike: Claude 5.1; do not substitute silently.
Claude is commissioned directly, not through Tightbeam. Codex owns integration.

## Product objective

Anyone installing Pimcamp on Linux can connect their own email account through an attractive, understandable browser interface. One account setup configures the existing operations, credential, and observation components consistently. No personal email address, home directory, workstation, provider, or Tailscale hostname is a product default. Racter is our test environment; Gibson is a later production target.

Pimcamp owns the integrating UI. Himalaya owns protocol operations; the installed observation adapter owns mail watching; the OAuth helper owns token acquisition/refresh. Inspect their current supported configuration interfaces before integration. Reuse existing capabilities instead of implementing another mail or OAuth client.

## Scope and screens

1. Accounts / welcome: empty state titled “Connect your email”, brief explanation of what Pimcamp enables, primary “Add account”. Existing accounts show address, optional display name, connection status, and “Check connection” / “Reconnect”. Never silently replace an existing account.
2. Choose connection: “Google / Gmail” and “IMAP & SMTP”. Explain that Google Workspace custom domains use the Google option. Namecheap may be an optional preset within IMAP settings, never a top-level product identity. No speculative unsupported provider buttons.
3. Account details: user-entered “Email address” with example alex@example.com; optional “Account name” used only locally. Accept @ and . in account names. Do not infer the account being installed from the operator, current browser session, or test fixture.
4. IMAP & SMTP: separate “Incoming mail” and “Outgoing mail” groups. Each has clearly labeled server hostname, port, connection security, and username. Defaults: IMAP TLS/993; SMTP TLS/465, with explicit STARTTLS/587 support where the backend supports it. Username initially follows the entered email but remains editable. Password field has reveal control, password-manager-compatible attributes, and help explaining app passwords where applicable. “Use the same login for outgoing mail” exposes separate outgoing credentials when unchecked. Presets only fill editable values. Advanced options use progressive disclosure; no hidden plaintext fallback.
5. Google: “Continue with Google”; explain that authorization happens at Google and Pimcamp does not collect the Google password. Show consent-in-progress, cancellation, expiry, missing OAuth application configuration, and organization-policy denial as distinct actionable states. Verify backend support, callback routing, client registration requirements, and scopes. Do not promise one-click login if the installation lacks a usable OAuth client. Any administrator configuration belongs in a clearly separated setup section, not ordinary mailbox fields. Use the returned authorized account identity and let the user confirm an unexpected account.
6. Review and connect: summarize address, account name, provider/method, incoming/outgoing host/port/security, and installation host when remote. Never echo passwords or tokens. Primary “Connect account”, secondary “Back”. One meaningful confirmation, not repeated approvals.
7. Connection results: show separate rows for incoming authentication, outgoing authentication, configuration saved, and observation readiness when supported. Use precise states: waiting, checking, passed, failed, unavailable. SMTP authentication is not proof of delivered mail; starting a watcher is not proof of a received event. Do not send test emails automatically during account configuration. Retry only failed operations when possible, preserving safe entered values.
8. Success: “Account connected”, address and enabled capabilities, return to accounts / add another account. Partial failure stays visible with a specific repair action; never display success when nothing was written. Explain any remaining prerequisite in ordinary language.

## Visual and interaction design

Create a polished, restrained utility: warm neutral background, clear typography, generous spacing, subtle borders and surface elevation, one accent color, consistent icons, no decorative dashboard clutter. Provide light and dark themes using CSS tokens and system preference. Use locally available/system fonts and bundled assets, no mandatory remote fonts/CDNs. Desktop content width approximately 920px with a compact progress rail and 560px form; collapse to a single column on mobile and narrow scratchpad windows. Minimum usable width 320px. All screens must look intentionally designed, including loading, empty, partial failure, and OAuth cancellation.

Semantic HTML and native form controls; keyboard-complete operation, visible focus, programmatic labels, error descriptions linked to controls, status announcements via aria-live, adequate contrast, reduced-motion support, no color-only meaning. Back preserves nonsecret form state. Disable duplicate submission during requests; do not disable navigation indefinitely. Loading always explains the operation and offers cancellation when safe.

Copy examples: “Email address — the address people send mail to”; “Incoming server — for example imap.example.com”; “We couldn’t sign in to incoming mail. Check your username and password.” Never expose raw tracebacks, coordination IDs, keyring terminology, or internal file paths in normal flows.

## Implementation and integration contract

Deliver reusable semantic HTML/CSS and minimal JavaScript using the repo’s existing rendering approach. Separate presentation from the onboarding service through a small view model and action boundary: list accounts, begin setup, validate fields, begin/cancel OAuth, check connections, commit account, and obtain status. Reuse existing endpoints where suitable; document actual payloads and errors after inspecting them. No new framework or service merely for the mockup.

Provide a runnable fixture preview with every state, explicitly marked as demo data, then wire the same components to real handlers. Fixtures must never masquerade as successful live checks. Configuration paths derive from the installation environment/XDG conventions. Account configuration and credential references must be account-scoped and additive. Preserve the existing account on failure and clean only setup-owned temporary state.

Serve one stable local setup origin. Default local-only access; remote test access uses explicit host configuration and correctly routed OAuth callbacks. A callback port is an implementation detail, never a competing setup URL. Keep secrets out of URLs, argv, logs, browser persistent storage, and rendered summaries; normal OAuth protocol parameters follow the provider’s supported flow. Use existing protected credential storage. Tokens must survive through supported refresh handling, not a one-time login illusion.

## Deliverables and acceptance

- Formal screen inventory, visual tokens, interaction/error states, and brief ownership mapping to the installed stack.
- Runnable HTML/CSS preview, screenshots at desktop and 375px width, keyboard walkthrough, light/dark states.
- Integrated UI in the Pimcamp repository, packaged as part of a normal Linux installation, plus concise user documentation.
- Generic IMAP setup works against configurable test endpoints; alternate address and custom host values persist correctly. Namecheap works as a preset, not hardcoded behavior.
- Google flow uses a user-selected account and accurately handles installation prerequisites and consent. Human consent is the only required user handoff once prerequisites are ready.
- Integration checks cover duplicate submission, invalid fields, rejected credentials, OAuth cancellation, partial connection failure, safe retry, saved configuration, and preservation of an existing account. No live test mail without an explicit send action.
- Real supported onboarding verified on Racter; no Gibson production rollout is implied.

Sequence: inspect existing code and lower interfaces; Claude 5.1 creates the designed preview; review visuals and interaction against this spec; integrate the same UI; test and document. Keep one responsible implementer and proportional review. Do not re-open design on every small correction.
