# Direct delivery

When two organizations both run Faxbot, documents between them can skip the fax call entirely. The sender still types a fax number. If that number belongs to a verified partner, Faxbot sends the original PDF straight to the partner's Faxbot, encrypted to the partner. The partner's [intake queue](intake.md) receives the exact original bytes.

If direct delivery cannot be used, Faxbot sends an ordinary fax under the same fax ID.

## Turn it on

Set these on each installation:

```env
DIRECT_DELIVERY_ENABLED=true
DIRECT_ORGANIZATION=County Clinic
DIRECT_FAX_NUMBER=+15550100002
PUBLIC_API_URL=https://fax.countyclinic.example
```

`DIRECT_FAX_NUMBER` is the number partners fax you on. `PUBLIC_API_URL` must be reachable over HTTPS by your partners.

Faxbot creates this installation's keys the first time you show your card. They are stored in a private file beside the configuration key, `.direct-identity.key` in the fax data folder, or at `FAXBOT_DIRECT_KEY_PATH`. Back the file up with the configuration key. Faxbot never exports the private keys.

## Enroll each other

Both organizations do these steps, each for the other.

1. In **Tools → Delivery routes → Direct partners**, select **Show our card** and send the card to the other organization. The card holds your organization name, fax number, address and public keys. It contains no secrets.
2. When their card arrives, select **Add partner** and paste it. Faxbot checks the card's signature.
3. Select **Send code by fax** next to the partner. Faxbot faxes a one-page code to the partner's fax number.
4. The partner reads the code from that fax. In their console they select **Confirm a code** next to your organization and enter it. Their Faxbot sends the code back to you, signed with their key.
5. The partner now shows as **Verified**. Faxes you send to their number go directly.

### Partner addresses

Faxbot only talks to partners on the public Internet. When you add a partner, and again before every request to it, Faxbot looks up the address on the partner's card. If it points to this computer, a private network (such as 10.x, 192.168.x or an IPv6 unique-local address), a link-local or cloud metadata address, or a multicast or unspecified address, Faxbot refuses with one sentence and sends nothing. Requests go to the address Faxbot checked, so a later change to the partner's DNS cannot redirect them.

For partners on a network you control, such as two installations on the same VPN, an owner can turn the check off with `DIRECT_ALLOW_PRIVATE_PEERS=true` (`faxbot settings set direct_allow_private_peers=true`). It is off by default.

The code proves that whoever holds the partner's key also receives faxes at the partner's number. Codes expire after 7 days, and five wrong codes close it; send a new code to try again.

A verified partner stays verified for a year. **Remove** stops direct delivery to and from that partner at once.

## What happens to each document

- Faxbot encrypts each PDF with a new key, and only the partner's installation can open it. A signed manifest records the sender, the recipient, a message ID and the SHA-256 fingerprint of the original.
- The partner checks the signature, that the document is addressed to them, and the fingerprint after decrypting. It stores the PDF unchanged, adds it to intake and returns a signed receipt.
- The fax shows as delivered when the receipt arrives. The route shows as **Direct delivery** in the fax's route history.
- Sending the same message twice does not create a second copy. A reused message ID with a different document is refused.
- No document is stored anywhere except the two installations.

## When it does not go directly

- When the partner cannot be reached, or signs a refusal, nothing was delivered. Faxbot sends the fax normally in the same attempt.
- When the answer is lost after the document was sent, Faxbot does not send it again. It asks the partner whether the document arrived. If the partner confirms receipt, the fax is marked delivered. If the partner signs that it never arrived, Faxbot sends an ordinary fax.
- When the partner has turned direct delivery off, its Faxbot answers as if the feature did not exist. The fax waits for confirmation, and you can check it in **Jobs**.

## API

Partner management needs `settings:read`, or `settings:write` for changes. Sending a code by fax also needs `fax:send`.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/direct/card` | This installation's card |
| GET, POST | `/direct/peers` | List or add partners |
| POST | `/direct/peers/{id}/challenge` | Fax the partner a code |
| POST | `/direct/peers/{id}/confirm` | Confirm a code the partner faxed you |
| POST | `/direct/peers/{id}/revoke` | Remove a partner |
| GET | `/direct/deliveries` | Recent documents sent and received |

Partners' installations call `POST /direct/deliveries`, `GET /direct/deliveries/{message_id}` and `POST /direct/verifications`. These routes carry no API key; each request is checked against the enrolled partner's signing key.
