# Racter runtime acceptance — 2026-09-05

The seven-operation runtime at commit
`2c45f8b336a86f71d2c5b795eadf7173a648cc57` completed this additional real-mail
acceptance sequence through the public `pimcamp.v1` executable boundary:

1. Ordinary configured-mailbox list and selected-message read succeeded.
2. A temporary test profile selected the account's spam folder for both
   observation and authoritative queries. The existing provider routes these
   self-addressed test messages there; filtering was not changed.
3. The subscription emitted normalized `started`. This means execution started,
   not that IMAP readiness was established. The runner allowed ten seconds
   before sending.
4. Exactly one new self-addressed composition was sent, with a fixed mutation
   ID reserved for this logical test. Pimcamp returned `sent`.
5. A real normalized `new_mail` hint arrived from Carillon.
6. Subsequent public Pimcamp list/read found the unique exact test subject and
   verified the unique body. No synthetic signal or IMAP APPEND was used.
7. Subscription teardown exited 0. The temporary profile was removed; the
   permanent configuration compared byte-for-byte unchanged. The test message
   was retained, with no filtering or credential changes.

After teardown, ordinary installed list/read succeeded again. No test watcher
or temporary profile remained.

This closes the previously unproved self-send → event → retrieval sequence.
An earlier self-send reached spam while observation targeted INBOX; a separate
APPEND test had proved events but was not evidence of SMTP delivery.

The runtime gate ran 52 tests: 50 passed, two credentialed journeys skipped.
Those skips are not live evidence; the real sequence above is recorded
separately. This additional acceptance does not claim a new reply-send or junk
test, support for every provider, or that all incoming mail reaches INBOX.

## Exact lower tools

- Himalaya 2.1.0 executable SHA-256:
  `7bc31ca0ea596218d97f1b2637e14c6653b1ebf9741711ac0f8a675384d67472`.
- Carillon 0.1.0 executable SHA-256:
  `9e691c49f69779415791b53b6151300bb60b4ab9e7cf5ca26489943559cd503e`.

No email address, credential, body, subject, message identifier, or reusable
client token is included in this public report. Onboarding UI work is not part
of this runtime acceptance or release.
