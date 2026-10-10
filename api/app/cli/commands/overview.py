"""faxbot overview: what the console's Overview shows, read from the same routes.

Needs attention collects what waits for a person (held faxes, uncertain and failed sends, received faxes
without an owner or overdue, email delivery failures, overdue expected faxes and matches to confirm),
grouped by the action it needs, each with the command that lists exactly those faxes. Every source keeps
its own state: one this key may not read, or one that failed, is named, and "Nothing needs attention" is
said only when every source answered. The console computes the same items from the same answers
(admin_ui/src/components/overview/attention.ts); overviewAttention.json holds both to them.
"""
from .. import state
from ..errors import CliError

# The reads, in the order sources are named; the console reads the same routes.
SOURCES = (
    ('health', '/admin/health-status', None),
    ('holds', '/routing/holds', {'state': 'open'}),
    ('work', '/work/counts', None),
    ('intake', '/intake/items', {'limit': 1}),
    ('expected', '/expected-faxes/counts', None),
    ('costs', '/routing/costs', None),
    ('network', '/admin/sip/network', None),
)

SOURCE_NAMES = {
    'health': "Faxbot's status and sent faxes",
    'holds': 'held faxes',
    'work': 'owners of received faxes',
    'intake': 'email delivery',
    'expected': 'expected faxes',
    'costs': 'carrier charges',
    'network': 'the network check',
}

GROUPS = (
    ('serious', 'Serious problems'),
    ('decide', 'Waiting for your decision'),
    ('owner', 'Waiting for an owner, or overdue'),
    ('failed', 'Did not go through'),
    ('check', 'To check'),
)

NOT_READY_TEXT = {
    'send': 'Faxbot is not ready to send faxes',
    'receive': 'Faxbot is not ready to receive faxes',
    'both': 'Faxbot is not ready to send or receive faxes',
}


def read_sources(api, reads=SOURCES):
    """Each source as ('ready', body), ('denied',), ('unavailable',) or ('error',).

    A key Faxbot does not accept, or a server it cannot reach, stops the command instead: every
    source would say the same.
    """
    sources = {}
    for name, path, params in reads:
        try:
            sources[name] = ('ready', api.get(path, params=params))
        except CliError as failure:
            if failure.status is None or failure.status == 401:
                raise
            sources[name] = (('denied',) if failure.status == 403
                             else ('unavailable',) if failure.status in (404, 405) else ('error',))
    return sources


def not_ready_for(health):
    """The direction an installation is set up for that is not ready now: send, receive, both or None."""
    sending = bool(health.get('backend')) and not health.get('backend_healthy')
    receiving = bool(health.get('receiving_backend')) and not health.get('receiving_ready')
    return 'both' if sending and receiving else 'send' if sending else 'receive' if receiving else None


def held_detail(holds):
    def count(kind):
        return sum(1 for hold in holds if hold.get('kind') == kind)
    parts = [text for number, text in ((count('approval'), f"{count('approval')} waiting for approval"),
                                       (count('no_route'), f"{count('no_route')} with no route your rules allow"),
                                       (count('window'), f"{count('window')} waiting for a time window")) if number]
    return f"{', '.join(parts)}. Nothing has been sent for them."


def _list(names):
    return names[0] if len(names) < 2 else f"{', '.join(names[:-1])} and {names[-1]}"


def attention(sources):
    """The Needs attention items, in the console's order, and what could not be checked."""
    items = []

    def add(key, group, label, count, command, *, detail=None, serious=False):
        items.append({'key': key, 'group': group, 'label': label, 'count': count, 'detail': detail,
                      'serious': serious, 'command': command})

    def ready(name):
        kind, *body = sources[name]
        return body[0] if kind == 'ready' else None

    health = ready('health')
    if health is not None:
        if not health.get('backend') and not health.get('receiving_backend'):
            add('no-provider', 'check', 'No fax provider is set up yet', None, 'faxbot delivery providers list')
        direction = not_ready_for(health)
        if direction:
            add('not-ready', 'serious', NOT_READY_TEXT[direction], None, 'faxbot admin health', serious=True)
        jobs = health.get('jobs') or {}
        if jobs.get('reconciliation_required'):
            add('uncertain', 'serious', 'Sent faxes with an uncertain result', jobs['reconciliation_required'],
                'faxbot faxes sent list --status reconciliation_required', serious=True,
                detail='Check your provider account before you send any of them again.')
        if jobs.get('recent_failures'):
            add('failed', 'failed', 'Sent faxes that failed in the last 24 hours', jobs['recent_failures'],
                'faxbot faxes sent list --status failed --hours 24')
    holds = (ready('holds') or {}).get('holds') or []
    if holds:
        add('held', 'decide', 'Faxes your rules are holding', len(holds), 'faxbot faxes sent list --held',
            detail=held_detail(holds))
    expected = ready('expected') or {}
    if expected.get('proposed'):
        add('proposed', 'decide', 'Expected faxes with a possible match to confirm', expected['proposed'],
            'faxbot faxes expected list --show proposed')
    work = ready('work') or {}
    if work.get('unassigned'):
        add('unassigned', 'owner', 'Received faxes waiting for an owner', work['unassigned'],
            'faxbot faxes received owners --unassigned')
    if work.get('overdue'):
        add('overdue', 'owner', 'Received faxes that are overdue', work['overdue'], 'faxbot faxes received owners --overdue')
    if expected.get('overdue'):
        add('expected-overdue', 'owner', 'Expected faxes that are overdue', expected['overdue'],
            'faxbot faxes expected list --show overdue')
    intake = (ready('intake') or {}).get('counts') or {}
    if intake.get('failed'):
        add('not-delivered', 'failed', 'Received faxes not delivered by email', intake['failed'],
            'faxbot faxes received deliveries list --state failed')
    costs = ready('costs')
    if costs is not None:
        unrecorded = sum(row.get('unrecorded_calls') or 0
                         for row in [*(costs.get('providers') or []), *(costs.get('received') or [])])
        if unrecorded:
            add('unrecorded', 'check', 'Carrier charges with no matching fax, last 30 days', unrecorded,
                'faxbot savings spending')
    network = ready('network') or {}
    if network.get('applies') and network.get('action') == 'turned_off' and not network.get('t38_enabled'):
        add('t38-network', 'check',
            'One network change would let faxes go over the internet; faxes still go through meanwhile', None,
            'faxbot delivery providers trunk network status')

    order = [key for key, _ in GROUPS]
    items.sort(key=lambda item: order.index(item['group']))  # stable: each group keeps the order above
    denied = [name for name, *_ in SOURCES if sources[name][0] == 'denied']
    failed = [name for name, *_ in SOURCES if sources[name][0] in ('error', 'unavailable')]
    return {'serious': any(item['serious'] for item in items), 'complete': not denied and not failed,
            'items': items, 'not_available': denied, 'not_checked': failed}


def attention_lines(view):
    """The lines faxbot overview prints for Needs attention."""
    lines = ['Needs attention']
    for group, title in GROUPS:
        members = [item for item in view['items'] if item['group'] == group]
        if members:
            lines.append(title)
        for item in members:
            lines.append(f"  {item['label']}" + (f": {item['count']}" if item['count'] is not None else ''))
            if item['detail']:
                lines.append(f"    {item['detail']}")
            lines.append(f"    {item['command']}")
    if not view['items']:
        lines.append('Nothing needs attention.' if view['complete']
                     else 'Nothing needs attention in what Faxbot could check.')
    if view['not_available']:
        lines.append(f"Not available to this account: {_list([SOURCE_NAMES[name] for name in view['not_available']])}.")
    if view['not_checked']:
        lines.append(f"Could not check {_list([SOURCE_NAMES[name] for name in view['not_checked']])}. "
                     'Run the command again.')
    return lines


# -- The value blocks: what Faxbot is doing, and the next improvements --------------------------------------------

# Read only with settings:read; a key without it is told so for each block.
VALUE_SOURCES = (
    ('capabilities', '/routing/capabilities', None),
    ('savings', '/routing/savings', None),
    ('sending', '/routing/recommendations/sending', None),
    ('facts', '/routing/recommendations/facts', None),
)

BLOCK_TEXT = {
    'denied': 'Not available to this account.',
    'unavailable': 'Not available on this server.',
    'error': 'Could not load this. Run the command again.',
}

# Each unit's parts, never added together: two parts can act on the same fax. Money is the server's own total.
UNIT_PARTS = (
    ('pages', 'Pages not sent', (('separator_pages', 'pages_saved', 'Fewer separator pages'),
                                 ('case_packets', 'pages_saved', 'Case packets'),
                                 ('packing', 'pages_saved', 'Dense pages'), ('encoding', 'pages_saved', 'Encoded pages'),
                                 ('continuation', 'pages_not_resent', 'Only the missing pages'),
                                 ('partner_repair', 'pages_not_resent', 'Missing pages to partners'))),
    ('seconds', 'Call time saved', (('sslfax', 'seconds_saved', 'Faster pages'), ('packing', 'seconds_saved', 'Dense pages'),
                                    ('encoding', 'seconds_saved', 'Encoded pages'),
                                    ('fax_friendly', 'seconds_saved', 'Lighter shading'),
                                    ('coding', 'seconds_saved', 'Smallest page coding'))),
    ('calls', 'Calls avoided', (('sending_together', 'calls_saved', 'Sending together'),
                                ('direct_delivery', 'calls_avoided', 'Direct delivery'),
                                ('direct_fax_images', 'calls_avoided', 'Fax images to partners'),
                                ('own_numbers', 'calls_avoided', 'Faxes to your own numbers'))),
    ('bytes', 'Data not sent again', (('direct_bytes', 'bytes_saved', 'Send once and reuse'),)),
)

IMPROVEMENT_LABELS = {'now': 'You can do this now', 'fact': 'Needs a fact', 'agreement': "Needs a recipient's agreement",
                      'experimental': 'Experimental'}
KIND_ORDER = ('now', 'fact', 'agreement', 'experimental')
IMPROVEMENTS_SHOWN = 8


def _plural(count, one, many=None):
    return f"{count} {one if count == 1 else (many or one + 's')}"


def duration_text(seconds):
    whole = round(seconds)
    if whole < 60:
        return _plural(whole, 'second')
    minutes = round(whole / 60)
    if minutes < 60:
        return _plural(minutes, 'minute')
    return f"{_plural(minutes // 60, 'hour')} {_plural(minutes % 60, 'minute')}"


def bytes_text(count):
    if count < 1000:
        return _plural(count, 'byte')
    if count < 1_000_000:
        return f'{count / 1000:.1f} KB'
    return f'{count / 1_000_000:.1f} MB'


def _unit_value(unit, value):
    if unit == 'pages':
        return _plural(value, 'page')
    if unit == 'seconds':
        return duration_text(value)
    if unit == 'calls':
        return _plural(value, 'call')
    return bytes_text(value)


def _capabilities(answer):
    return [capability for outcome in answer.get('outcomes') or [] for capability in outcome.get('capabilities') or []]


def used_capabilities(answer):
    used = [{'key': c['key'], 'name': c['name'], 'sentence': c['here']['sentence'], 'used': c['here']['used'],
             'address': c['address']}
            for c in _capabilities(answer) if c['here'].get('used', 0) > 0 and c['here'].get('sentence')]
    return sorted(used, key=lambda item: -item['used'])  # stable: equal counts keep the catalogue's order


def result_lines(savings):
    lines = []
    if any(float(amount.get('amount') or 0) != 0 for amount in savings.get('total_saved') or []):
        lines.append({'unit': 'money', 'label': 'Money (estimate)', 'sentence': savings.get('total_sentence'), 'parts': []})
    for unit, label, parts in UNIT_PARTS:
        found = [{'key': part, 'label': name, 'value': _unit_value(unit, (savings.get(part) or {}).get(field) or 0)}
                 for part, field, name in parts if ((savings.get(part) or {}).get(field) or 0) > 0]
        if found:
            lines.append({'unit': unit, 'label': label, 'sentence': None, 'parts': found})
    return lines


def connect_next(answer):
    """For a new installation: the connections that alone would make more capabilities work, and which."""
    found = {}
    for capability in _capabilities(answer):
        missing = [p for p in capability['prerequisites'] if not p['met']]
        if not missing or any(p['kind'] != 'connection' for p in missing):
            continue
        for prerequisite in missing:
            entry = found.setdefault(prerequisite['address'], {'address': prerequisite['address'],
                                                               'label': prerequisite['address_label'], 'capabilities': []})
            if capability['name'] not in entry['capabilities']:
                entry['capabilities'].append(capability['name'])
    return sorted(found.values(), key=lambda entry: -len(entry['capabilities']))[:3]


def next_improvements(capabilities, sending, facts):
    items = []
    for capability in _capabilities(capabilities or {}):
        if capability.get('ready') and capability.get('improvement'):
            items.append({'key': f"capability-{capability['key']}", 'kind': capability['improvement']['kind'],
                          'kind_label': capability['improvement']['label'], 'title': capability['name'],
                          'sentence': capability['sentence'], 'command': capability['setting']['command']})
    for item in ((sending or {}).get('items') or [])[:3]:
        items.append({'key': f"sending-{item['number']}", 'kind': 'now', 'kind_label': IMPROVEMENT_LABELS['now'],
                      'title': f"A cheaper route to {item.get('display_name') or item['number']}",
                      'sentence': item['sentence'], 'command': 'faxbot savings opportunities sending'})
    rows = [(recipient, row) for recipient in (facts or {}).get('recipients') or [] for row in recipient['facts']
            if not row.get('realized')]
    for recipient, row in rows[:3]:
        kind = 'agreement' if row['kind'] == 'authorization' else 'fact'
        items.append({'key': f"fact-{recipient['number']}-{row['fact']}", 'kind': kind,
                      'kind_label': IMPROVEMENT_LABELS[kind],
                      'title': f"{row['title']} ({recipient.get('name') or recipient['display']})",
                      'sentence': row['step'], 'command': 'faxbot savings facts'})
    return [item for kind in KIND_ORDER for item in items if item['kind'] == kind][:IMPROVEMENTS_SHOWN]


def everyday_lines(sources):
    def unreadable(source):
        return BLOCK_TEXT[source[0]] if source[0] in ('denied', 'unavailable') else 'Could not check.'
    work, health, expected, intake = (sources[name] for name in ('work', 'health', 'expected', 'intake'))
    if work[0] == 'ready':
        counts = work[1]
        email = intake[1]['counts'] if intake[0] == 'ready' else {}
        waiting_email = email.get('received', 0) + email.get('sending', 0)
        parts = [_plural(counts['open'], 'open fax', 'open faxes'),
                 counts['unassigned'] and f"{counts['unassigned']} without an owner",
                 waiting_email and f'{waiting_email} waiting for email delivery']
        received = ', '.join(part for part in parts if part) + '.'
    else:
        received = unreadable(work)
    if health[0] == 'ready':
        status, jobs = health[1], health[1].get('jobs') or {}
        if not status.get('backend'):
            sent = ('This installation receives faxes only; sending is not set up.' if status.get('receiving_backend')
                    else 'Sending is not set up yet.')
        else:
            sent = (f"{_plural(jobs.get('queued', 0), 'fax', 'faxes')} waiting to send, "
                    f"{jobs.get('in_progress', 0)} sending now.")
    else:
        sent = unreadable(health)
    waiting = (f"{_plural(expected[1]['waiting'], 'expected fax', 'expected faxes')} waiting." if expected[0] == 'ready'
               else unreadable(expected))
    return [('Received', received), ('Sent', sent), ('Expected', waiting)]


def doing_lines(sources, value):
    lines = ['What Faxbot is doing']
    capabilities = value['capabilities']
    if capabilities[0] != 'ready':
        return lines + [f'  {BLOCK_TEXT[capabilities[0]]}']
    answer, days = capabilities[1], capabilities[1].get('days', 30)
    health = sources['health']
    if health[0] == 'ready' and not health[1].get('backend') and not health[1].get('receiving_backend'):
        lines.append('  No fax provider is set up yet, so Faxbot has nothing to improve so far.')
        for entry in connect_next(answer):
            lines += [f"  Connect in {entry['label']}",
                      f"    Then these work right away: {', '.join(entry['capabilities'])}."]
        return lines
    used = used_capabilities(answer)
    savings = value['savings']
    results = result_lines(savings[1]) if savings[0] == 'ready' else None
    if not used and results == []:
        return lines + [f'  Nothing Faxbot can improve acted on your faxes in the last {days} days, so there are no '
                        'results yet.']
    if used:
        lines.append(f'  Used here in the last {days} days')
        lines += [f"    {item['name']}: {item['sentence']}" for item in used]
    if results is None:
        lines.append('  The results could not be read. Run the command again.')
    elif results:
        lines.append('  Results, each in its own unit')
        for line in results:
            text = line['sentence'] or ' · '.join(f"{part['label']} {part['value']}" for part in line['parts'])
            lines.append(f"    {line['label']}: {text}")
        lines.append('    How each was counted: faxbot savings results')
    return lines


def next_lines(value):
    lines = ['Next improvements']
    capabilities = value['capabilities']
    if capabilities[0] != 'ready':
        return lines + [f'  {BLOCK_TEXT[capabilities[0]]}']
    sending, facts = value['sending'], value['facts']
    items = next_improvements(capabilities[1], sending[1] if sending[0] == 'ready' else None,
                              facts[1] if facts[0] == 'ready' else None)
    if not items:
        lines.append('  Nothing to suggest right now. Every capability, and what each needs, is on the Capabilities '
                     'page.')
    for item in items:
        lines += [f"  {item['title']}: {item['kind_label']}", f"    {item['sentence']}"]
        if item['command']:
            # An automatic capability has no command; its page on the console says where it is set.
            lines.append(f"    {item['command']}")
    if sending[0] in ('error', 'unavailable') or facts[0] in ('error', 'unavailable'):
        lines.append('  Some advice could not be checked. Run the command again.')
    return lines


def overview_view(sources, value):
    """Every block's lines, in the console's order: Needs attention first while a problem is serious."""
    view = attention(sources)
    blocks = {
        'doing': doing_lines(sources, value),
        'next': next_lines(value),
        'everyday': ['Everyday faxes'] + [f'  {label}: {text}' for label, text in everyday_lines(sources)],
        'attention': ['Needs attention'] + [f'  {line}' for line in attention_lines(view)[1:]],
    }
    order = ['attention', 'doing', 'next', 'everyday'] if view['serious'] else ['doing', 'next', 'everyday', 'attention']
    return view, blocks, order


def overview():
    """Show the Overview: what Faxbot is doing for you, the next improvements, everyday faxes and what needs attention, as on the console."""
    api = state.api()
    view, blocks, order = overview_view(read_sources(api), read_sources(api, VALUE_SOURCES))

    def human(out):
        for key in order:
            for line in blocks[key]:
                out.line(line)
        out.line('Every way Faxbot saves money, step by step: faxbot savings mechanisms')
    state.out().result({'attention': view, 'order': order, 'blocks': blocks}, human)


