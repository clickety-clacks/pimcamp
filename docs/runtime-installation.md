# Runtime installation

The runtime is a local command-line boundary, not a background mail server.
Each request starts a process; `subscribe_new_mail` stays open for its client
and owns its observation process. Installing the runtime does not automatically
start a watcher, onboard an account, copy secrets, or send mail.

Requirements: Linux and Python 3.12 or later. For the verified IMAP/SMTP stack,
install Himalaya 2.1.0 and Carillon 0.1.0 from their upstream releases. Ortie is
needed only for an OAuth-based deployment; plain IMAP/SMTP does not require it.
Neverest is optional and is not installed by this MVP.

## Release layout

Published Pimcamp source archives retain `pimcamp` beside `src/pimcamp/`.
Extract into a new versioned directory, for example
`~/.local/lib/pimcamp/releases/v0.1.0`, and link `~/.local/bin/pimcamp` to that
directory's executable. The launcher resolves symlinks before loading its
modules. Keep the previous version for rollback when upgrading.

Verify the downloaded archive against the release's `SHA256SUMS` before
extracting. Upstream dependency archives should likewise be checked against
their published release-asset digests. Do not replace an existing account
configuration or credential store when updating software.

## Account configuration

Use the closed configuration shape in [README.md](../README.md), with private
absolute paths to the installed tools, separate lower account configurations,
owner-only state, and a high-entropy client credential represented in the
configuration only by its SHA-256 digest. Mail credentials belong in protected
storage accessed through credential commands, not in the repository or shell
history. The caller supplies its client credential through private stdin.

Default configuration: `~/.config/pimcamp/config.json`, respecting
`XDG_CONFIG_HOME`. An explicit `PIMCAMP_CONFIG` overrides it. Software installation
alone is not proof that an account is configured or that live mail works.

Kernel-keyring-only credentials are volatile across reboot and must not be
described as persistent production onboarding. The generic onboarding UI remains
separate work; this release does not ship an installer that silently collects or
copies account secrets.

## Verification

From the extracted directory, run:

```sh
python3 -m compileall -q src tests pimcamp
python3 -m unittest discover -s tests -v
```

Missing-account skips are expected on a software-only installation and do not
prove live integration. The Racter evidence is documented in
[racter-runtime-acceptance.md](racter-runtime-acceptance.md).
