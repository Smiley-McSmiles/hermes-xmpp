import pytest

from hermes_xmpp.policy import (GUEST, OWNER, RateLimiter, bare_jid, is_ignored,
                               session_id_for, session_key, session_key_for, tier_for)


@pytest.mark.parametrize("raw,expected", [
    ("AI@example.org", "ai@example.org"),
    ("owner@example.org/phone", "owner@example.org"),
    ("  bob@example.org  ", "bob@example.org"),
    ("example.org", "example.org"),
    ("weird@Example.ORG/Resource/With/Slashes", "weird@example.org"),
])
def test_bare_jid(raw, expected):
    assert bare_jid(raw) == expected


def test_bare_jid_rejects_empty():
    with pytest.raises(ValueError):
        bare_jid("   ")


def test_owners_are_case_and_resource_insensitive():
    owners = ("owner@example.org",)
    assert tier_for("owner@example.org/phone", owners) == OWNER
    assert tier_for("owner@example.org", owners) == OWNER
    assert tier_for("owner@example.org.evil.com", owners) == GUEST
    assert tier_for("someone@example.org", owners) == GUEST


def test_no_owners_means_everyone_is_a_guest():
    assert tier_for("owner@example.org", ()) == GUEST


def test_session_key_is_stable_and_not_the_jid():
    key = session_key("owner@example.org/phone")
    assert key == session_key("owner@example.org")
    assert key.startswith("xmpp:")
    assert "owner" not in key
    assert session_key("other@example.org") != key


def test_session_key_differs_per_bot_for_the_same_contact():
    a = session_key_for("bot@example.org", "owner@example.org")
    b = session_key_for("ollama@example.org", "owner@example.org")
    assert a != b
    assert a.startswith("xmpp:") and "owner" not in a
    # stable across resources and case
    assert a == session_key_for("BOT@example.org/hermes", "owner@example.org/phone")


def test_session_id_is_one_per_person_not_per_device():
    phone = session_id_for("bot@example.org", "owner@example.org/phone")
    laptop = session_id_for("BOT@example.org", "owner@example.org/laptop")
    assert phone == laptop
    assert phone.startswith("xmpp-")
    assert "owner" not in phone
    # a different bot is a different conversation
    assert session_id_for("ollama@example.org", "owner@example.org") != phone


def test_ignored_jids_match_case_and_resource():
    ignore = ("Ollama@example.org",)
    assert is_ignored("ollama@example.org/phone", ignore)
    assert not is_ignored("owner@example.org", ignore)
    assert not is_ignored("ollama@example.org.evil", ignore)
    assert not is_ignored("ollama@example.org", ())


def test_rate_limiter_counts_per_sender_and_window():
    limiter = RateLimiter(limit=2, window_s=60)
    assert limiter.allow("a@x", now=0)
    assert limiter.allow("a@x", now=1)
    assert not limiter.allow("a@x", now=2)
    assert limiter.allow("b@x", now=2)
    # window slides
    assert limiter.allow("a@x", now=61)
