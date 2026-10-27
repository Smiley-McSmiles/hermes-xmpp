# hermes-xmpp

[![tests](https://github.com/Smiley-McSmiles/hermes-xmpp/actions/workflows/tests.yml/badge.svg)](https://github.com/Smiley-McSmiles/hermes-xmpp/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![python: 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![protocol: XMPP | OMEMO](https://img.shields.io/badge/protocol-XMPP%20%7C%20OMEMO-purple.svg)](https://xmpp.org)

Talk to Hermes Agent from any XMPP client — Conversations, Gajim, Ward, Dino, Monal.

A small daemon signs in to one XMPP account, forwards 1:1 messages and group-chat mentions
to Hermes' local API server, and sends the answers back. The operator gets the full
toolset; everyone else gets plain text from a profile where no tool exists. OMEMO 2 and
legacy OMEMO in direct chats and rooms, HTTP uploads including encrypted media, and one
conversation per person that survives restarts and device changes.

- **[docs/INSTALL.md](docs/INSTALL.md)** — full server walk-through, including a local Ollama model.
- **[hermes.toml.example](hermes.toml.example)** — every setting, commented.
- Status: in use daily against Prosody with Hermes' `hermes-api-server` toolset. Interfaces
  may still move; the wire protocol is XMPP, so clients do not care.

One-command install on the machine that runs Hermes:

```sh
git clone https://github.com/Smiley-McSmiles/hermes-xmpp
cd hermes-xmpp
./install.sh --jid bot@example.org --owner you@example.org --service
```

It does not touch Hermes itself; it prints the two `API_SERVER_KEY` lines to paste into the
owner and guest profiles.

## Where things are

    hermes.toml        bot JID, authorized owners, endpoints, OMEMO/upload/room settings
    secrets/           account.secret, owner.key, guest.key — 0600, git-ignored
    hermes_xmpp/       the bridge
    systemd/           user unit

A deployment lives in `~/.hermes-xmpp/<bot>/` (config, secrets, OMEMO store,
session ids, spool); the checkout is only needed to build and upgrade it.

`hermes.toml` is the one file to edit: `[policy] owners` is the tool-access
list, `[account] jid` is the bot identity.
A small daemon logs into one XMPP account, forwards 1:1 messages to the local
Hermes API server, and sends the answer back as chat messages.

```
XMPP contact ──▶ hermes-xmpp ──▶ Hermes API server ──▶ agent turn
                     ▲                                      │
                     └────────────── reply text ◀───────────┘
```

## Features

- **A new message interrupts the answer in progress.** The bridge drives each turn
  through the Hermes Runs API, so it holds the run id and calls
  `POST /v1/runs/{id}/stop` when another message arrives; the new text is then
  answered in the same session. Send `stop` to stop the model
  without asking a new question — the bridge answers `Stopped.` and nothing
  reaches the model. Messages that arrive while a stop settles are folded into
  the next turn instead of being dropped.
- **One conversation per user, not per device.** The Hermes session id is derived
  from the bot JID and the sender's *bare* JID, so a phone, a laptop and a
  desktop all share one transcript and one memory scope, and a restart cannot
  fork a user into a second session.
- **Conversations stay private.** The bot holds no transcript: it derives the
  session id instead of storing it, message text never goes to the log (only
  lengths and JIDs), attachment spools are deleted after each turn, and the
  system prompt is sent as an ephemeral instruction. Owner and guest tiers each
  run in their own Hermes profile (`xmpp-owner`, `xmpp-guest`), so XMPP history
  and memory live outside the operator's CLI profile and its `session_search`.
- **OMEMO 0.3 (legacy) and OMEMO 2 / 0.8** in 1:1 chats, one plugin handling both.
  `[features] omemo_mode` is `auto` by default; set it to `2` or `legacy` to refuse
  sending to a contact whose devices lack that version (no silent downgrade).
  Identity + sessions persist in `~/.hermes-xmpp/omemo.json`. New
  devices are trusted automatically (BTBV) and logged at WARNING.
- **HTTP uploads (XEP-0363)** via `upload.example.org`: inbound links on the upload host are
  downloaded for every sender, and images (jpg/png/gif/webp, up to `max_inline_image_mb`,
  default 8) are inlined into the model request so a vision model actually sees them. The
  agent sends files by writing `[[attach: /path]]` in its reply. Encrypted media
  (`aesgcm://`, XEP-0454) is decrypted, including clients that pad the key material.
  A local vision model must be declared in the serving profile
  (`providers.custom.models.<model>.supports_vision: true`), or Hermes treats it as
  text-only and replaces the image with a text fallback — see
  [docs/INSTALL.md](docs/INSTALL.md).
- **Encrypted-media aware attachments.** Downloads accept plain `https` links on the
  upload host and `aesgcm://` / `#aesgcm=` links (OMEMO media sharing, XEP-0454):
  the AES-256-GCM blob is opened with the key from the fragment, so a photo sent
  from a phone reaches the model instead of arriving as ciphertext. Downloads retry
  with backoff (a message can outrun the sender's upload), and the file type comes
  from magic bytes rather than the URL suffix.
- **Mid-turn commentary.** While a run is going, the bridge subscribes to
  `GET /v1/runs/{id}/events` and forwards `message.interim` — so "hold on, let me
  look into that" arrives as its own message before the answer, instead of being
  lost. `[features] interim_messages` / `max_interim_messages` bound it.
- **`[policy] on_new_message`** decides what a message does to the answer in flight:
  `interrupt` (default) stops it and answers the new text next, `queue` lets it
  finish. A turn that carries an attachment is never interrupted either way.
- **Message reactions (XEP-0444)** both ways. The agent reacts by putting
  `[[react: 👍]]` in its reply; the bridge sends the reaction against the stanza id
  of the message it is answering and strips the marker. The operator can queue one
  for the running bridge with `hermes-xmpp react <jid> <message-id> 👍` (a job in
  `<spool>/outbox`, sent within a couple of seconds). OMEMO 2 encrypts the reaction
  inside the ciphertext; legacy OMEMO 0.3 encrypts the body only, so with legacy
  devices the reaction goes out in the clear (logged, and dropped outright when
  `omemo_mode` is pinned).
- **Group chats (MUC)** at `conference.example.org`: joins `[rooms] join` and
  answers when addressed by nick (`hermes: ...`) unless `respond = "always"`.
  Room owner tier requires a real JID match in a non-anonymous room, or
  `trust_nicks = true`.
- **Encrypted rooms.** `[rooms] encrypt = true` encrypts room messages with
  OMEMO 2 (XEP-0420 SCE) for every member, the same envelope Ward and
  Conversations use, and reads both OMEMO 2 and legacy OMEMO on the way in.
  slixmpp-omemo does not implement the OMEMO 2 half, so the bridge builds and
  parses the envelope itself (`hermes_xmpp/sce.py`). It needs a non-anonymous
  room (member JIDs must be visible) and refuses to fall back to plaintext: if a
  member has no OMEMO device it says so in the room instead of leaking the text.
- **Contact requests are approved by the operator.** With
  `[features] auto_accept_subscriptions = false` (the default) a stranger's presence
  subscription is stored in `pending-<bot>.json` and every `[policy] owners` JID gets a chat
  message naming the requester. The owner answers `approve <jid>`, `deny <jid>` or `pending`;
  those commands never reach the model. Set it to `true` to accept everyone immediately.
- **`invite <jid>` asks a new person to become a contact, `join <room>` takes the bot
  into a room.** Owner-only forced commands handled by the bridge: `invite` adds the JID
  to the roster and sends the presence subscription, `join` joins the room as
  `rooms.nick` (add it to `[rooms] join` to keep it after a restart). Neither costs a
  model turn, so both work even with a model that cannot call tools. `stop` works the
  same way in the other direction.

## Trust tiers

Enforced by routing, not by prompt wording:

| Tier  | Who                        | Endpoint                              | Capabilities |
|-------|----------------------------|---------------------------------------|--------------|
| owner | JIDs in `policy.owners`    | `127.0.0.1:8642/p/xmpp-owner/` (`xmpp-owner` profile: private session store + memory) | full toolset: terminal, files, projects, web |
| guest | everyone else              | same listener, `/p/xmpp-guest/` (`xmpp-guest` profile, `platform_toolsets.api_server = []`) | none — text answers only |

Both tiers own a private profile, so what is said over XMPP never lands in the
operator's CLI session list or memory. The guest profile's API surface exposes
zero toolsets, and the system message restates that no tools exist: a guest
cannot reach this machine's shell, files or projects regardless of what they
type.

`[policy] ignore_jids` lists bare JIDs that are never answered at all — put the
other bridge bots there, or two bots that know each other will answer each
other forever.

The daemon itself never executes anything and only ever addresses a reply to
the bare JID that wrote in. There is deliberately no "send to arbitrary JID"
tool: that would turn any inbound message into a prompt-injection channel.

## Layout

```
hermes_xmpp/config.py   TOML + 0600 secret files
hermes_xmpp/policy.py   JID normalisation, owner/guest tiering, rate limit
hermes_xmpp/chunk.py    fence-aware message splitting
hermes_xmpp/hermes_api.py  Runs API client (start/poll/stop) + completion fallback
hermes_xmpp/bot.py      slixmpp client, per-conversation runner, typing, interrupts
hermes_xmpp/__main__.py CLI: run | check | init | unit
tests/                  unit tests (56)
systemd/                user unit
```

## Install

```sh
cd ~/Projects/hermes-xmpp
./install.sh --jid bot@example.org --owner you@example.org --service
```

The default deployment root is `~/.hermes-xmpp`: the venv goes in
`~/.hermes-xmpp/venv`, the config and `secrets/` in `~/.hermes-xmpp` itself, and
OMEMO identity and the attachment spool beside them. Override with
`--dir`, `--venv` or `HERMES_XMPP_DIR` / `HERMES_XMPP_VENV`; two bots use two
`--dir` values under the same root (see [docs/INSTALL.md](docs/INSTALL.md)).

Manual equivalent:

```sh
python3 -m venv ~/.hermes-xmpp/venv
~/.hermes-xmpp/venv/bin/pip install .
~/.hermes-xmpp/venv/bin/pip install pytest     # tests only
~/.hermes-xmpp/venv/bin/python -m pytest -q
```

Create the secrets (each file must be 0600):

```sh
mkdir -p ~/.hermes-xmpp
printf '%s\n' 'XMPP-PASSWORD' > ~/.hermes-xmpp/account.secret
printf '%s\n' "$OWNER_API_KEY" > ~/.hermes-xmpp/owner.key
printf '%s\n' "$GUEST_API_KEY" > ~/.hermes-xmpp/guest.key
chmod 600 ~/.hermes-xmpp/*.secret ~/.hermes-xmpp/*.key
```

The URL, JID, owner list and chunk limits are in `~/.hermes-xmpp/hermes.toml`.

## Hermes side

Both tiers are separate Hermes profiles, so XMPP transcripts and memory are not
part of the operator's CLI profile:

```sh
hermes profile create xmpp-owner --clone
hermes profile create xmpp-guest --clone
hermes -p xmpp-guest config set platform_toolsets.api_server "[]"
```

Owner profile (`~/.hermes/profiles/xmpp-owner/.env`) — full toolset,
`api_server` defaults to the complete toolset for a profile:

```
API_SERVER_ENABLED=true
API_SERVER_KEY=<owner key>
```

Guest profile (`~/.hermes/profiles/xmpp-guest/.env`):

```
API_SERVER_ENABLED=true
API_SERVER_KEY=<guest key>
```

A multiplexed gateway serves secondary profiles on the same listener under
`/p/<profile>/`, so the configured endpoints are
`http://127.0.0.1:8642/p/xmpp-owner/v1/chat/completions` and
`http://127.0.0.1:8642/p/xmpp-guest/v1/chat/completions` — no second port.

Then `hermes gateway restart` (from a shell outside the gateway). A new profile
created while the gateway runs is picked up live, but a restart is the reliable
path.

## Run

```sh
hermes-xmpp check          # config, secrets, both API servers, XMPP reachability
hermes-xmpp run            # foreground
```

As a user service:

```sh
mkdir -p ~/.config/systemd/user
cp systemd/hermes-xmpp.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hermes-xmpp.service
journalctl --user -u hermes-xmpp -f
```

## Chat commands

Handled by the bridge itself, so they work even when the model cannot call tools and never
cost a turn. `stop` is a bare word; the rest take an argument.

| Command | Who | What it does |
| --- | --- | --- |
| `stop` | anyone | Interrupt the answer in progress (`Stopped.`), no new question |
| `invite <jid>` | owner, DM | Add the contact to the roster and send a presence subscription |
| `join <room@conference.host>` | owner, DM | Join the room now as `rooms.nick`; add it to `[rooms] join` to keep it |
| `approve <jid>` / `deny <jid>` | owner | Accept or decline a waiting contact request |
| `pending` | owner | List the waiting contact requests |

Anything else goes to the model. In a room, `invite` and `join` are ordinary text.

## Behaviour notes

- One runner per conversation: a message that arrives while the agent is working
  interrupts that run (`/v1/runs/{id}/stop`) and is answered next, merged with any
  other text that arrived in the meantime. A bare `stop` stops without a
  new question.
- Delivery receipts (XEP-0184): incoming messages that ask for a receipt are acked as
  soon as the bridge has them (so the operator's client shows delivered), and the bot's
  own replies carry a receipt request. Rooms are excluded.
- Typing indicator (`composing`) while the agent works, re-sent every 15s so clients do
  not drop it during long turns; `active` goes out after the last chunk is delivered.
- Replies longer than `max_chunk_chars` are split on line boundaries, with code
  fences closed and re-opened across chunks.
- Per-sender rate limit (`rate_limit_per_minute`), default 8/min.
- Group chats (MUC) answer only when addressed, unless `rooms.respond = "always"`.
- Conversation continuity uses one deterministic session id per (bot, bare JID)
  plus an `X-Hermes-Session-Key` derived from the same pair: one context per
  person, without the JID ever being sent to the API.
- That id is salted with `SESSION_ID_VERSION` (`policy.py`). Hermes tombstones a
  deleted session id and refuses it forever (`409 not_found`, then
  `503 storage_unavailable` once the tombstone row is gone), so the way to give
  contacts a clean conversation is to bump the version and reinstall — never
  delete their session. The bridge also rotates to a suffixed id in-process if
  the server rejects the derived one.

## Encryption

The bridge is an OMEMO client (XEP-0384), not a relay that reads ciphertext:

- 1:1 chats use OMEMO 2 when the contact has a device for it and legacy OMEMO otherwise,
  with `[features] omemo_mode` (`auto`, `2`, `legacy`) deciding whether a downgrade is
  allowed. Nothing is sent in plaintext.
- Rooms use OMEMO 2 over the XEP-0420 stanza-content envelope (`[rooms] encrypt = true`),
  read both as OMEMO 2 and as legacy OMEMO. slixmpp-omemo has no OMEMO 2 group support, so
  the bridge builds that envelope itself (`hermes_xmpp/sce.py`).
- Device identities live in `omemo.json` (`[features] omemo_storage`); new devices are
  trusted on first use and logged at WARNING, so an unexpected device is visible. Back the
  file up: losing it means contacts see a new identity.
- Message text is never written to the log, never stored by the bridge, and never sent as
  part of a session id — that is the privacy model, and it is why the bridge can read
  plaintext after decrypting: the agent has to see the message to answer it.

## Security model

- `[policy] owners` is the tool-access list: an owner turn runs in a Hermes profile with
  the full toolset, so treat that JID list as shell access to the host.
- Everyone else runs in a second profile whose `platform_toolsets.api_server = []`, so no
  tool exists in the turn. Keep it that way unless a guest should be able to act.
- Secrets are files, 0600, never in git: `secrets/account.secret` (XMPP password),
  `secrets/owner.key` and `secrets/guest.key` (Hermes API keys, one per profile).
- The bridge only talks to `127.0.0.1:8642` (Hermes' API server) and to your XMPP server.
- Report a problem privately if it is a vulnerability; ordinary bugs go to the issue
  tracker.

## License

MIT — see [LICENSE](LICENSE). Use it, fork it, ship it; the Hermes team is welcome to it.
