# Email and folders

Use **Numbers → Email and folders** in the Admin Console to connect a mailbox or folder to Faxbot. A connector can bring documents into a mailbox or send faxes from email or files. This is separate from **Numbers → Email delivery**, which emails received faxes to your staff.

## Add a connector

Select **Add a connector**, enter a name and choose what it does:

- **Bring in documents from a mailbox**: Faxbot checks the selected email folder for messages and files the documents it finds.
- **Bring in documents from a folder**: Faxbot checks a folder on the server and files documents into the selected mailbox.
- **Fax what people email**: Faxbot accepts fax requests sent to a mailbox address.
- **Fax files put in a folder**: Faxbot sends fax requests from files placed in a server folder.

For an email connector, choose the mail service and sign-in method, then enter the mailbox address and the requested account details. For another mail service, enter its incoming mail server and port. Faxbot shows service-specific guidance in the form. For a sending connector, enter the approved sender addresses and the Faxbot person for each one. Faxbot only sends requests from an approved address when it can confirm the sender and that person may send faxes.

For a folder connector, enter the folder path as Faxbot sees it inside its container. Mount the scanner share or other folder at that path, and make sure Faxbot can read and write there. For a folder that sends faxes, put a JSON sidecar next to each PDF with the same filename stem. The sidecar must contain the destination number, for example:

```json
{"to": "+13035550100"}
```

Faxbot waits for each file to stop changing before it handles it. Folder connectors accept PDF, TIFF and TIF documents. Faxbot moves handled files into a `done` subfolder and files it could not use into `failed`; the latter includes a text file with the reason.

## Check and manage connectors

After saving, select **Test** on the connector to check that Faxbot can reach the mailbox or folder. The page shows its status, recent items and whether each item was brought in, sent, refused or seen again. A duplicate is not handled twice.

Select **Pause** to stop checks, or **Resume** to start them again. **Change** edits the connector. **Remove** stops checks and revokes a sending connector's key; items it already handled remain listed.

A sending connector gets a key that can only send faxes. Find it under **Access → Keys & phones**. Pausing the connector revokes that key.
