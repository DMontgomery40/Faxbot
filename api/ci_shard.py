"""Run the complete backend collection across isolated CI jobs, grouped by file."""
from collections import Counter


def parse_shard(value):
    try:
        index, count = (int(part) for part in value.split('/'))
    except (TypeError, ValueError):
        raise ValueError('Use a one-based shard such as 1/8.') from None
    if not 1 <= index <= count <= 64:
        raise ValueError('Require 1 <= shard <= total <= 64.')
    return index - 1, count


def selected_files(nodeids, index, count):
    """Assign each whole module once; balance by collected test count."""
    sizes = Counter(nodeid.split('::', 1)[0] for nodeid in nodeids)
    loads, assignments = [0] * count, {}
    for path, size in sorted(sizes.items(), key=lambda item: (-item[1], item[0])):
        target = min(range(count), key=lambda shard: (loads[shard], shard))
        assignments[path] = target
        loads[target] += size
    return {path for path, target in assignments.items() if target == index}


def pytest_addoption(parser):
    parser.addoption('--faxbot-test-shard', type=parse_shard, default=None,
                     help='One-based shard, e.g. 1/8; every job collects the full suite first.')


def pytest_collection_modifyitems(config, items):
    shard = config.getoption('--faxbot-test-shard')
    if shard is None:
        return
    paths = selected_files([item.nodeid for item in items], *shard)
    selected, deselected = [], []
    for item in items:
        (selected if item.nodeid.split('::', 1)[0] in paths else deselected).append(item)
    reporter = config.pluginmanager.getplugin('terminalreporter')
    if reporter:
        reporter.write_line(f'Backend shard {shard[0] + 1}/{shard[1]}: '
                            f'{len(selected)} of {len(items)} tests, {len(paths)} whole files')
    config.hook.pytest_deselected(items=deselected)
    items[:] = selected
