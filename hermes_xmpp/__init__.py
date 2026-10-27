"""XMPP <-> Hermes bridge.

A small daemon that logs into an XMPP account, forwards 1:1 messages to a
running Hermes API server, and sends the answer back as a chat message.

Two trust tiers, enforced here rather than in a prompt:

* owner  - the sender's JID is listed in ``policy.owners``; the message goes to
           the full-tool API server (terminal, files, projects).
* guest  - everyone else; the message goes to a separate Hermes profile whose
           API server has no toolsets enabled, and a system message restates
           that no tools are available.

The bridge itself never executes anything and never addresses a stanza to a
JID other than the one that wrote in.
"""

__version__ = "0.3.0"
