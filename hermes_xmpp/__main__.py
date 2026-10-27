"""Entry point: ``hermes-xmpp {run,check,init,unit}``."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import secrets
import signal
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__
from .bot import Bridge
from .config import ConfigError, load, read_secret

log = logging.getLogger("hermes_xmpp")

CONFIG_TEMPLATE = """\
# ─────────────────────────────────────────────────────────────────────────────
#  hermes-xmpp — the XMPP bridge for Hermes Agent
#
#  Bot account:        {jid}
#  Password:           secrets/account.secret  (0600, not in git)
#  Authorized users:   [policy] owners — currently {owners}
#                      → full tools: terminal, files, projects, web
#  Everyone else:      guest tier, zero tools, plain text answers only
# ─────────────────────────────────────────────────────────────────────────────

[account]
jid = "{jid}"
{host_line}port = 5222
resource = "hermes"
starttls = true
password_file = "secrets/account.secret"

# ── Authorized operators: full Hermes toolset (shell, files, projects) ───────
# A dedicated Hermes profile keeps these conversations out of the operator's
# CLI profile: separate session store, separate memory, separate logs.
# model + provider are sent with every request, overriding the profile default.
[owner]
url = "{owner_url}"
key_file = "secrets/owner.key"
model = "{model}"
provider = "{provider}"
timeout_s = 1800

# ── Everyone else: a Hermes profile whose platform_toolsets.api_server is []
# so no tool exists in the turn at all. Served by the same listener under
# /p/<profile>/.
[guest]
url = "{guest_url}"
key_file = "secrets/guest.key"
model = "{model}"
provider = "{provider}"
timeout_s = 180

[policy]
# ── WHO GETS TOOL ACCESS ────────────────────────────────────────────────────
# Matched case-insensitively, resource ignored. Add one entry per operator.
owners = [{owners_toml}]
# Bare JIDs that are never answered. List other bridge bots here, or two bots
# that know each other will reply to each other forever.
ignore_jids = []

max_chunk_chars = 3800
rate_limit_per_minute = 8
quiet_unknown_senders = false

[features]
# OMEMO 0.3 (legacy) and OMEMO 2 / 0.8 are handled by the same plugin.
omemo = true
# "auto" uses whichever versions the contact's devices speak; "2" or "legacy"
# refuses to send when the contact has no device for that version.
omemo_mode = "auto"
# false: a stranger's contact request is sent to [policy] owners for approve/deny.
# true: accept every request immediately (reciprocal subscription included).
auto_accept_subscriptions = false
uploads = {uploads}
upload_service = "{upload_service}"
max_upload_mb = 40
# Inline images from incoming upload links into the model request (vision models).
inline_images = true
max_inline_image_mb = 8
spool_dir = "spool"
omemo_storage = "omemo.json"

[rooms]
# Rooms to join on startup. An empty list disables multi-user chat.
join = [{rooms_toml}]
nick = "{nick}"
# "mention": only answer when addressed by nick; "always": answer every message.
respond = "mention"
owner_nicks = []
trust_nicks = false
# Encrypt room messages with OMEMO 2 (SCE), like the 1:1 chats. Needs a
# non-anonymous room so member JIDs are visible; the bridge refuses to fall back
# to plaintext when an encrypted room cannot be encrypted.
encrypt = false
"""


def _models_url(endpoint_url: str) -> str:
    base = endpoint_url.rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    return base + "/models"


def _probe(endpoint, key: str) -> tuple[bool, str]:
    request = urllib.request.Request(
        _models_url(endpoint.url),
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read() or b"{}")
        names = [m.get("id") for m in payload.get("data", []) if isinstance(m, dict)]
        return True, f"HTTP {response.status}, models={names[:4] or 'n/a'}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code} from {endpoint.url}"
    except Exception as exc:  # noqa: BLE001
        return False, f"cannot reach {endpoint.url}: {exc}"


def cmd_check(config_path: str) -> int:
    cfg = load(config_path)
    problems = 0
    print(f"config    {cfg.path}")
    print(f"account   {cfg.account.jid} via {cfg.account.host or 'DNS SRV'}:{cfg.account.port}")
    print(f"owners    {', '.join(cfg.policy.owners) or '(none - every sender is a guest)'}")
    if cfg.policy.ignore_jids:
        print(f"ignored   {', '.join(cfg.policy.ignore_jids)}")

    try:
        read_secret(cfg.account.password_file)
        print("password  ok (0600)")
    except ConfigError as exc:
        print(f"password  FAIL {exc}")
        problems += 1

    for label, endpoint in (("owner", cfg.owner), ("guest", cfg.guest)):
        try:
            key = read_secret(endpoint.key_file)
        except ConfigError as exc:
            print(f"{label:8}  FAIL key: {exc}")
            problems += 1
            continue
        ok, detail = _probe(endpoint, key)
        model = f"{endpoint.provider or 'profile-default'}:{endpoint.model}"
        print(f"{label:8}  {'ok  ' if ok else 'FAIL'} {detail}")
        print(f"          model {model}")
        problems += 0 if ok else 1

    host = cfg.account.host or cfg.account.jid.rsplit("@", 1)[-1]
    try:
        with socket.create_connection((host, cfg.account.port), timeout=8):
            print(f"xmpp      ok ({host}:{cfg.account.port} reachable)")
    except OSError as exc:
        print(f"xmpp      FAIL {host}:{cfg.account.port}: {exc}")
        problems += 1

    print(f"omemo     {cfg.features.omemo}   uploads {cfg.features.uploads}"
          f"   rooms {', '.join(cfg.rooms.join) or '(none)'}")
    return 0 if problems == 0 else 1


def _write_secret(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value.strip() + "\n")
    os.chmod(path, 0o600)


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(os.path.expanduser(args.dir)).resolve()
    config_path = target / "hermes.toml"
    if config_path.exists() and not args.force:
        print(f"{config_path} exists; pass --force to overwrite", file=sys.stderr)
        return 2

    jid = args.jid.strip()
    if "@" not in jid:
        print("--jid must look like bot@example.org", file=sys.stderr)
        return 2
    domain = jid.rsplit("@", 1)[-1].lower()
    owners = [j.strip().lower() for j in (args.owner or []) if j.strip()] or []
    host = args.host or None

    password = os.environ.get("HERMES_XMPP_PASSWORD", "")
    if not password:
        if sys.stdin.isatty():
            password = getpass.getpass(f"XMPP password for {jid}: ")
        if not password:
            print("set HERMES_XMPP_PASSWORD (or run interactively) to store the account password",
                  file=sys.stderr)
            return 2

    owner_key = secrets.token_urlsafe(32)
    guest_key = secrets.token_urlsafe(32)

    target.mkdir(parents=True, exist_ok=True)
    (target / "secrets").mkdir(mode=0o700, exist_ok=True)
    _write_secret(target / "secrets" / "account.secret", password)
    _write_secret(target / "secrets" / "owner.key", owner_key)
    _write_secret(target / "secrets" / "guest.key", guest_key)

    listener = args.listener.rstrip("/")
    config_path.write_text(CONFIG_TEMPLATE.format(
        jid=jid,
        host_line=f'host = "{host}"\n' if host else "",
        owners=", ".join(owners) or "(none)",
        owners_toml=", ".join(f'"{o}"' for o in owners),
        owner_url=f"{listener}/p/{args.owner_profile}/v1/chat/completions",
        guest_url=f"{listener}/p/{args.guest_profile}/v1/chat/completions",
        model=args.model,
        provider=args.provider,
        uploads="true" if args.upload_service else "false",
        upload_service=args.upload_service or "",
        rooms_toml=", ".join(f'"{r}"' for r in (args.room or [])),
        nick=args.nick or jid.split("@", 1)[0],
    ), encoding="utf-8")
    os.chmod(config_path, 0o644)

    print(f"wrote {config_path}")
    print(f"wrote {target / 'secrets'} (0600)")
    print()
    print("Next, wire the Hermes side.")
    print()
    print(f"Owner profile (~/.hermes/profiles/{args.owner_profile}/.env) — a private")
    print("profile keeps XMPP transcripts and memory out of your CLI profile:")
    print("  API_SERVER_ENABLED=true")
    print(f"  API_SERVER_KEY={owner_key}")
    print()
    print(f"  hermes profile create {args.owner_profile} --clone")
    print()
    print(f"Guest profile (~/.hermes/profiles/{args.guest_profile}/.env):")
    print("  API_SERVER_ENABLED=true")
    print(f"  API_SERVER_KEY={guest_key}")
    print()
    print(f"  hermes profile create {args.guest_profile} --clone")
    print(f"  hermes -p {args.guest_profile} config set platform_toolsets.api_server \"[]\"")
    print(f"  hermes -p {args.guest_profile} config set model.default {args.model}")
    print(f"  hermes -p {args.guest_profile} config set model.provider {args.provider}")
    print()
    print("The gateway serves both profiles on the same listener under /p/<name>/.")
    print("Then restart the gateway (outside Hermes) and run:")
    print(f"  hermes-xmpp check --config {config_path}")
    print(f"  hermes-xmpp unit --config {config_path} > ~/.config/systemd/user/hermes-xmpp.service")
    print("  systemctl --user daemon-reload && systemctl --user enable --now hermes-xmpp")
    return 0


def cmd_unit(args: argparse.Namespace) -> int:
    cfg = load(args.config)
    config_path = Path(cfg.path).resolve()
    executable = args.executable or sys.executable
    instance = (args.name or cfg.account.jid.split("@", 1)[0]).strip()
    unit_name = f"hermes-xmpp-{instance}"
    print(f"# save as ~/.config/systemd/user/{unit_name}.service")
    print(f"# then: systemctl --user daemon-reload && systemctl --user enable --now {unit_name}")
    data_dirs = sorted({str(Path(cfg.features.omemo_storage).expanduser().parent),
                        str(Path(cfg.features.spool_dir).expanduser())})
    unit = f"""[Unit]
Description=Hermes XMPP bridge — {instance} ({cfg.account.jid})
Documentation=https://github.com/Smiley-McSmiles/hermes-xmpp
Wants=network-online.target hermes-gateway.service
After=network-online.target hermes-gateway.service

[Service]
Type=simple
WorkingDirectory={config_path.parent}
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=HERMES_XMPP_CONFIG={config_path}
Environment=HERMES_XMPP_INSTANCE={instance}
ExecStart={executable} -m hermes_xmpp run
Restart=always
RestartSec=5
TimeoutStopSec=20

# Writes only the OMEMO store, session ids and the attachment spool.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={' '.join(data_dirs)}
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
RestrictNamespaces=true
LockPersonality=true

[Install]
WantedBy=default.target
"""
    print(unit, end="")
    return 0


async def cmd_run(config_path: str) -> int:
    cfg = load(config_path)
    bridge = Bridge(cfg, read_secret(cfg.owner.key_file), read_secret(cfg.guest.key_file))

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, bridge.disconnect)
        except NotImplementedError:  # pragma: no cover - non-POSIX
            pass

    log.info("hermes-xmpp %s starting as %s", __version__, cfg.account.jid)
    await bridge.run_forever()
    log.info("stopped")
    # A turn may still be blocked in a worker thread (an image turn waits on the
    # completions route, which can take minutes). Normal interpreter shutdown would
    # wait for that thread, outlive TimeoutStopSec and be killed with SIGABRT —
    # nothing is persisted in memory here, so leave without joining it.
    logging.shutdown()
    os._exit(0)


def cmd_react(args) -> int:
    """Hand a reaction to the running bridge: it owns the OMEMO sessions."""
    from . import reactions

    cfg = load(args.config)
    path = reactions.queue_reaction(cfg.features.spool_dir, args.jid, args.message_id, args.emoji)
    print(f"queued reaction {''.join(args.emoji)} to {args.jid} on message {args.message_id}")
    print(f"job written to {path}; the running bridge sends it within a few seconds")
    return 0


def cmd_handoff(args) -> int:
    """Seed the contact's session with a CLI transcript and say so in chat.

    ``/handoff xmpp`` in Hermes pipes the brief here: the run writes it into the one
    session this contact already owns with this bot, so the next XMPP message continues
    the CLI conversation instead of starting cold.
    """
    from . import hermes_api, policy, reactions

    cfg = load(args.config)
    to = policy.bare_jid(args.to)
    brief = args.brief if args.brief is not None else sys.stdin.read()
    if not brief.strip():
        print("empty handoff brief (pass --brief or pipe it on stdin)", file=sys.stderr)
        return 2
    sid = policy.session_id_for(cfg.account.jid, to)
    if args.dry_run:
        print(f"dry run: would seed session {sid} and queue {len(brief)} chars to {to}")
        return 0
    key = read_secret(cfg.owner.key_file)
    turn = hermes_api.Turn(
        system=("Handoff from the operator's Hermes CLI session. The message is context "
                "you now own: do not act on it yet, acknowledge it in one short line."),
        user=brief, session_key=sid)
    ack = ""
    try:
        run_id = hermes_api.start(cfg.owner, key, turn, sid)
        answer = hermes_api.wait(cfg.owner, key, run_id, session_key=sid,
                                 timeout=min(args.timeout, cfg.owner.timeout_s))
        ack = answer.text.strip()
    except hermes_api.BridgeError as exc:
        print(f"could not seed {sid} ({exc.user_message}); sending the brief itself")
    path = reactions.queue_text(cfg.features.spool_dir, to, ack or brief)
    print(f"handoff written to session {sid}")
    print(f"queued {'the acknowledgement' if ack else 'the brief'} for {to} ({path.name})")
    return 0


def main(argv: list[str] | None = None) -> int:
    default_config = os.environ.get(
        "HERMES_XMPP_CONFIG",
        "hermes.toml" if os.path.exists("hermes.toml") else "~/.hermes-xmpp/hermes.toml",
    )
    parser = argparse.ArgumentParser(prog="hermes-xmpp", description="XMPP bridge for Hermes Agent")
    parser.add_argument("--config", default=default_config)
    parser.add_argument("--log-level", default=os.environ.get("HERMES_XMPP_LOG_LEVEL", "INFO"))
    parser.add_argument("--version", action="version", version=f"hermes-xmpp {__version__}")
    # --config is also accepted after the subcommand: `hermes-xmpp check --config X`.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS)
    common.add_argument("--log-level", default=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="run the bridge in the foreground", parents=[common])
    sub.add_parser("check", help="validate config, secrets, API servers and XMPP reachability",
                   parents=[common])

    init = sub.add_parser("init", help="create a config and secret files in a new directory",
                          parents=[common])
    init.add_argument("--dir", default=".", help="target directory (default: current)")
    init.add_argument("--jid", required=True, help="bot JID, e.g. hermes@example.org")
    init.add_argument("--owner", action="append", help="authorized operator JID (repeatable)")
    init.add_argument("--host", help="XMPP server host (default: derived from the JID)")
    init.add_argument("--model", default="qwen3:8b", help="model id (default: qwen3:8b for Ollama)")
    init.add_argument("--provider", default="custom", help="provider name (default: custom → Ollama)")
    init.add_argument("--listener", default="http://127.0.0.1:8642", help="Hermes API listener")
    init.add_argument("--owner-profile", default="xmpp-owner",
                      help="private Hermes profile for the operator tier (default: xmpp-owner)")
    init.add_argument("--guest-profile", default="xmpp-guest", help="profile name for untrusted senders")
    init.add_argument("--upload-service", default="", help="XEP-0363 upload host, e.g. upload.example.org")
    init.add_argument("--room", action="append", help="MUC room to join (repeatable)")
    init.add_argument("--nick", default="", help="MUC nick (default: the JID's local part)")
    init.add_argument("--force", action="store_true", help="overwrite an existing config")

    react = sub.add_parser("react", parents=[common],
                           help="queue a message reaction; the running bridge sends it")
    react.add_argument("jid", help="contact or room the reaction is for")
    react.add_argument("message_id", help="stanza id of the message being reacted to")
    react.add_argument("emoji", nargs="+", help="one or more reactions, e.g. 👍 ❤️")

    handoff = sub.add_parser("handoff", parents=[common],
                             help="seed a contact's session from a CLI transcript")
    handoff.add_argument("--to", required=True, help="contact JID to hand the session to")
    handoff.add_argument("--brief", help="handoff text (default: read stdin)")
    handoff.add_argument("--timeout", type=float, default=180.0,
                         help="seconds to wait for the seeding run (default 180)")
    handoff.add_argument("--dry-run", action="store_true",
                         help="seed nothing and queue nothing; report only")

    unit = sub.add_parser("unit", help="print a systemd user unit for this config", parents=[common])
    unit.add_argument("--executable", help="python interpreter (default: the running one)")
    unit.add_argument("--name", help="instance name for the unit (default: the JID's local part)")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("slixmpp").setLevel(
        logging.DEBUG if os.environ.get("HERMES_XMPP_XMPP_DEBUG") else logging.WARNING)

    try:
        if args.command == "check":
            return cmd_check(args.config)
        if args.command == "init":
            return cmd_init(args)
        if args.command == "unit":
            return cmd_unit(args)
        if args.command == "react":
            return cmd_react(args)
        if args.command == "handoff":
            return cmd_handoff(args)
        return asyncio.run(cmd_run(args.config))
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
