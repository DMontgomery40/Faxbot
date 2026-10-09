# Numbers

## Your numbers

**Numbers → Your numbers** (`#/numbers/list`) lists every fax number Faxbot carries: the trunk's numbers, HumbleFax's account numbers and caller number, eFax's caller ID, SignalWire's sending number and FreeSWITCH's caller ID. Each row shows the number, **Provided by**, its mailbox (or "No mailbox: received faxes are visible to people with access to everything."), its email delivery and who can see its faxes. **Choose mailbox** gives a number a mailbox.

Numbers without a mailbox are shown only to people who can see settings.

## Mailboxes

**Numbers → Mailboxes** (`#/numbers/mailboxes`) lists mailboxes, who can see each one and, for people who read settings, how long a received fax may wait before it is overdue and who is told.

## Email delivery

**Numbers → Email delivery** (`#/numbers/email`) sets the email server Faxbot uses for received faxes and each email delivery (which number, which inbox, the subject). Changes take effect as soon as you apply them. See [Intake](../operations/intake.md).

## Email and folders

**Numbers → Email and folders** (`#/numbers/connectors`) connects mailboxes and mounted folders to bring documents into Faxbot or send faxes from email or files. Set the mailbox or folder, sign-in details and routing needed for each connector, then select **Test** to check it. The page shows recent items and their outcomes; you can pause, resume, change or remove a connector. See [Email and folders](../operations/connectors.md) for setup details.

## Sender identity

**Numbers → Sender identity** (`#/numbers/identity`) sets the **Header text** printed at the top of each page and the **Station ID** the receiving machine shows, for faxes sent over your carrier line or phone system. Faxes sent through a fax service show the name and number set in that service's account.
