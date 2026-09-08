# Onboarding integration boundary

Implementation notes from direct inspection on 2026-09-04. The UI requirements
are in [onboarding-ui-spec.md](onboarding-ui-spec.md). Claude is commissioned
directly for the HTML/CSS/JavaScript design; this is not a Tightbeam assignment.

## Baseline inspected on 2026-09-04

The repository's executable implements the seven mail operations over JSON
stdio. It has no account setup HTTP boundary. The Racter test installation has
a separate Python onboarding server outside this repository. That server is
installation-specific, not a distributable generic onboarding implementation:

- Incoming and outgoing hosts and ports are fixed to one provider.
- Gmail address, alias and OAuth storage entry are fixed constants.
- Binary and configuration paths are fixed to the test machine.
- Password onboarding creates the initial configuration rather than adding
  another account. Gmail has an additive configuration path.
- Password authentication checks both IMAP and SMTP without sending mail.
- Atomic file writes and rollback helpers exist, but they are not a
  multi-account transaction spanning all configuration and secret changes.

Do not deploy a cosmetic replacement over these assumptions. No existing
account, credential or live configuration has been changed by this inspection.

## Integration requirements

The repository now contains account-scoped adaptations of the installed Linux
keyring helpers in `scripts/pimcamp-keyctl-read` and `scripts/pimcamp-keyctl-write`.
They accept `account:<64 lowercase hex characters>:imap|smtp|oauth|client`.
The opaque identifier must identify an account credential revision, not a
human label or email address. New setup uses `--create-only`; token refresh can
update only its own exact OAuth entry. The old Racter helpers and fixed entry
names remain untouched. No live key operations were performed in the helper
tests.

Linux keyring credentials are volatile. These helpers preserve the current
test stack's storage model; they do not establish persistence across reboot.
The launcher now defaults to libsecret's `secret-tool`, backed by the desktop
Secret Service's default collection. It requires a working, unlocked provider
such as a desktop keyring; storage protection and persistence follow that
provider's configuration. `--credential-backend linux-keyring` is an explicit
volatile test option, and the review screen warns about its lifetime. There is
no silent fallback to volatile storage or plaintext configuration.

Account publication now has a local tested store: a stable account link points
to a complete immutable configuration revision. Reconnect compares the original
revision before replacing that link. Existing revisions remain available to
in-flight readers; revision/credential retention still needs an explicit policy.
This store is connected to the local setup service and authenticated HTTP
handlers. Isolated acceptance on Racter is recorded below; its existing mailbox
deployment has not been replaced.

Configuration generation produces separate strict Himalaya and Carillon files,
plus a Pimcamp profile pointing to both. Generated profiles are tested through
Pimcamp's actual configuration parser. Himalaya's installed `account check`
command is the intended per-backend authentication operation; setup does not
use a send or mailbox-read command to test a login.

## Current launch and remaining integration

`scripts/pimcamp-setup` launches the local UI on port 33281 by default and never
silently changes ports. The launcher writes a short-lived, owner-only HTML
handoff under the runtime directory and opens it in the browser. Its token is
submitted in a POST body, not a URL or command argument. The server consumes the
handoff once, deletes that file, and issues an HttpOnly session cookie. Actions
require the exact local Host and Origin, the session cookie, and a CSRF header.
Request bodies are bounded and no request/provider details are logged.

The frontend uses the real service only when served by that boundary. Opening
the checked-in HTML file directly remains an explicitly labeled fixture preview.
Live mode cannot fall back to demo actions or use gallery links to synthesize a
successful connection.

Browser-to-server acceptance has passed with isolated fixture credentials and
lower authentication checks replaced by a fixture checker. It proved the local
launch-file POST/cookie handoff, real HTTP actions, custom account publication,
HttpOnly cookie behavior, secret-free listings, and truthful partial observation
status. It did not authenticate against a mail provider or write a real keyring.

The first saved account now selects its stable account configuration as the
normal runtime default, without replacing any existing runtime configuration.
Reconnect updates the account link so that the selected default follows it.

Still incomplete: importing the earlier installation's externally managed
account; headless production keyring unlock configuration; browser-level
Google consent verification and release acceptance. Profiles
must not be called production-ready before these requirements are resolved.

### Candidate installation and design review

`python3 scripts/install --version <unique-release-name>` now installs both the
mail CLI and `pimcamp-setup`, including backend modules, credential helpers and
all UI assets. It preserves old releases and account data, refuses unrelated
executable replacement, and does not install the lower mail tools. Isolated
installation tests include launching the installed setup command's help output.

On 2026-09-08 the candidate passed 120 ordinary tests on Racter, with two live
tests skipped. Claude completed the direct HTML/CSS design commission. Eezo's
existing Chromium build then passed 150 gallery/layout checks (1280, 375 and
320px, light/dark) and eight interaction checks; desktop and mobile screenshots
were inspected. These checks exercise fixture flows, not real account consent.
Racter's cached Chromium could not start with its host sandbox policy; no
sandbox policy was changed.

Real IMAP onboarding acceptance subsequently passed on Racter using the existing
authorized test account through protected credential commands. It authenticated
IMAP and SMTP with the installed lower tools, saved an isolated account profile,
selected that profile as the isolated runtime default, and completed a public
Pimcamp `list` request. The source configuration remained byte-for-byte unchanged;
all acceptance-owned credential entries were removed afterward. No mail was sent
or filed. This verifies the explicit volatile Linux-keyring lane, not persistent
Secret Service storage. The opt-in test is `tests.test_live_onboarding`; ordinary
test runs skip it unless the documented environment variables in that file are set.

Persistent Secret Service acceptance also passed on Racter using
`python3 tests/support/secret_service_probe.py`. This launches a private D-Bus
session and GNOME Keyring with temporary XDG directories and generated test
values. It verifies create/read, account-owned OAuth refresh writes, persistence
across daemon restart plus unlock, absence of the generated values in the stored
keyring file, and exact-item deletion. It neither uses nor unlocks the operator's
keyring. Temporary data and the private daemon are removed on exit. This is not
proof of unattended unlock after a host reboot; that depends on the installation's
login/keyring configuration and must be documented for a headless deployment.

The latest ordinary Racter gate ran 123 tests successfully with three opt-in
live tests skipped, plus three Node transport checks. The IMAP onboarding live
test and isolated Secret Service probe were enabled and passed separately.
The final pre-commit Eezo browser run passed 150 layout checks and 13 interaction checks, including real
Tab, text-entry and Enter events through account details and IMAP/SMTP settings
to a secret-free review screen, Back preserving account details, recoverable
connection-check errors, truthful save uncertainty, and unverified watcher copy.

`node tests/onboarding_transport.mjs` verifies Google transport lifecycle without
network access: a provider-detached window does not imply cancellation, a retry
waits for the previous start request and cleanup, and failure clears the pending
grant. These are deterministic transport checks, not proof of real Google consent.

The redesigned frontend also passed all seven browser-to-server fixture checks
on Eezo: authenticated launch, real HTTP transport, custom account publication,
secret-free review/listing, HttpOnly session cookie, and truthful observation
status. This run exposed and fixed revision containment checks when a store's
parent directory is a legitimate symlink. A new regression test passed on Racter.
The fixture server was stopped after verification; it used no real mail or keys.

### Gibson candidate deployment (2026-09-08)

Candidate commit `e00d9dcd1693ed2402af2e9bf60c13c4e3037251` was installed
using its included installer on the authorized production target. Both the mail
CLI and setup launcher now resolve to the candidate release, and the installed
setup command's help invocation succeeded. The earlier v0.1.0 release remains
intact. Installation did not copy credentials, configure a mailbox, or send mail.

The host's Secret Service returned no default collection. A persistent operator
terminal is awaiting a new encrypted-storage password before account setup can
save credentials. This is not evidence of an onboarded production mailbox or
unattended keyring unlock. The active test mailbox remains unchanged. Deployment
paths and operator identity belong in the private environment runbook, not in
the reusable UI or agent skill.

### Google integration evidence

The authorization adapter targets Ortie 2.2.0. Its supported `auth get --json`
produces the authorization URI, state and PKCE verifier. The adapter validates
Google's endpoint, the configured client/callback/scope, and the S256 challenge.
`auth resume` is sent through Ortie's supported stdin REPL, followed by a
nonsecret token-metadata check; authorization codes and verifiers do not enter
process arguments or a shell history file. Callback policy tests cover wrong
state/address, cancellation, organization denial and expiry.

An offline construction probe passed using a copy of the actual installed
Racter Ortie binary, a fake client ID, and disabled credential commands. It
validated the real binary's authorization-request output without contacting
Google, obtaining consent, reading a token or writing credentials. This is not
evidence of a completed Google login. Browser callback routing, provider-verified
account identity, cancellation, and refresh-backed account publication are now
wired. Private-adapter tests cover saving a Google account, preserving its token
references after temporary cleanup, rejecting unconfirmed identity changes, and
rejecting unrelated callbacks before exchange. No real Google consent or token
refresh has yet been verified.

The launcher accepts `--google-client-id` and `--ortie`. An optional
`--google-client-secret-command` points to an absolute installation-owned helper;
the client secret itself must never be supplied as an argument. Without an
application ID the UI presents the installation prerequisite, not a nonfunctional
sign-in promise. The callback uses the same fixed loopback origin as setup.

Keep account setup separate from the seven mail-operation request envelopes.
Ship the setup UI with Pimcamp, backed by an owner-local setup boundary. Reuse
the lower tools and credential helper rather than implementing another mail
client or token-refresh service.

The backend must accept explicit account identity and editable incoming and
outgoing settings. Allocate secret entries per account and per credential,
never one global `imap` or `gmail-oauth` entry. Resolve paths from installation
configuration and XDG locations. Preserve existing accounts and default-account
selection when adding an account.

An account is not usable through Pimcamp merely because Himalaya accepted its
configuration. Setup must also select/configure the operations and observation
adapters consistently. Distinguish authenticated, saved, process started and
new-mail observation verified; none implies the next.

Serve UI and setup actions at one stable, loopback-bound origin by default.
Require a session-bound authorization/CSRF mechanism, validate Origin and Host,
limit request size, and do not expose credentials in URLs, responses or logs.
Remote browser use needs explicit secure transport and callback routing, not
a hard-coded test-host address or an unprotected LAN listener.

## Verification before deployment

1. Preview clearly identifies fixtures and never claims to configure mail.
2. Validation covers arbitrary addresses and account names containing `@` and
   `.`, separate SMTP credentials, TLS/STARTTLS and field-specific errors.
3. Fake-backend tests cover configuration preservation, duplicate accounts,
   failure rollback, retry, request authorization and secret redaction.
4. Existing repository tests still pass; live tests are reported independently
   from fixture tests and skips.
5. Racter remains the test target. Verify a real supported onboarding path
   there before proposing a production deployment on Gibson. Do not send mail
   as an implicit side effect of connection checks.
