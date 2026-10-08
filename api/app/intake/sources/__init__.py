"""Intake connectors: email mailboxes and watched folders that file documents or send faxes, each once.

- ``mail``: one email's identity, sender confirmation (DKIM/SPF/DMARC), fax number and attachments.
- ``imap`` and ``oauth``: the mailbox session over TLS, with a password or an XOAUTH2 token.
- ``folder``: waiting until a file stops changing, and done/ and failed/.
- ``receive``: filing through the generic import contract (``work/imports.py``).
- ``send`` and ``keys``: one fax per message or file, under the connector's own key.
- ``replies``: the one email reply a sender gets.
- ``store``, ``poller`` and ``http``: records, the background check and the routes.
"""
