"""The California court send profile (research N26): filing with a court and serving papers by fax.

California Rules of Court, rules 2.300-2.306 (read 2026-10-10 at courts.ca.gov):

- **Filing** (rule 2.304): a party may fax a document directly only to a court whose local rule provides for it;
  the mandatory cover sheet MC-005 goes first, each document says "By fax" immediately below its title, and the
  sending machine's transmission record is the filer's proof if the court does not file it (2.304(d)).
- **Signatures** (rule 2.305): a signature produced by fax is deemed an original; the sender keeps the original.
- **Service** (rule 2.306): only on the parties' agreement, confirmed in writing, to the number last given on a
  filed and served document; complete on transmission of the entire document, and after 5 p.m. deemed to occur on
  the next court day; time to act or respond is extended by two court days, except for a notice of intent to move
  for a new trial, a notice of intent to move to vacate a judgment, and a notice of appeal.

Under the profile Faxbot sends the pages exactly as given (no packing, shading, encoded pages, trimming, header
notice or shared call), by a fax call to the number given (no direct delivery, partner relay or alternate number),
and keeps the transmission record with the fax. It states only what the rules say and never makes a legal claim
beyond them: courts are leaving fax (``rules.COURTS``), and the court's own local rule decides whether it takes one.
"""
