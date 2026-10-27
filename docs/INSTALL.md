# Installing hermes-xmpp on a server (Ollama / local models)

This is the whole path from a bare server to "message the bot from your phone". Two processes are
involved and only one of them is this package:

```
XMPP contact ──▶ hermes-xmpp ──▶ Hermes API (127.0.0.1:8642) ──▶ Ollama (127.0.0.1:11434)
                 the bridge        the agent + tools             the model
```

The bridge does no model work itself. It forwards a turn to Hermes, and Hermes calls whichever provider
its profile is configured with. For local AI that provider is `custom` pointed at Ollama's
OpenAI-compatible endpoint.

## 1. Ollama

```sh
ollama pull qwen3:8b            # or any tool-calling model you prefer
ollama serve                    # usually already running as a service
curl -s http://127.0.0.1:11434/v1/models | head -c 200
```

Pick a model that actually calls tools. A chat-only model will answer, but the owner tier's terminal and
file tools will never fire. `qwen3`, `llama3.3`, `mistral-nemo` and `devstral` are reasonable; small 3–4B
models tend to ignore tool schemas when the prompt gets long.

## 2. Hermes Agent

```sh
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
hermes setup --portal        # or choose your provider in `hermes model`
```

The owner tier runs in its own Hermes profile. That keeps XMPP transcripts and
memory out of the default CLI profile (so `session_search` and the CLI session
list never show them):

```sh
hermes profile create xmpp-owner --clone
hermes -p xmpp-owner config set model.provider custom
hermes -p xmpp-owner config set model.base_url http://127.0.0.1:11434/v1
hermes -p xmpp-owner config set model.default qwen3:8b
```

Enable its API server — append to `~/.hermes/profiles/xmpp-owner/.env`:

```
API_SERVER_ENABLED=true
API_SERVER_KEY=<paste the owner key printed by hermes-xmpp init>
```

`api_server` already defaults to the full `hermes-api-server` toolset, so the
operator keeps terminal, files, projects and web.

**Local vision models need a capability declaration.** Hermes consults its model
catalog before sending an image: an unknown local alias is treated as text-only and
the image is replaced by a `vision_analyze` text fallback (which needs a working
vision model of its own). Declare it per model in the profile that serves the bot:

```sh
hermes -p xmpp-owner config set providers.custom.models.gemma4-ollama.supports_vision true
hermes -p xmpp-guest config set providers.custom.models.gemma4-ollama.supports_vision true
```

The setting is read per turn, so no gateway restart is needed. Keep it per model,
not `model.supports_vision` at the top level: the top-level flag applies to every
model the profile serves, including text-only ones.

## 3. Guest profile (untrusted senders)

The guest tier is a separate profile whose API surface exposes **zero toolsets**, so an unknown contact
cannot reach the shell no matter what they type.

```sh
hermes profile create xmpp-guest --clone
hermes -p xmpp-guest config set platform_toolsets.api_server "[]"
hermes -p xmpp-guest config set model.provider custom
hermes -p xmpp-guest config set model.base_url http://127.0.0.1:11434/v1
hermes -p xmpp-guest config set model.default qwen3:8b
```

Append to `~/.hermes/profiles/xmpp-guest/.env`:

```
API_SERVER_ENABLED=true
API_SERVER_KEY=<paste the guest key printed by hermes-xmpp init>
```

Restart the gateway **from a shell outside Hermes** (a restart kills the process that runs the agent):

```sh
hermes gateway restart
curl -s http://127.0.0.1:8642/v1/models -H "Authorization: Bearer $OWNER_KEY"
```

## 4. The bridge

The package is not on PyPI or GitHub yet: install from this checkout, or from the wheel in `dist/`.
Everything a deployment owns lives under `~/.hermes-xmpp` (default): the venv at
`~/.hermes-xmpp/venv`, each bot's config, `secrets/`, OMEMO store, session ids and spool in
`~/.hermes-xmpp/<name>/`.

Shortcut: `./install.sh` does everything in this section (venv, package, CLI symlink, config, secrets)
and with `--service` also wires the systemd unit. `--dir` and `--venv` move the deployment root and
the venv.

```sh
./install.sh --jid hermes@example.org --owner you@example.org \
  --model qwen3:8b --provider custom \
  --upload-service upload.example.org --room room@conference.example.org \
  --service
```

It prompts for the XMPP password (or read `HERMES_XMPP_PASSWORD`), then prints the `API_SERVER_KEY`
lines for the two profiles. Re-running upgrades the package and keeps the existing config.

By hand, it is the same three steps:

```sh
python3 -m venv ~/.hermes-xmpp/venv
~/.hermes-xmpp/venv/bin/pip install ~/Projects/hermes-xmpp        # or: pip install dist/hermes_xmpp-*.whl
~/.local/bin/hermes-xmpp --version
```

Create the deployment (config + secrets) in its own directory:

```sh
HERMES_XMPP_PASSWORD='xmpp-password' \
  hermes-xmpp init \
    --dir ~/.hermes-xmpp \
    --jid hermes@example.org \
    --owner you@example.org \
    --model qwen3:8b \
    --provider custom \
    --upload-service upload.example.org \
    --room room@conference.example.org
```

That writes `~/.hermes-xmpp/hermes.toml` and `~/.hermes-xmpp/secrets/` (0600) and prints the `API_SERVER_KEY`
lines you paste into the two `.env` files above. `--provider custom` is what Ollama resolves to inside
Hermes; if you later switch to OpenRouter, change `provider`/`model` in the TOML and restart only the bridge.

Verify everything before starting the service:

```sh
hermes-xmpp check --config ~/.hermes-xmpp/hermes.toml
```

It reports the account, owner list, both API endpoints (with their resolved `provider:model`), XMPP
reachability and which optional features are on.

## 5. Run it as a service

```sh
hermes-xmpp unit --config ~/.hermes-xmpp/hermes.toml > ~/.config/systemd/user/hermes-xmpp.service
systemctl --user daemon-reload
systemctl --user enable --now hermes-xmpp
journalctl --user -u hermes-xmpp -f
```

The generated unit pins `HERMES_XMPP_CONFIG`, runs out of the config directory, and grants write access
only to the OMEMO store and the attachment spool.

## 6. First contact

1. Add the bot JID as a contact. Unless `[features] auto_accept_subscriptions = true`, the
   request is not accepted: the owners get a chat message naming the requester and answer
   `approve <jid>` or `deny <jid>`. Owner JIDs themselves are accepted immediately.
2. Trust the bot's OMEMO device in your client (fingerprint is logged at startup:
   `OMEMO ready: device id …, fingerprint …`).
3. Send a message. The journal should show `inbound <message> … encrypted=True` → `decrypted` →
   `reply for … ready` → `sent encrypted reply`.

## Running several bots (one per model)

Each instance is one account + one directory + one systemd unit. They share the Hermes listener and
choose their model per request, so a DeepSeek bot and an Ollama bot run side by side without a second
Hermes install.

```sh
HERMES_XMPP_PASSWORD='...' hermes-xmpp init --dir ~/.hermes-xmpp/bot    --jid bot@example.org    \
  --owner you@example.org --model deepseek/deepseek-v4.1-flash-1017 --provider openrouter
HERMES_XMPP_PASSWORD='...' hermes-xmpp init --dir ~/.hermes-xmpp/ollama --jid ollama@example.org \
  --owner you@example.org --model qwen3:8b --provider custom

# One listener has one API_SERVER_KEY per profile, so both instances must present the
# same keys. Copy them (do not let the second init keep its freshly generated pair):
cp ~/.hermes-xmpp/bot/secrets/owner.key ~/.hermes-xmpp/ollama/secrets/owner.key
cp ~/.hermes-xmpp/bot/secrets/guest.key ~/.hermes-xmpp/ollama/secrets/guest.key
chmod 600 ~/.hermes-xmpp/ollama/secrets/*.key

for i in bot ollama; do
  hermes-xmpp unit --config ~/.hermes-xmpp/$i/hermes.toml > ~/.config/systemd/user/hermes-xmpp-$i.service
done
systemctl --user daemon-reload
systemctl --user enable --now hermes-xmpp-bot hermes-xmpp-ollama
```

Why this stays clean:

- `--dir` defaults to `~/.hermes-xmpp`; give each instance its own subdirectory there.
- OMEMO identity and the attachment spool live inside each instance directory, so keys never mix.
  Switching accounts never reuses another bot's identity (python-omemo would raise `StopIteration` on a
  shared store). No transcript or session id is stored on the bridge side at all.
- The memory/session scope includes the **bot's own JID**, so the same operator talking to both bots keeps
  two separate memories rather than one shared blob.
- MUC nicks default to the JID's local part, so both bots can sit in the same room without a conflict.
- `[owner].url` / `[guest].url` are per instance: point a bot at `/p/<profile>/v1/chat/completions` if it
  should get a different tool policy. Both instances here share `xmpp-owner` / `xmpp-guest`.
- Put the other bot's JID in `[policy] ignore_jids` on each instance. Two bots that know each other
  otherwise answer each other's replies forever (and burn the rate limit doing it).

Watch them independently with `journalctl --user -u hermes-xmpp-bot -f` and the same for the other unit.

## Troubleshooting

| Symptom | Check |
|---|---|
| No reply, no log line | `grep "inbound <message>"` — nothing means the server never delivered it |
| Replies "backend is not responding" | `hermes-xmpp check`; the API listener is down or the key is wrong |
| Contact shows offline | the bridge sends directed presence at startup and every 4 min; check the roster shows `subscription="both"` |
| Works, then stops every few minutes | missed XEP-0199 pings; keepalive is enabled, check `stream error` lines |
| Model answers but never uses tools | the local model ignores tool schemas — pick a bigger tool-calling model |
| OMEMO: "couldn't encrypt that reply" | the contact's device list is stale; trust the new device in the client |

## Notes for a shared server

- Bind the API server to loopback only (the default `127.0.0.1:8642`). The bridge is the only client.
- `[policy] owners` is the tool-access list. Keep it to JIDs you control; everyone else is a guest with
  zero toolsets.
- The bridge cannot address arbitrary JIDs, by design; that would turn any inbound message into an
  exfiltration path.
- OMEMO works in 1:1 chats only; group rooms are plaintext because python-omemo has no group keying.
