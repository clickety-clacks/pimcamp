# Pimcamp

Pimcamp is a stable local email capability boundary for user interfaces,
agents, and scripts. It gives these clients one structured seam instead of
making them depend on a specific command-line tool, provider, or email
protocol.

Pimcamp is not an email app. It does not provide a user interface, a sync
engine, a search index, or a general email automation platform.

## Where Pimcamp fits

```mermaid
flowchart TB
    subgraph Clients
        UI[Email UIs]
        Agents[Agents]
        Scripts[Scripts]
    end

    UI --> Pimcamp
    Agents --> Pimcamp
    Scripts --> Pimcamp

    Pimcamp[Pimcamp<br/>stable local capability boundary]

    Pimcamp --> Himalaya[Himalaya<br/>structured email operations]
    Pimcamp --> Mirador[Mirador / Carillon<br/>new-mail observations]

    Himalaya --> Providers[Email providers and local stores]
    Mirador --> IMAP[IMAP IDLE]
    Mirador --> JMAP[JMAP push / event stream]
    Mirador --> Maildir[Maildir event detection<br/>by polling]
    IMAP --> Providers
    JMAP --> Providers
    Maildir --> Providers

    Neverest[Neverest<br/>optional synchronization] -. not required by MVP .-> Pimdir[Pimdir local replica]
```

Pimcamp uses components from the Pimalaya ecosystem behind its boundary:

- [Himalaya](https://github.com/pimalaya/himalaya) provides structured email
  operations.
- [Mirador](https://github.com/pimalaya/mirador), now named Carillon, provides
  change observation through IMAP IDLE, a JMAP push/event stream, or Maildir
  event detection by polling.
- [Neverest](https://github.com/pimalaya/neverest) can synchronize email when
  a deployment needs a Pimdir local replica. Synchronization is optional and
  is not required by the MVP.

A future private adapter composition could place Maildir and notmuch beneath
the same Pimcamp boundary. That composition would reuse the existing public
capabilities. It is not part of the MVP and does not add a capability.

## MVP capabilities

The public MVP surface contains exactly seven capabilities:

1. `list` — list current message summaries.
2. `read` — retrieve the current form of a selected message.
3. `compose` — create a structured composition without sending it.
4. `reply` — create a structured reply from the current source message.
5. `send` — send a composition or reply.
6. `junk` — file a selected message as junk by the strongest supported method.
7. `subscribe_new_mail` — stream normalized new-mail observations so a client
   can retrieve authoritative state through Pimcamp.

Pimcamp is an independent project. It is not an official Pimalaya project and
is not affiliated with or endorsed by Pimalaya.

## Build authority

- Spec artifact: `art_4e44121c`
- Canonical spec commit: `bf8c76f8879184bb5dadcb23d7930d7e3ecbb509`
- Spec SHA-256: `05ba0b738f9cac4413c7bf859f9ba92b2319ffe0368906f68b261e211b44983b`
- Independent reviewed-clean verdict: `att_c06fccb3`
- Review report: `art_89006412`

The reviewed specification is the product authority.
