import os

import pytest

from hermes_xmpp.config import ConfigError, load, read_secret

TOML = """
[account]
jid = "AI@example.org"
password_file = "{tmp}/account.secret"
host = "example.org"
port = 5222

[owner]
url = "http://127.0.0.1:8642/v1/chat/completions"
key_file = "{tmp}/owner.key"

[guest]
url = "http://127.0.0.1:8643/v1/chat/completions"
key_file = "{tmp}/guest.key"

[policy]
owners = ["owner@example.org"]
max_chunk_chars = 3000
rate_limit_per_minute = 5
"""


def _write(path, text, mode=0o600):
    path.write_text(text)
    os.chmod(path, mode)


@pytest.fixture()
def cfgfile(tmp_path):
    _write(tmp_path / "account.secret", "hunter2\n")
    _write(tmp_path / "owner.key", "owner-key")
    _write(tmp_path / "guest.key", "guest-key")
    path = tmp_path / "hermes.toml"
    _write(path, TOML.format(tmp=tmp_path), 0o644)
    return path


def test_load_reads_account_endpoints_and_policy(cfgfile):
    cfg = load(cfgfile)
    assert cfg.account.jid == "AI@example.org"
    assert cfg.account.host == "example.org"
    assert cfg.owner.model == "hermes-agent"
    assert cfg.policy.owners == ("owner@example.org",)
    assert cfg.policy.max_chunk_chars == 3000
    assert read_secret(cfg.account.password_file) == "hunter2"


def test_world_readable_secret_is_refused(cfgfile, tmp_path):
    os.chmod(tmp_path / "owner.key", 0o644)
    cfg = load(cfgfile)
    with pytest.raises(ConfigError, match="chmod 600"):
        read_secret(cfg.owner.key_file)


def test_missing_secret_is_reported(cfgfile):
    cfg = load(cfgfile)
    with pytest.raises(ConfigError, match="missing secret"):
        read_secret(str(cfg.path.parent / "nope"))


def test_plain_http_remote_endpoint_is_refused(tmp_path, cfgfile):
    text = cfgfile.read_text().replace("http://127.0.0.1:8642", "http://10.0.0.5:8642")
    cfgfile.write_text(text)
    with pytest.raises(ConfigError, match="loopback"):
        load(cfgfile)


def test_jid_without_domain_is_refused(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text('[account]\njid = "hermes"\n')
    with pytest.raises(ConfigError, match="bare JID"):
        load(path)


def test_ignore_jids_are_loaded(cfgfile):
    text = cfgfile.read_text().replace(
        "[policy]\nowners = [\"owner@example.org\"]",
        "[policy]\nowners = [\"owner@example.org\"]\nignore_jids = [\"ollama@example.org\"]")
    cfgfile.write_text(text)
    assert load(cfgfile).policy.ignore_jids == ("ollama@example.org",)
    assert cfgfile.exists()


def test_omemo_mode_defaults_to_auto(cfgfile):
    assert load(cfgfile).features.omemo_mode == "auto"


def test_omemo_mode_accepts_2_and_legacy(cfgfile):
    base = cfgfile.read_text()
    for mode in ("2", "legacy"):
        cfgfile.write_text(base.replace(
            "[policy]", f'[features]\nomemo_mode = "{mode}"\n\n[policy]'))
        assert load(cfgfile).features.omemo_mode == mode


def test_omemo_mode_rejects_unknown_values(cfgfile):
    text = cfgfile.read_text().replace(
        "[policy]", '[features]\nomemo_mode = "both"\n\n[policy]')
    cfgfile.write_text(text)
    with pytest.raises(ConfigError, match="omemo_mode"):
        load(cfgfile)
