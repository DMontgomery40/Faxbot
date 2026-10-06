# Sinch Fax API v3

Faxbot uploads each PDF directly to Sinch. This differs from the Phaxio connection, which gives the provider a URL to fetch.

## Configure Faxbot

1. In the Admin Console, open **System → Setup** and choose Sinch.
2. Enter your Sinch Project ID, API Key, and API Secret. If you signed up with Phaxio and were redirected to Sinch, those credentials may work; you still need the Sinch Project ID.
3. Apply the changes. To review or change the credentials later, open **Providers → Sinch**.

Faxbot uploads the document to Sinch. The public URL and Phaxio status callback used by the Phaxio connection are not needed for this sending flow. Faxbot checks status using the Sinch account and fax ID captured when it accepted the job; checking status does not submit the fax again. If the outcome is unknown, reconcile it with Sinch before attempting another transmission.

Send faxes from **Faxes → Send a fax** and check their stored status in **Faxes → Sent**. Faxbot accepts PDF and TXT files. If an upload fails, check the Project ID, API Key, API Secret, and the Sinch address setting under **Providers → Sinch**. Faxbot uses Sinch's usual address when that optional setting is empty.

## Receiving faxes

In Sinch, set the incoming fax webhook to the Sinch address under **Addresses to give your provider** on **Providers → In use**. It ends in `/sinch-inbound`. You can set a username and password for this webhook in the Sinch settings under **Providers → Sinch**. With both set, Faxbot checks those credentials. Without them, Faxbot confirms the fax in your Sinch project and downloads its document from Sinch. See [Receiving faxes](../operations/receiving.md).
