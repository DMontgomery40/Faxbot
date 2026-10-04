# Work

The **Work** screen gives each received document an owner. The owner acknowledges it, does what it needs, and marks it done. Faxbot keeps the history of who owned it and when, and can export that history with the document as evidence.

Work is separate from delivery. A fax reaching Faxbot, an email reaching an inbox and a person acknowledging the document are three different events, and Faxbot records each one separately. An email server accepting a message does not mean anyone read it.

## How documents reach the queue

Every received document gets one work item once Faxbot holds the document itself:

- A fax becomes a work item when its document has arrived. A fax that is still **waiting for the document** has no item yet; the Work screen lists it under **Waiting for documents** so it is visible, but it has no owner until the document arrives. The Inbox shows why it is waiting.
- A document imported from another system (see [Import documents from another system](#import-documents-from-another-system)) becomes a work item the same way.
- Documents received by [direct delivery](direct-delivery.md) are delivered through [intake](intake.md) and do not appear in Work.

The item sits in the mailbox the document was routed to by its fax number. A document that matched no routing rule is in no mailbox; only people with access on everything see it.

A test fax created from the console or with `faxbot inbound simulate` becomes an item too, marked **Test fax**, so you can try the queue without a real fax.

When two documents have identical bytes, each keeps its own item, because they are separate arrivals. Each shows "Same document as the one received …" with the other's time.

## Owners and backups

An owner must already be able to see the document. Assigning work never gives anyone access: the **Assign** list shows only enabled people who hold `work:read` and `inbound:read` on that document and have set their own password. When an owner later loses access, the item stays visible to managers, who can reassign it.

Each mailbox can have a backup person. If an item is not acknowledged by its target time, Faxbot records that it was missed and gives the item to the backup person, as long as the backup can still see the document. If there is no backup, or the backup can no longer see it, the owner stays the same and the history says why. This happens once per item.

## Acknowledgement target

The acknowledgement target is your team's operational target, not a legal deadline. Set it in hours:

- **Installation target**: in the Work screen's settings, or with `faxbot work settings --acknowledge-hours 24`. The setting is `WORK_ACKNOWLEDGE_HOURS`; `0` (the default) sets no target.
- **Mailbox target**: overrides the installation target for one mailbox. `0` means no target for that mailbox.

The clock starts when the document became available: when Faxbot acquired the document, or when the fax arrived if it was stored before acquisition records existed. Faxbot works out the due time once, when the item is created. Changing a target applies to documents that arrive afterwards. Restarts, repeated provider notifications and duplicate documents never restart the clock.

## States

| State | Example sentence |
| --- | --- |
| Waiting for an owner | Waiting for an owner. |
| Assigned | Assigned to Dana; acknowledge by 3 Oct 14:05. |
| Overdue | Overdue; assigned to Dana. |
| Escalated | Overdue; escalated to Sam. |
| Acknowledged | Acknowledged by Dana. |
| Done | Done: filed in the case system. |

Times are shown in your local time. Each item also explains its target, for example "Acknowledge within 24 hours of the document arriving (installation setting)", or "No acknowledgement target set".

Only the owner can acknowledge. The owner, or anyone who manages work on that document, can mark it done with a short note. Managers can assign, reassign and reopen. If two people change the same item at once, the second sees "This item changed; reload and try again." and nothing is overwritten.

## Evidence export

**Export** downloads a zip for one item:

- `manifest.json`: the item, the document's size, page count and SHA-256 digest, how Faxbot acquired it (source, account, the source's operation ID and revision, the time the source reported, the time Faxbot imported and acquired it, and the source's own report), email deliveries with the addresses they went to and when, and the ownership and deadline history.
- `original.pdf`: the document as Faxbot acquired it. It is included only when you may also open documents (`inbound:document`) on that item.
- `history.txt`: one sentence per event, with times in UTC.

The manifest names what is missing instead of leaving it out silently, for example "No provider receipt was retained for this fax.", "The original document is no longer stored." or "The original document was withheld because you do not have permission to read documents."

The same item, unchanged, always produces the same manifest apart from the export's own ID and time. Each export is recorded in the item's history.

What an export does not prove: a digest shows whether the file changed after Faxbot stored it. It does not prove who sent the document, that it is complete, or when it was sent. Provider and email records show what those systems reported to Faxbot, not that a person read the document.

## Import documents from another system

Another system can hand Faxbot a PDF to be handled like a received fax. Send it to `POST /imports` with a key that has `work:import`:

```bash
curl -X POST https://fax.example.com/imports \
  -H "X-API-Key: $FAXBOT_API_KEY" \
  -F file=@referral.pdf \
  -F 'manifest={"source_system":"case-system","operation_id":"case-41","to_number":"+15550100001","source_received_at":"2026-10-03T14:05:00Z"}'
```

Or from the command line: `faxbot import referral.pdf --source case-system --id case-41 --to +15550100001`.

| Manifest field | Required | Meaning |
| --- | --- | --- |
| `source_system` | Yes | The system the document comes from, up to 64 characters. |
| `operation_id` | Yes | The document's ID in that system, up to 100 characters. |
| `revision` | No | A new version of a document already imported under the same ID. |
| `source_received_at` | No | When that system received it, as an RFC 3339 time with its offset. |
| `to_number` | No | The fax number it was addressed to; this picks the mailbox. Numbers without a country code use the installation country. |
| `from_number` | No | The fax number it came from. |
| `pages` | No | The page count that system reported. |

Faxbot checks that the file is a valid PDF before recording anything. The reply is `{"import_id", "inbound_id", "status"}`:

- `received`: a new document.
- `duplicate`: the same operation ID and revision with the same bytes was imported before; nothing new was created.
- HTTP 409: the same operation ID and revision arrived with different bytes. The first document is kept and the conflict is recorded.
- HTTP 400: a missing file, a file that is not a valid PDF, or a manifest Faxbot cannot read, with one sentence saying which.

An import is identified by the importing key's account, the operation ID and the revision. Give each source system its own key, or keep operation IDs unique across the systems that share one.

## Permissions

| Permission | Allows |
| --- | --- |
| `work:read` | See work items, their history and counts. Given on everything, a mailbox or one document. |
| `work:manage` | Assign, reassign, mark done and reopen. |
| `work:export` | Export evidence. The original document also needs `inbound:document`. |
| `work:import` | Import documents from another system. |

Settings need `settings:read` to see and `settings:write` to change. The built-in roles include them: Owner and Administrator have all four; Fax Operator has `work:read` and `work:manage`; Fax Viewer has `work:read`; Auditor has `work:read` and `work:export`. See [Access control](../security/access-control.md).

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/work` | Items you can see; `view=mine`, `unassigned`, `overdue` or `all`, plus `state`, `mailbox` and `limit` |
| GET | `/work/counts` | Counts of the items you can see |
| GET | `/work/{id}` | One item |
| GET | `/work/{id}/history` | Its history, oldest first |
| GET | `/work/{id}/assignees` | People who can see the document and so can own it |
| POST | `/work/{id}/assign` | `{principal_id, version}` |
| POST | `/work/{id}/acknowledge` | `{version}` |
| POST | `/work/{id}/done` | `{note, version}` |
| POST | `/work/{id}/reopen` | `{version}` |
| GET | `/work/{id}/export` | The evidence zip |
| GET, PUT | `/work/settings` | Mailbox targets and backup people |
| POST | `/imports` | Import a document |

An item you cannot see answers 404, the same as one that does not exist. Every change needs the `version` you last saw.

## Not live-validated

Work, export and import are tested with synthetic documents and local databases. They have not been run against a customer's case system or a production fax account.
