"""Send short faxes to the same number together in one SIP trunk call.

Off unless an operator records, for one fax number, that the recipient agreed
to receive several documents in one call. It holds a fax only on a route where
one call costs less than several (a per-minute SIP trunk with a minimum, or a
per-call charge). A held fax waits at most the number's maximum wait; it goes
sooner when the call would exceed the page cap or when any waiting fax is
marked "Send now". One call carries the documents in order, each preceded by a
separator page; when the recipient has also agreed to it, one index page
listing each document's page range comes first instead, saving all but one of
those pages. Every fax keeps its own delivery record, attempt, idempotency
and outcome, and nothing is ever resent automatically.

Modules: ``policy`` (when sending together saves money, and why not),
``store`` (settings, history and the per-fax rows), ``outcomes`` (which faxes
the call's confirmed pages delivered), ``image`` (separator pages, the index
page and the one combined fax image), ``transport`` (the worker's batch path),
``results`` (applying the call's result to every fax in it), ``money`` (charge
shares and savings) and ``http`` (the API).
"""
