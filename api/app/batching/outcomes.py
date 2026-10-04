"""Which faxes in one shared call were delivered, from the call's confirmed page count.

A fax machine confirms each page before the next one is sent, so the pages
confirmed so far are always the first ones. A fax is delivered when its
separator page and all its own pages were confirmed. When the call failed:

- a fax whose separator and pages all came before the failure is delivered;
- a fax the call never reached (at most its separator page could have arrived)
  failed, exactly as a single fax whose call failed: its document was not sent;
- the fax whose separator was confirmed but not all its pages may have partly
  arrived (the page after the last confirmed one may have been on its way), so
  it failed without being resent automatically (``partly_sent``) and a person
  decides;
- with no confirmed page count at all, every fax may have arrived and waits
  for a person (``pages_unconfirmed``).
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Outcome:
    job_id: str
    attempt_id: str
    status: str            # success | failed | unconfirmed
    category: str | None   # partly_sent | pages_unconfirmed | None
    sentence: str | None   # one plain sentence for a failure, at most 80 characters


def _pages(count):
    return '1 page' if count == 1 else f'{count} pages'


def map_call(members, *, succeeded, confirmed_pages, failure_sentence=None):
    """``members``: rows with ``id``, ``attempt_id``, ``first_page``, ``last_page``, in call order.

    ``succeeded``: the fax engine reported the whole call sent. ``confirmed_pages``:
    the call's confirmed page count, or None when it is unknown.
    """
    outcomes = []
    for member in members:
        first, last = member['first_page'], member['last_page']
        own = last - first  # the fax's own pages after its separator
        if succeeded:
            outcomes.append(Outcome(member['id'], member['attempt_id'], 'success', None, None))
        elif confirmed_pages is None:
            outcomes.append(Outcome(member['id'], member['attempt_id'], 'unconfirmed', 'pages_unconfirmed', None))
        elif last <= confirmed_pages:
            outcomes.append(Outcome(member['id'], member['attempt_id'], 'success', None, None))
        elif confirmed_pages < first:
            # Not even its separator was confirmed, so none of its own pages had been sent.
            sentence = (failure_sentence if confirmed_pages == 0 and failure_sentence
                        else 'The call failed before this fax was sent.')
            outcomes.append(Outcome(member['id'], member['attempt_id'], 'failed', None, sentence[:80]))
        else:
            # Its separator was confirmed, so its next page may have been on its way.
            arrived = confirmed_pages - first
            sentence = (f'The call failed after {arrived} of its {_pages(own)}; check before sending again.'
                        if arrived else 'The call failed as this fax began; check before sending again.')
            outcomes.append(Outcome(member['id'], member['attempt_id'], 'failed', 'partly_sent', sentence[:80]))
    return outcomes
