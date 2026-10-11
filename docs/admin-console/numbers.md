# Numbers

## Your numbers

**Delivery setup → Numbers** (`#/delivery/numbers`) lists every fax number Faxbot carries: the trunk's numbers, HumbleFax's account numbers and caller number, eFax's caller ID, SignalWire's sending number and FreeSWITCH's caller ID. Each row shows the number, **Provided by**, its mailbox (or "No mailbox: received faxes are visible to people with access to everything."), its email delivery and who can see its faxes. **Choose mailbox** gives a number a mailbox.

Numbers without a mailbox are shown only to people who can see settings.

## Mailboxes

**Delivery setup → Mailboxes** (`#/delivery/mailboxes`) lists mailboxes, who can see each one and, for people who read settings, how long a received fax may wait before it is overdue and who is told.

## Email delivery

**Delivery setup → Staff email delivery** (`#/delivery/email`) sets the email server Faxbot uses for received faxes and each email delivery (which number, which inbox, the subject). Changes take effect as soon as you apply them. See [Intake](../operations/intake.md).

## Email and folders

**Delivery setup → Email and folders** (`#/delivery/connectors`) connects mailboxes and mounted folders to bring documents into Faxbot or send faxes from email or files. Set the mailbox or folder, sign-in details and routing needed for each connector, then select **Test** to check it. The page shows recent items and their outcomes; you can pause, resume, change or remove a connector. See [Email and folders](../operations/connectors.md) for setup details.

## Number moves and line inventory

**Delivery setup → Number moves** (`#/delivery/moves`) shows number-move advice and plans, line inventory and carrier closure information. Select **Import line inventory** to upload a CSV or Excel file, and set whether its dates are month-first or day-first. Select **Import carrier list** to add a carrier's discontinued or grandfathered areas; enter the carrier, list type, source date and source URL when the file does not supply them. Review each imported line's carrier match and dates before using them in a move plan or a POTS-replacement comparison. A listed area or date is evidence for your review, not a guarantee that a line can or cannot be moved.

## Sender identity

**Delivery setup → Sending identity** (`#/delivery/identity`) sets the **Header text** printed at the top of each page and the **Station ID** the receiving machine shows, for faxes sent over your carrier line or phone system. Faxes sent through a fax service show the name and number set in that service's account.

The same page has **Header notice**. Enter one line of up to 120 characters and select **Save** to print it on every page. You can add a different notice for a mailbox; that notice replaces the organization notice for faxes sent from that mailbox. Select **Remove** to stop using a notice. From the command line, use `faxbot delivery identity notice set "Your notice"` and add `--mailbox MAILBOX` for a mailbox-specific notice; use `faxbot delivery identity notice clear` to remove it.
