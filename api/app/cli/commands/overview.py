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


def read_sources(api):
    """Each source as ('ready', body), ('denied',), ('unavailable',) or ('error',).

    A key Faxbot does not accept, or a server it cannot reach, stops the command instead: every
    source would say the same.
    """
    sources = {}
    for name, path, params in SOURCES:
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
            add('no-provider', 'check', 'No fax provider is set up yet', None, 'faxbot providers list')
        direction = not_ready_for(health)
        if direction:
            add('not-ready', 'serious', NOT_READY_TEXT[direction], None, 'faxbot system health', serious=True)
        jobs = health.get('jobs') or {}
        if jobs.get('reconciliation_required'):
            add('uncertain', 'serious', 'Sent faxes with an uncertain result', jobs['reconciliation_required'],
                'faxbot sent list --status reconciliation_required', serious=True,
                detail='Check your provider account before you send any of them again.')
        if jobs.get('recent_failures'):
            add('failed', 'failed', 'Sent faxes that failed in the last 24 hours', jobs['recent_failures'],
                'faxbot sent list --status failed')
    holds = (ready('holds') or {}).get('holds') or []
    if holds:
        add('held', 'decide', 'Faxes your rules are holding', len(holds), 'faxbot sent list --held',
            detail=held_detail(holds))
    expected = ready('expected') or {}
    if expected.get('proposed'):
        add('proposed', 'decide', 'Expected faxes with a possible match to confirm', expected['proposed'],
            'faxbot expected list --show proposed')
    work = ready('work') or {}
    if work.get('unassigned'):
        add('unassigned', 'owner', 'Received faxes waiting for an owner', work['unassigned'],
            'faxbot received owners --unassigned')
    if work.get('overdue'):
        add('overdue', 'owner', 'Received faxes that are overdue', work['overdue'], 'faxbot received owners --overdue')
    if expected.get('overdue'):
        add('expected-overdue', 'owner', 'Expected faxes that are overdue', expected['overdue'],
            'faxbot expected list --show overdue')
    intake = (ready('intake') or {}).get('counts') or {}
    if intake.get('failed'):
        add('not-delivered', 'failed', 'Received faxes not delivered by email', intake['failed'],
            'faxbot received deliveries list --state failed')
    costs = ready('costs')
    if costs is not None:
        unrecorded = sum(row.get('unrecorded_calls') or 0
                         for row in [*(costs.get('providers') or []), *(costs.get('received') or [])])
        if unrecorded:
            add('unrecorded', 'check', 'Carrier charges with no matching fax, last 30 days', unrecorded,
                'faxbot costs spending')
    network = ready('network') or {}
    if network.get('applies') and network.get('action') == 'turned_off' and not network.get('t38_enabled'):
        add('t38-network', 'check',
            'One network change would let faxes go over the internet; faxes still go through meanwhile', None,
            'faxbot providers trunk network status')

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


def overview():
    """Show what needs attention now, as on the console's Overview: held faxes, uncertain and failed sends, received faxes without an owner, email delivery failures and overdue expected faxes, each with the command that lists them."""
    view = attention(read_sources(state.api()))

    def human(out):
        for line in attention_lines(view):
            out.line(line)
    state.out().result({'attention': view}, human)
