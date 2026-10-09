# Recipients

## Recipients

**Recipients → Recipients** (`#/recipients/list`) lists the fax numbers you send to and what each one cost. **Details** for a number shows the route order for its next fax, each route priced in its own unit ("Telnyx · about $0.005 a minute, at least 1 minute"; "included in your plan"), the last 30 days by route, a preferred route, and whether the recipient accepts a one-page list instead of documents it already has.

### Encoded pages

In a number's **Details**, turn on **Allow encoded pages for this number** only after the recipient agrees to decode them and confirms that this meets their document-handling requirements. Check the agreement box, choose **Page style** and **Error correction**, and select **Save encoded pages**. Dense pages and medium error correction are the defaults. An optional **Shared key** encrypts the document; agree on the key outside the fax call and enter the same key on the receiving Faxbot. With a key, the picture style shows a plain pattern instead of the first page.

Faxbot decides again for each delivery attempt. It selects encoded pages when the route predicts a lower bill, or fewer pages on a plan or unpriced route without a known increase in line time. It can send other eligible pages instead. The original document and fax image remain unchanged. Use `faxbot recipients encoded show NUMBER`, `faxbot recipients encoded set NUMBER --recipient-agreed` and `faxbot recipients encoded off NUMBER` from the command line.

For an explicit enumerative profile 1 file, run `faxbot system codec encode original.pdf --layout enumerative --output payload.tiff`. The recipient can run `faxbot system codec decode received.pdf --output original.pdf` with a Faxbot version that supports the profile. This layout needs an unchanged received raster. Faxbot does not select it automatically, and the static browser decoder does not support it.

An explicit enumerative fax sent through Telnyx to HumbleFax on 9 October 2026 recovered its synthetic original. That check establishes one carrier path, not compatibility with every route or decoder.

## Partners

**Recipients → Partners** (`#/recipients/partners`) lists organizations that receive your documents directly, with no fax call, and the direct-delivery settings: your organization and fax number, your card to give partners, and **Allow partners on private networks (advanced)**, off by default. See [direct delivery](../operations/delivery-routes.md).

## Case packets

**Recipients → Case packets** (`#/recipients/cases`) lists the recent cases this installation sent packets for, one row per case and recipient, with documents sent and received, pages and when the last one went. "No case packets have been sent yet." when there are none. **Open** or **Look up** shows what a recipient already has for a case; **Preview** shows what will be left out before **Send the documents**. See [case packets](../operations/delivery-routes.md#case-packets).
