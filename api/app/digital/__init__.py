"""Delivery routes that need no fax call: Direct Secure Messaging and FHIR (D20).

Many fax recipients, especially in US healthcare, can also receive by Direct
Secure Messaging (through a HISP, S/MIME and a trust bundle) or through a FHIR
endpoint. A recipient's Direct address or FHIR endpoint is used only once the
administrator confirms it; the route planner then offers it beside the fax
accounts, priced by the account's plan through the shared predictor, and the
sending rules can name, prefer or forbid it.

Not yet run against a real HISP, FHIR server or EHR: everything here is built
from the published specifications and proven with synthetic peers.
"""
