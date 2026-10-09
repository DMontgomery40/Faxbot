"""One plain sentence per expected-fax state, proposal, history event and reconciliation line.

Sentences that contain a time use the installation's time zone with its
abbreviation (``people_time.short``). The console and the CLI show the same
sentences; the console writes the due time again in the reader's local time.
"""
from .. import people_time
from .expectations import loads


TARGET = 'This is an operational target, not a legal deadline.'
SIGNAL_TEXT = {
    'subaddress': 'its subaddress',
    'form_field': "the partner's registered form",
    'digital_message': 'its Direct message ID',
    'email_subject': 'its email subject',
    'counterparty_number': "the sender's fax number",
    'partner': 'the partner who sent it',
    'direct_address': "the sender's Direct address",
    'extraction': 'text read from the document',
    'person': 'a person',
}
DUE_TEXT = {
    'entered': 'the due time given when it was added',
    'import': 'the due time in the import',
    'source': "the import source's hours",
    'mailbox': "the mailbox's acknowledgement target",
    'installation': "the installation's acknowledgement target",
}
RECONCILE_TEXT = {
    'done': 'Already done during the outage; record it in the source system and do not submit it again.',
    'new': 'Nothing was done during the outage; submit it normally.',
    'not_in_export': 'Recorded during the outage, but the new export does not list it; check it by hand.',
    'revision': 'Recorded during the outage for revision {recorded}, but the export now shows revision {revision}; '
                'check which one was done.',
    'several': 'More than one action was recorded during the outage for this item; check which one stands.',
    'uncertain': 'The action recorded during the outage may not have gone through; check it before doing anything.',
    'fax_not_confirmed': 'The fax sent for it during the outage is not confirmed as delivered; check it before '
                         'doing anything.',
    'answered': 'A fax answered this during the outage, but no action was recorded for it; decide whether to '
                'record it.',
}


def time_text(moment):
    return people_time.short(moment)


def sentence(note):
    note = (note or '').strip()
    if not note:
        return ''
    return note if note[-1] in '.!?' else note + '.'


def state_key(row, now):
    """waiting, proposed, overdue, matched, cancelled, completed_elsewhere or replaced."""
    state = row['state']
    if state == 'proposed_match':
        return 'proposed'
    if state == 'overdue' or (state == 'open' and row['due_at'] is not None and now > row['due_at']):
        return 'overdue'
    return 'waiting' if state == 'open' else state


def state_text(row, names, now, *, match_signal=None):
    key = state_key(row, now)
    if key == 'matched':
        if row['matched_by']:
            who = names.get(row['matched_by']) or 'someone who is no longer listed'
            return f"Matched by {who} on {time_text(row['matched_at'])}."
        how = SIGNAL_TEXT.get(match_signal, 'its reference')
        return f"Arrived and matched by {how} on {time_text(row['matched_at'])}."
    if key == 'proposed':
        return 'A received fax may be the one; confirm or reject it.'
    if key == 'overdue':
        owner = names.get(row['owner_principal_id'])
        if row['escalated_at'] is not None and owner:
            return f"Overdue since {time_text(row['due_at'])}; escalated to {owner}."
        return f"Overdue since {time_text(row['due_at'])}; nothing that closes it has arrived."
    if key == 'cancelled':
        who = names.get(row['closed_by']) or 'someone'
        return f"Cancelled by {who}: {sentence(row['closed_note'])}"
    if key == 'completed_elsewhere':
        who = names.get(row['closed_by']) or 'someone'
        return f"Completed another way, recorded by {who}: {sentence(row['closed_note'])}"
    if key == 'replaced':
        return 'Replaced by a newer revision from the import.'
    if row['due_at'] is not None:
        return f"Waiting; expected by {time_text(row['due_at'])}."
    return 'Waiting; no due time is set.'


def due_text(row):
    if row['due_at'] is None:
        return 'No due time is set.'
    hours = row['due_hours']
    where = DUE_TEXT.get(row['due_source'], 'the due time given')
    if hours:
        span = '1 hour' if hours == 1 else f'{hours} hours'
        return f'Expected within {span}, from {where}. {TARGET}'
    return f'Expected by {time_text(row["due_at"])}, from {where}. {TARGET}'


def parts_text(parts):
    parts = [part for part in parts if part]
    if not parts:
        return ''
    return parts[0] if len(parts) == 1 else ', '.join(parts[:-1]) + ' and ' + parts[-1]


def proposal_text(link, row):
    """Why Faxbot thinks a received fax may answer this expectation, in one sentence."""
    evidence = loads(link['evidence'], {}) or {}
    reason, how = link['reason'], SIGNAL_TEXT.get(link['signal'], 'its reference')
    if reason == 'revision_unstated':
        return (f"It carries the reference in {how}, but does not say which revision; you expect revision "
                f"{row['required_revision']}.")
    if reason == 'wrong_revision':
        return (f"It carries the reference in {how}, but says revision {evidence.get('revision')}; you expect "
                f"revision {row['required_revision']}.")
    if reason == 'parts':
        return (f"It carries the reference in {how}; check that it includes "
                f"{parts_text(loads(row['required_parts'], []))}.")
    if reason == 'ambiguous':
        return f'Its reference in {how} fits more than one expected fax; choose which one it answers.'
    if reason == 'extraction':
        return 'Text read from the document mentions this reference; check the document before confirming.'
    who = row['counterparty'] or 'the sender'
    start = {'counterparty_number': f'It came from a fax number you listed for {who}',
             'partner': f'It came from {who}, the partner you expect this from',
             'direct_address': f"It came from {who}'s Direct address"}.get(link['signal'], 'It came from the sender')
    return f"{start}, but nothing in it names {row['reference']}."


def event_text(kind, details):
    """One time-free sentence for a history row; names are those stored when it happened."""
    actor = details.get('actor_name') or 'Faxbot'
    how = SIGNAL_TEXT.get(details.get('signal'), 'its reference')
    if kind == 'created':
        if details.get('source_name'):
            return f"Imported from {details['source_name']}."
        return f'{actor} added it.'
    if kind == 'revised':
        return f"A new revision ({details.get('revision') or 'unnamed'}) arrived in the import."
    if kind == 'replaced':
        return f"Replaced by revision {details.get('revision') or 'unnamed'} from the import."
    if kind == 'conflict':
        return 'The import changed this row without a new revision; the first version is kept until you decide.'
    if kind == 'conflict_kept':
        return f"{actor} kept the first version and set the import's change aside."
    if kind == 'conflict_applied':
        return f"{actor} used the import's changed version."
    if kind == 'proposed':
        return f'A received fax may answer it ({how}); waiting for a person.'
    if kind == 'proposal_rejected':
        note = sentence(details.get('note'))
        return f'{actor} said a proposed fax does not answer it.' + (f' {note}' if note else '')
    if kind == 'matched':
        return f'A received fax answered it, matched by {how}.'
    if kind == 'confirmed':
        return f'{actor} confirmed a received fax answers it.'
    if kind == 'also_arrived':
        return f'Another received fax carried its reference ({how}); the match did not change.'
    if kind == 'overdue':
        if details.get('to_name'):
            return f"Not answered in time; escalated to {details['to_name']}."
        if details.get('backup_name'):
            return (f"Not answered in time; {details['backup_name']} is the backup but cannot see this mailbox, so "
                    'the owner did not change.')
        return 'Not answered in time; no backup person is set, so the owner did not change.'
    if kind == 'cancelled':
        return f"{actor} cancelled it: {sentence(details.get('note'))}"
    if kind == 'completed_elsewhere':
        return f"{actor} recorded that it was completed another way: {sentence(details.get('note'))}"
    if kind == 'missing_from_export':
        return 'The latest full export no longer lists it; it stays open until a person decides.'
    if kind == 'back_in_export':
        return 'The export lists it again.'
    if kind == 'outage_action':
        return f"During an outage, {actor} recorded: {sentence(details.get('action'))}"
    if kind == 'exported':
        return f'{actor} exported its evidence.'
    return kind.replace('_', ' ').capitalize() + '.'
