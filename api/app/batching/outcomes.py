"""Which faxes in one shared call were delivered, from the call's confirmed page count.

A fax machine confirms each page before the next one is sent, so the pages
confirmed so far are always the first ones. Each fax occupies a fixed page
range in the call, recorded when the call was formed, under one of two
layouts: a separator page before each fax, or one index page (page 1) that
lists every fax's pages, with no page between one fax and the next.

A fax is delivered when all its own pages were confirmed (and, with
separators, its separator page). When the call failed:

- a fax whose pages all came before the failure is delivered;
- a fax whose first page cannot have been on its way failed, exactly as a
  single fax whose call failed: its document was not sent. With separators
  that is any fax whose separator page was not confirmed; with an index page,
  any fax whose page just before its own first page was not confirmed;
- otherwise the fax may have partly arrived (the page after the last
  confirmed one may have been on its way), so it failed without being resent
  automatically (``partly_sent``) and a person decides. With an index page
  there is no separator between faxes, so a call that ends exactly where one
  fax ends leaves the next one in this state;
- with no confirmed page count at all, every fax may have arrived and waits
  for a person (``pages_unconfirmed``).

When the engine reported the whole call sent but confirmed fewer pages than
the call has, the faxes whose pages were all confirmed are delivered and the
others wait for a person (``pages_unconfirmed``): a recipient files each fax
by its page range, so a missing page is never taken as delivered.
"""
from dataclasses import dataclass


INDEX_PAGE = 'index_page'


@dataclass(frozen=True)
class Outcome:
    job_id: str
    attempt_id: str
    status: str            # success | failed | unconfirmed
    category: str | None   # partly_sent | pages_unconfirmed | None
    sentence: str | None   # one plain sentence for a failure, at most 80 characters


def _pages(count):
    return '1 page' if count == 1 else f'{count} pages'


def own_start(member):
    """The call page where this fax's own pages start (after its separator page, if it has one)."""
    return member['first_page'] if member.get('layout') == INDEX_PAGE else member['first_page'] + 1


def map_call(members, *, succeeded, confirmed_pages, failure_sentence=None):
    """``members``: rows with ``id``, ``attempt_id``, ``first_page``, ``last_page`` and ``layout``, in call order.

    ``layout`` is 'separators' or 'index_page'; missing or None means separators (calls formed before
    the index page existed). ``succeeded``: the fax engine reported the whole call sent.
    ``confirmed_pages``: the call's confirmed page count, or None when it is unknown.
    """
    outcomes = []
    total = max((member['last_page'] for member in members), default=0)
    for member in members:
        last, start = member['last_page'], own_start(member)
        own = last - start + 1
        if succeeded and (confirmed_pages is None or confirmed_pages >= total or last <= confirmed_pages):
            outcomes.append(Outcome(member['id'], member['attempt_id'], 'success', None, None))
        elif succeeded or confirmed_pages is None:
            # Sent, the engine says, but its pages were not all confirmed: a person checks what arrived.
            outcomes.append(Outcome(member['id'], member['attempt_id'], 'unconfirmed', 'pages_unconfirmed', None))
        elif last <= confirmed_pages:
            outcomes.append(Outcome(member['id'], member['attempt_id'], 'success', None, None))
        elif confirmed_pages + 1 < start:
            # The page before its first page was not confirmed, so none of its own pages had been sent.
            sentence = (failure_sentence if confirmed_pages == 0 and failure_sentence
                        else 'The call failed before this fax was sent.')
            outcomes.append(Outcome(member['id'], member['attempt_id'], 'failed', None, sentence[:80]))
        else:
            # The page before its first page was confirmed, so its next page may have been on its way.
            arrived = confirmed_pages - (start - 1)
            sentence = (f'The call failed after {arrived} of its {_pages(own)}; check before sending again.'
                        if arrived else 'The call failed as this fax began; check before sending again.')
            outcomes.append(Outcome(member['id'], member['attempt_id'], 'failed', 'partly_sent', sentence[:80]))
    return outcomes
