# Agent-assisted account setup

This is the operating guide for agents installing Pimcamp and opening its
included account setup UI for a user. Deployment hostnames, usernames, accounts,
and credential-helper paths belong in the installation's environment/runbook,
not in this guide. Install the [Pimcamp skill](../skills/pimcamp/SKILL.md) for
subsequent email operations.

## What the agent owns

Run installation and launch commands for the user. Keep ownership of the running
setup process, secure transport, and verification after private input. The user
enters their email settings in the HTML UI and authorizes Google at Google.
Never ask them to paste passwords, OAuth codes, or tokens into chat.

1. Establish which Linux host and OS user will own mail configuration. Do not
   infer these from where the browser or coding agent runs. Check existing
   accounts and runtime configuration without printing credentials.
2. Follow the [candidate installation instructions](../README.md#install-the-development-candidate).
   Verify Python 3.12+, Himalaya 2.1.0, Carillon 0.1.0, and installed executable
   paths. Use a unique release name and preserve existing configurations and
   releases. The installer does not install lower tools or onboard an account.
3. Establish working encrypted password storage **before asking for email
   credentials**. See the next section. Do not silently fall back to plaintext
   files or the explicitly volatile Linux-keyring test backend.
4. Launch `pimcamp-setup`, not `ui/onboarding/index.html`. The latter is a labeled
   demonstration and cannot configure email. Use the remote procedure below
   when the browser is not on the mail host.
5. Guide the user through Add account, connection method, their own email
   address, editable server/login settings, review, and Connect account. A local
   account name can contain `@` and `.`. Never prefill a developer's identity.
6. Verify the actual result. Incoming/outgoing authentication, saved account
   configuration, and new-mail observation are separate states. Connection
   checks do not send mail. A saved account is not proof of delivered mail or a
   received notification. Use the public Pimcamp interface for an authorized
   read-only verification; do not disclose credentials in doing so.

## Explain password storage before prompting

Use plain language such as:

> Pimcamp needs a safe place on the mail host to keep your email passwords and
> sign-in tokens. Linux calls this encrypted password vault a “keyring.” It lets
> Pimcamp access your account without asking for the email password every time.
> The vault password unlocks that storage; it is not a password requested by
> your email provider. While unlocked, programs running as your OS user may be
> able to access its stored credentials.

For **creation**, add:

> Choose a new, strong vault password and confirm it. It does not need to match
> your email or computer login password. Save it in your password manager with
> the mail host's name. You may need it after a restart; without it, stored
> credentials may be inaccessible and the email account may need reconnecting.

For **unlocking**, say instead:

> Enter the existing vault password you chose earlier. This unlocks the same
> vault; it does not create a new one or change your email password.

Current limitation: the included HTML UI does **not** initialize or unlock a
headless host's vault. Use the installation's verified Secret Service provider
setup/unlock procedure, explaining why this separate prerequisite is appearing.
Do not present a one-off operator helper as a packaged Pimcamp feature.

If private input genuinely needs a terminal, open a persistent task-specific
tmux session and surface it using the user's desktop conventions. Preserve its
nonsecret result for verification. Do not ask the user to run commands that the
agent can run. Never put secrets in argv, shell history, captured output, or logs.

Check that the credential client and unlock procedure target the **same running
Secret Service**, under the same OS user and D-Bus session. An installed
`secret-tool`, a vault file, or an unlock command's zero exit status is not proof
of working storage. Verify unlocked state and a temporary generated-secret
save/read/removal through Pimcamp's credential backend. Remove only the owned
test entry; never clear or recreate an existing vault to repair setup.

Do not assume `gnome-keyring-daemon --unlock` is a client command for an already
running daemon: starting competing services can leave Pimcamp talking to a
different, locked service. Unattended unlock after reboot is deployment-specific
and is not established by a successful interactive unlock.

## Open the live HTML UI

On a local desktop, run the installed `pimcamp-setup`. Supply absolute lower-tool
paths with `--himalaya` and `--carillon` if they are not on that process's PATH.
The launcher creates a private, short-lived HTML handoff and opens it in the
browser. That handoff authorizes the browser; it is not the demo preview.

For a browser on another machine:

1. Keep the setup process running on the mail host, for example in a named tmux
   session. Supply `--no-open --remote-host-label <mail-host-label>` and the
   required absolute tool paths. The service stays on `127.0.0.1:33281`.
2. Maintain an SSH tunnel from the browser machine to that same port. Template:
   `ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L 127.0.0.1:33281:127.0.0.1:33281 <mail-host>`.
   Resolve the placeholder from deployment documentation. If the port is busy,
   inspect/reuse the existing task tunnel; do not choose a random new port.
3. The launcher prints the origin and a `launch_file` path, **not its token**.
   Copy that exact file over SSH into an owner-only temporary directory on the
   browser machine, keep mode 0600, and open the file in the user's browser.
   Do not print its contents, put its token in a URL, or upload it to a public
   file-sharing service. Open promptly: the handoff expires after five minutes.
4. The browser POSTs the one-time token and receives an HttpOnly cookie. Confirm
   the live “Connect your email” page loads. Remove the local handoff copy after
   it is consumed; the server removes its own copy. Keep the tunnel and service
   alive while the user works. Google callbacks use this same origin/tunnel.

Google additionally requires an installation-owned OAuth application and Ortie
2.2.0. Install the helper first, passing its absolute path with `--ortie` when
needed. If no application is configured, use **Set up Google sign-in** in the
development UI to guide the user through Google's registration pages and import
the downloaded Desktop-client JSON. Do not collect that file through chat or
print its contents. Local registration save does not verify Google consent,
provider-side settings, or an email account; continue to account sign-in after
the save. Never assume the browser's current Google account is the desired email identity. See
the [integration notes](onboarding-integration.md#google-integration-evidence).

## Recover without misleading the user

- **Setup expired:** attempts currently expire after 15 minutes. With the same
  authenticated browser, reopen the live root page at port 33281 and start Add
  account again. Unsaved details may need re-entry. This does not require a new
  vault password. If browser authorization is missing, obtain a fresh launcher
  handoff; inspect the existing process before restarting anything.
- **Could not save to keyring:** check storage, not the email password first.
  Keep the form open while repairing storage. Verify the repair before asking
  for a retry; an expired attempt still needs restarting as above.
- **Saving could not be confirmed:** inspect saved account state before retrying
  or creating a replacement attempt. Do not claim nothing was written merely
  because the browser lost a response.
- **Missing observation readiness:** explain that the account may be saved while
  new-mail events remain unverified. Do not invent a successful notification.

After the user finishes, record verified deployment status in the environment
runbook. Clean up only this task's finished terminals, handoff files and tunnels
when no longer needed. Do not stop mail services or other agents' sessions.
