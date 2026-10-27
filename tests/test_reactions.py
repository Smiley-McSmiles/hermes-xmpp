import json

import pytest
import slixmpp

from hermes_xmpp import reactions


# -- marker parsing ----------------------------------------------------------

def test_extract_markers_pulls_the_emoji_out_of_the_text():
    cleaned, emojis = reactions.extract_markers("Shipping it now. [[react: 👍]]\nTesting next.")
    assert emojis == ["👍"]
    assert "[[" not in cleaned
    assert cleaned == "Shipping it now.\nTesting next."


def test_extract_markers_without_marker_is_identity():
    cleaned, emojis = reactions.extract_markers("plain reply, no markers")
    assert cleaned == "plain reply, no markers"
    assert emojis == []


def test_several_markers_and_several_emoji_merge_without_duplicates():
    cleaned, emojis = reactions.extract_markers(
        "done [[react: 👍 ❤️]]\n([[react: 👍 🎉 ]])")
    assert emojis == ["👍", "❤️", "🎉"]
    assert "[[" not in cleaned


def test_reaction_count_is_capped():
    _, emojis = reactions.extract_markers("[[react: 1 2 3 4 5 6]]")
    assert len(emojis) == reactions.MAX_REACTIONS


def test_attach_marker_is_left_for_the_upload_path():
    _, emojis = reactions.extract_markers("file: [[attach: /tmp/x.log]]")
    assert emojis == []


def test_emoji_containing_a_bracket_does_not_break_parsing():
    cleaned, emojis = reactions.extract_markers("ok [[react: 🏳️‍🌈]]")
    assert emojis == ["🏳️‍🌈"]
    assert cleaned == "ok"


# -- operator queue ----------------------------------------------------------

def test_queue_reaction_writes_a_job_the_bridge_can_read(tmp_path):
    path = reactions.queue_reaction(tmp_path, "owner@example.org", "abc123", ["👍"])
    assert path.parent == tmp_path / reactions.OUTBOX_DIR
    assert json.loads(path.read_text()) == {
        "to": "owner@example.org", "id": "abc123", "reactions": ["👍"]}
    assert path.stat().st_mode & 0o077 == 0  # owner-only


def test_queued_text_job_is_shaped_for_the_bridge(tmp_path):
    path = reactions.queue_text(tmp_path, "owner@example.org", "hello")
    assert json.loads(path.read_text()) == {"to": "owner@example.org", "text": "hello"}
    with pytest.raises(ValueError):
        reactions.queue_text(tmp_path, "a@b", "   ")


def test_queued_jobs_sort_in_the_order_they_were_written(tmp_path):
    first = reactions.queue_reaction(tmp_path, "a@b", "id1", ["👍"])
    second = reactions.queue_text(tmp_path, "a@b", "after")
    assert sorted(p.name for p in first.parent.iterdir()) == [first.name, second.name]


def test_queue_reaction_refuses_a_job_without_an_id_or_emoji(tmp_path):
    with pytest.raises(ValueError):
        reactions.queue_reaction(tmp_path, "a@b", "", ["👍"])
    with pytest.raises(ValueError):
        reactions.queue_reaction(tmp_path, "a@b", "id1", [])


# -- stanza shape ------------------------------------------------------------

def test_stanza_carries_the_reactions_namespace_and_target_id():
    client = slixmpp.ClientXMPP("bot@example.org", "secret")
    client.register_plugin("xep_0444")
    msg = client.make_message(mto="owner@example.org", mtype="chat")
    client.plugin["xep_0444"].set_reactions(msg, "stanza-id-1", ["👍", "❤️"])
    xml = str(msg)
    assert 'xmlns="urn:xmpp:reactions:0"' in xml
    assert 'id="stanza-id-1"' in xml
    assert xml.count("<reaction>") == 2
    assert "👍" in xml and "❤️" in xml


def test_stanza_carries_a_store_hint_so_the_server_archives_it():
    client = slixmpp.ClientXMPP("bot@example.org", "secret")
    client.register_plugin("xep_0444")
    msg = client.make_message(mto="owner@example.org", mtype="chat")
    client.plugin["xep_0444"].set_reactions(msg, "stanza-id-1", ["👍"])
    msg.enable("store")
    assert 'xmlns="urn:xmpp:hints"' in str(msg)
