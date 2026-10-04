"""One plain sentence per work state, due rule and history event.

API sentences that contain a time use the installation's time zone with its
abbreviation (UTC when none is set). The console and the CLI build the same
sentences from ``state_key`` and the item fields with the reader's local time.
"""
from .. import people_time


def time_text(moment):
    """3 Oct 2:05 PM MDT: a short time with its zone, for sentences and files that travel."""
    return people_time.short(moment)


def sentence(note):
    note = (note or '').strip()
    if not note:
        return ''
    return note if note[-1] in '.!?' else note + '.'


def state_key(item, now):
    """waiting, assigned, overdue, escalated, acknowledged or done."""
    if item['state'] == 'done':
        return 'done'
    if item['state'] == 'acknowledged':
        return 'acknowledged'
    owner = item['owner_principal_id']
    if owner is not None and item['escalated_at'] is not None and owner == item['backup_principal_id']:
        return 'escalated'
    if item['due_at'] is not None and now > item['due_at']:
        return 'overdue'
    return 'assigned' if owner is not None else 'waiting'


def state_text(item, names, now):
    """``names`` maps principal ids to display names."""
    key = state_key(item, now)
    owner = names.get(item['owner_principal_id']) or 'someone who is no longer listed'
    if key == 'done':
        note = sentence(item['done_note'])
        return f'Done: {note}' if note else 'Done.'
    if key == 'acknowledged':
        who = names.get(item['acknowledged_by']) or owner
        return f'Acknowledged by {who}.'
    if key == 'escalated':
        return f'Overdue; escalated to {owner}.'
    if key == 'overdue':
        return f'Overdue; assigned to {owner}.' if item['owner_principal_id'] else 'Overdue; waiting for an owner.'
    if key == 'assigned':
        if item['due_at'] is not None:
            return f"Assigned to {owner}; acknowledge by {time_text(item['due_at'])}."
        return f'Assigned to {owner}.'
    return 'Waiting for an owner.'


def due_text(item):
    hours = item['due_hours']
    if not hours:
        return 'No acknowledgement target set'
    span = '1 hour' if hours == 1 else f'{hours} hours'
    where = 'mailbox setting' if item['due_source'] == 'mailbox' else 'installation setting'
    return f'Acknowledge within {span} of the document arriving ({where})'


def event_text(kind, details):
    """One time-free sentence for a history row; names are those stored when it happened."""
    actor = details.get('actor_name') or 'Faxbot'
    if kind == 'received':
        where = details.get('mailbox')
        return f'The document arrived in {where}.' if where else 'The document arrived.'
    if kind == 'assigned':
        return f"{actor} assigned it to {details.get('to_name') or 'someone'}."
    if kind == 'reassigned':
        return f"{actor} moved it from {details.get('from_name') or 'someone'} to {details.get('to_name') or 'someone'}."
    if kind == 'acknowledged':
        return f'{actor} acknowledged it.'
    if kind == 'escalated':
        if details.get('to_name'):
            return f"Not acknowledged in time; escalated to {details['to_name']}."
        if details.get('backup_name'):
            return (f"Not acknowledged in time; {details['backup_name']} is the backup but cannot see this "
                    'document, so the owner did not change.')
        return 'Not acknowledged in time; no backup person is set, so the owner did not change.'
    if kind == 'done':
        note = sentence(details.get('note'))
        return f'{actor} marked it done: {note}' if note else f'{actor} marked it done.'
    if kind == 'reopened':
        return f'{actor} reopened it.'
    if kind == 'exported':
        return f'{actor} exported its evidence.'
    return kind.replace('_', ' ').capitalize() + '.'
