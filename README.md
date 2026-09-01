# Pimcamp

Pimcamp is a narrow local mail boundary for one configured account. A client
uses one executable and seven JSON operations. It never needs Himalaya command
syntax, lower credentials, backend identifiers, or raw observation data.

The exact public capabilities are `list`, `read`, `compose`, `reply`, `send`,
`junk`, and `subscribe_new_mail`. There is no wildcard grant and no eighth
product operation.

## Authority

Build authority:

- Spec artifact: `art_4e44121c`
- Canonical spec commit: `bf8c76f8879184bb5dadcb23d7930d7e3ecbb509`
- Spec SHA-256: `05ba0b738f9cac4413c7bf859f9ba92b2319ffe0368906f68b261e211b44983b`
- Independent reviewed-clean verdict: `att_c06fccb3`
- Review report: `art_89006412`

The reviewed specification is the product authority. A ruled amendment for
truthful Mirador startup semantics is present at tightbeam-specs commit
`c898bd4e91b7d8b6f92f2ef421d7845e6868f2be`, artifact `art_8bc4f300`, SHA-256
`cda69c3763eb5e03efaa577686af196bd6a955b13ab9aa672bb1e16d37a7f542`.
The real observation binding remains held until that amendment has an
independent reviewed-clean verdict.

## Runtime

Pimcamp requires Linux, Python 3.12 or later, and an owner-only deployment
configuration. The repository-local executable is `./pimcamp`.

Finite calls read one request value from standard input, write one JSON line to
standard output, and exit with status 0 or 1. A subscription keeps standard
output open for normalized event lines and closes its lower process on client
`SIGTERM`.

The default configuration path is
`$XDG_CONFIG_HOME/pimcamp/config.json`, or
`~/.config/pimcamp/config.json` when `XDG_CONFIG_HOME` is absent. Deployment
may set `PIMCAMP_CONFIG` before it launches the client process. The
configuration file, the state directory, the SQLite state file, and mutation
lock files must be owner-only.

The real operations adapter targets Himalaya 2.1.0 source commit
`bbdfb09b8b8841a509df463a80066531fb81af04` and its locked
`pimalaya-cli 0.2.4` JSON printer. Its configuration has this closed shape:

```json
{
  "credentials": {
    "<sha256-of-high-entropy-client-credential>": {
      "client_identity": "local-client",
      "grants": ["list", "read", "compose", "reply", "send", "junk", "subscribe_new_mail"]
    }
  },
  "adapter_wait_seconds": 10,
  "state_path": "/private/pimcamp/state.sqlite3",
  "operations_adapter": {
    "kind": "himalaya",
    "executable": "/usr/bin/himalaya",
    "account": "personal",
    "config_paths": ["/private/himalaya/config.toml"],
    "inbox": "INBOX",
    "junk_mailbox": "Junk",
    "from": {"name": "Sender", "address": "sender@example.test"}
  },
  "observation_adapter": {
    "kind": "command",
    "command": ["/private/bin/pimcamp-observation-adapter"]
  }
}
```

The Himalaya adapter uses explicit SMTP envelope recipients. It does not place
`Bcc` in the RFC 5322 headers. It reports `move_to_junk` only when deployment
positively configures `junk_mailbox`; current inspected Himalaya source provides
no proved spam-reporting call, so the adapter does not claim one.

A finite invocation looks like this:

```sh
printf '%s' '{"contract_version":"pimcamp.v1","client_credential":"<secret>","input":{"limit":25}}' | ./pimcamp list
```

Do not place a real credential in shell history in normal use. Supply the
request through the calling program's private input path.

## Safety properties

- Authorization finishes before adapter I/O or mutation reservation.
- Message references and cursors are opaque Pimcamp values.
- List, read, reply-source lookup, and notification-triggered refresh use the
  current operations adapter. Notifications are hints, not mail truth.
- Send and junk reserve a durable receipt before one adapter attempt. A reused
  mutation ID replays its terminal result or returns `conflict`; ambiguity is
  recorded as `outcome_unknown` and never retried implicitly.
- One deployment-owned absolute deadline bounds all finite lower phases. A
  client `SIGTERM` starts a fresh observation teardown deadline.
- Public errors and ordinary diagnostics omit mail content, credentials, and
  raw lower material.

## Verification

Run the repository gate:

```sh
python3 -m compileall -q src tests pimcamp
python3 -m unittest discover -s tests -v
python3 -m unittest tests.test_process_boundary -v
```

The process suite runs the same inherited public contract checks against two
independent private-port pairs. These deterministic ports are not lower-adapter
fixtures and claim no Himalaya or Mirador syntax.

Run the credentialed journey separately:

```sh
python3 -m unittest tests.test_live_adapters -v
```

An explicit local skip is honest but is not release evidence. Release evidence
requires the real journey to pass, real redacted captures, exact tool versions,
the selected junk mechanism, adapter-call counts, and the content-free
event/query ordering trace. See `tests/fixtures/README.md`.
