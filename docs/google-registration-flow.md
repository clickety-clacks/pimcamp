# Guided Google application setup

Implementation brief, 2026-09-08. Mike requested Fable 5.1 to design this flow
directly. It extends the existing HTML onboarding UI; it is not another setup
server, mail operation, or OAuth/token-refresh implementation.

## Required user journey

When Google sign-in has no application configuration, offer **Set up Google
sign-in** instead of directing the user to an unspecified administrator. Explain
that this one-time registration identifies the installation to Google, whereas
the later Google sign-in chooses and authorizes an email account.

Guide the user through selecting or creating a Google Cloud project, enabling
the Gmail API, configuring Google Auth Platform branding/audience/data access,
creating a Desktop app client, and downloading its JSON configuration. Open
official Google pages in the browser; never collect a Google password or embed
Google login inside Pimcamp. Retain the entered email and local account name
when leaving and returning to this guided flow.

Personal Gmail requires an External audience. Internal is only appropriate for
an eligible Workspace organization. External applications in Testing need an
allowed test user and mail grants expire after seven days. Do not imply that
Testing provides unattended permanent access, or advise bypassing organizational
restrictions. Pimcamp's current mail adapter requests `https://mail.google.com/`;
explain the broad mail access before consent rather than presenting it as only
basic profile access.

The Google-side procedure follows [Google's Gmail quickstart](https://developers.google.com/workspace/gmail/api/quickstart/python)
and [installed application authorization guide](https://developers.google.com/identity/protocols/oauth2/native-app).
Audience and grant lifetime are described in [Google's OAuth overview](https://developers.google.com/identity/protocols/oauth2).

## Import and continuation

Use a labeled file picker for the downloaded Desktop-client JSON. Do not render
its secret or store it in browser persistent storage. Clear imported content
when saved or cancelled, and explain that the downloaded file still exists on
the user's machine. Do not silently delete that user-owned download.

Import validates the client type and bounded client ID/secret fields. Provider
endpoints, redirect URI, commands, executable paths, and scopes come from trusted
installation code, never from imported JSON. A service-account or Web client
file must produce a useful correction, not a generic traceback.

Save the client secret in the installation's persistent encrypted credential
backend. The owner-only application manifest contains the public client ID and
opaque credential reference only. Publication must be atomic, retry-safe, and
must not overwrite another configured application or existing email accounts.
Failed publication cleans only the newly created secret. Runtime startup reloads
the application without a user passing its secret on the command line.

Success means **Google app configuration saved**, not **email account connected**.
Return to Google sign-in with the email draft intact and a fresh setup attempt
if necessary. The user must explicitly click Continue with Google so browser
popup permission remains tied to a user action. Existing OAuth exchange and
refresh remain Ortie's responsibility. Account identity is verified after
Google consent, not inferred from the imported client or browser login.

## Service boundary

Both actions use the existing owner-authenticated POST boundary, with exact
Host/Origin, HttpOnly cookie, CSRF token and bounded request body:

- `googleApplicationStatus` → `{configured, helperAvailable, clientId?}`.
- `configureGoogleApplication` with `{credentialsJson}` → `{configured: true,
  clientId}`. The response never returns the secret or imported JSON.

Executable installation is not a browser-submitted command. If Ortie or
persistent storage is absent, explain that prerequisite explicitly. The agent
owns allowed installation work; do not offer an enabled import that falsely
promises a working Google connection without those prerequisites.

## Acceptance

Verify light/dark and narrow/desktop layouts, keyboard operation, file selection,
wrong-file and storage-error recovery, cancellation, duplicate submission,
secret release, successful import, preserved draft, and return to account
consent. Exercise the real HTTP boundary and isolated persistent storage as well
as fixture presentation. Report those separately from real Google consent,
which requires the user's application registration and authorization. Existing
IMAP onboarding and its clear completed state must remain intact.
