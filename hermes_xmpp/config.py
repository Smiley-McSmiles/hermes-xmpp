"""Configuration loading and secret handling.

Settings live in a TOML file; passwords and API keys live in separate files
with 0600 permissions so the TOML can be committed and the secrets never are.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("~/.hermes-xmpp/hermes.toml")


class ConfigError(Exception):
    """Invalid or missing configuration."""


@dataclass(frozen=True)
class Account:
    jid: str
    password_file: str = "~/.hermes-xmpp/account.secret"
    host: str | None = None
    port: int = 5222
    resource: str = "hermes"
    starttls: bool = True


@dataclass(frozen=True)
class Endpoint:
    url: str
    key_file: str
    model: str = "hermes-agent"
    timeout_s: float = 300.0
    provider: str = ""

    @property
    def chat_url(self) -> str:
        """The OpenAI-compatible completions URL (inline-image turns)."""
        return self.url

    @property
    def runs_base(self) -> str:
        """Listener root, so run endpoints can be derived from one configured URL."""
        base = self.url.rstrip("/")
        for suffix in ("/v1/chat/completions", "/v1/responses", "/v1/runs"):
            if base.endswith(suffix):
                return base[: -len(suffix)]
        return base


@dataclass(frozen=True)
class Policy:
    owners: tuple[str, ...] = ()
    # Bare JIDs whose messages are never answered (other bridge bots: a reply
    # would make two bots talk to each other forever).
    ignore_jids: tuple[str, ...] = ()
    max_chunk_chars: int = 3800
    rate_limit_per_minute: int = 8
    owner_system_prompt: str = ""
    guest_system_prompt: str = ""
    quiet_unknown_senders: bool = False
    # What a message does to the answer already in flight: "interrupt" stops it
    # and answers the new text next, "queue" lets it finish and then answers.
    on_new_message: str = "interrupt"


@dataclass(frozen=True)
class Features:
    omemo: bool = True
    # "auto" encrypts to whatever versions the contact's devices speak; "2" and
    # "legacy" refuse to send when the contact has no device for that version.
    omemo_mode: str = "auto"
    auto_accept_subscriptions: bool = False  # False: ask the owners before adding a contact
    uploads: bool = True
    max_upload_mb: int = 40
    # Inline downloaded images into the model request so vision models see them.
    inline_images: bool = True
    max_inline_image_mb: int = 8
    # Forward the model's mid-turn commentary ("hold on, let me look into that")
    # as its own chat message when it goes off to use a tool.
    interim_messages: bool = True
    max_interim_messages: int = 3
    spool_dir: str = "~/.hermes-xmpp/spool"
    omemo_storage: str = "~/.hermes-xmpp/omemo.json"
    upload_service: str = ""


@dataclass(frozen=True)
class Rooms:
    join: tuple[str, ...] = ()
    nick: str = "hermes"
    respond: str = "mention"          # "mention" or "always"
    owner_nicks: tuple[str, ...] = ()
    trust_nicks: bool = False          # nick-only owner matching; spoofable in anonymous rooms
    encrypt: bool = False              # OMEMO 2 for room messages (needs member real JIDs)
    # When encryption to a room cannot be completed, send the answer in the clear
    # (marked "[unencrypted]") instead of staying silent. Off by default.
    plaintext_fallback: bool = False
    # Join a room when an owner invites the bot to it; strangers' invites are
    # forwarded to the owners as a question instead of being accepted.
    accept_invites: bool = True


@dataclass(frozen=True)
class Config:
    account: Account
    owner: Endpoint
    guest: Endpoint
    policy: Policy
    features: Features = field(default_factory=Features)
    rooms: Rooms = field(default_factory=Rooms)
    path: Path = field(default=DEFAULT_CONFIG_PATH)


def _expand(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value)))


def read_secret(path: str | Path) -> str:
    """Read a secret file, trimmed. Refuses group/world-readable files."""
    p = _expand(str(path))
    try:
        st = p.stat()
    except OSError as exc:
        raise ConfigError(f"missing secret file {p}: {exc}") from exc
    if st.st_mode & 0o077:
        raise ConfigError(f"secret file {p} is accessible by other users (chmod 600)")
    try:
        value = p.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(f"cannot read secret file {p}: {exc}") from exc
    if not value:
        raise ConfigError(f"secret file {p} is empty")
    return value


def _endpoint(raw: dict, default_key: str) -> Endpoint:
    if not isinstance(raw, dict):
        raise ConfigError("endpoint must be a table")
    url = str(raw.get("url", "")).strip()
    if not url.startswith(("http://127.0.0.1", "http://localhost", "https://")):
        raise ConfigError(f"endpoint {url!r} must be loopback http:// or an https:// URL")
    return Endpoint(
        url=url,
        key_file=str(raw.get("key_file", default_key)),
        model=str(raw.get("model", "hermes-agent")),
        timeout_s=float(raw.get("timeout_s", 300.0)),
        provider=str(raw.get("provider", "")),
    )


def load(path: str | Path = DEFAULT_CONFIG_PATH) -> Config:
    p = _expand(str(path))
    base = p.parent
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {p}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{p} is not valid TOML: {exc}") from exc

    def resolve(value: str) -> str:
        """Relative paths are relative to the config file, not the CWD."""
        candidate = Path(os.path.expanduser(str(value)))
        return str(candidate if candidate.is_absolute() else (base / candidate))

    account_raw = raw.get("account") or {}
    jid = str(account_raw.get("jid", "")).strip()
    if "@" not in jid:
        raise ConfigError("account.jid must be a bare JID like hermes@example.org")

    account = Account(
        jid=jid,
        password_file=resolve(str(account_raw.get("password_file", Account.password_file))),
        host=account_raw.get("host") or None,
        port=int(account_raw.get("port", 5222)),
        resource=str(account_raw.get("resource", "hermes")),
        starttls=bool(account_raw.get("starttls", True)),
    )

    policy_raw = raw.get("policy") or {}
    owners = tuple(str(o).strip() for o in (policy_raw.get("owners") or ()) if str(o).strip())
    ignore_jids = tuple(str(o).strip() for o in (policy_raw.get("ignore_jids") or ()) if str(o).strip())
    policy = Policy(
        owners=owners,
        ignore_jids=ignore_jids,
        max_chunk_chars=int(policy_raw.get("max_chunk_chars", 3800)),
        rate_limit_per_minute=int(policy_raw.get("rate_limit_per_minute", 8)),
        owner_system_prompt=str(policy_raw.get("owner_system_prompt", "")),
        guest_system_prompt=str(policy_raw.get("guest_system_prompt", "")),
        quiet_unknown_senders=bool(policy_raw.get("quiet_unknown_senders", False)),
        on_new_message=str(policy_raw.get("on_new_message", "interrupt")).lower(),
    )

    features_raw = raw.get("features") or {}
    features = Features(
        omemo=bool(features_raw.get("omemo", True)),
        omemo_mode=str(features_raw.get("omemo_mode", "auto")).strip().lower(),
        auto_accept_subscriptions=bool(features_raw.get("auto_accept_subscriptions", False)),
        uploads=bool(features_raw.get("uploads", True)),
        max_upload_mb=int(features_raw.get("max_upload_mb", 40)),
        inline_images=bool(features_raw.get("inline_images", True)),
        max_inline_image_mb=int(features_raw.get("max_inline_image_mb", 8)),
        interim_messages=bool(features_raw.get("interim_messages", True)),
        max_interim_messages=max(0, int(features_raw.get("max_interim_messages", 3))),
        spool_dir=resolve(str(features_raw.get("spool_dir", Features.spool_dir))),
        omemo_storage=resolve(str(features_raw.get("omemo_storage", Features.omemo_storage))),
        upload_service=str(features_raw.get("upload_service", "")),
    )

    rooms_raw = raw.get("rooms") or {}
    rooms = Rooms(
        join=tuple(str(r).strip() for r in (rooms_raw.get("join") or ()) if str(r).strip()),
        nick=str(rooms_raw.get("nick", "hermes")),
        respond=str(rooms_raw.get("respond", "mention")).lower(),
        owner_nicks=tuple(str(n).strip() for n in (rooms_raw.get("owner_nicks") or ()) if str(n).strip()),
        trust_nicks=bool(rooms_raw.get("trust_nicks", False)),
        accept_invites=bool(rooms_raw.get("accept_invites", True)),
        encrypt=bool(rooms_raw.get("encrypt", False)),
        plaintext_fallback=bool(rooms_raw.get("plaintext_fallback", False)),
    )
    if rooms.respond not in ("mention", "always"):
        raise ConfigError("rooms.respond must be 'mention' or 'always'")

    owner = _endpoint(raw.get("owner") or {}, "~/.hermes-xmpp/owner.key")
    guest = _endpoint(raw.get("guest") or {}, "~/.hermes-xmpp/guest.key")
    owner = Endpoint(owner.url, resolve(owner.key_file), owner.model, owner.timeout_s, owner.provider)
    guest = Endpoint(guest.url, resolve(guest.key_file), guest.model, guest.timeout_s, guest.provider)

    if policy.max_chunk_chars < 500:
        raise ConfigError("policy.max_chunk_chars must be at least 500")
    if policy.on_new_message not in ("interrupt", "queue"):
        raise ConfigError("policy.on_new_message must be 'interrupt' or 'queue'")
    if features.omemo_mode not in ("auto", "2", "legacy"):
        raise ConfigError("features.omemo_mode must be 'auto', '2' or 'legacy'")
    if features.max_upload_mb < 1:
        raise ConfigError("features.max_upload_mb must be positive")
    if features.max_inline_image_mb < 1:
        raise ConfigError("features.max_inline_image_mb must be positive")

    return Config(account=account, owner=owner, guest=guest, policy=policy,
                  features=features, rooms=rooms, path=p)
