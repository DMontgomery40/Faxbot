"""CI partitioning must retain every collected case exactly once."""
import os
from pathlib import Path
import random

import pytest

from api.ci_shard import parse_shard, selected_files


def test_every_case_and_module_belongs_to_exactly_one_shard_in_any_collection_order():
    nodes = [f'tests/test_{module}.py::TestCases::test_value[{case}]'
             for module, size in enumerate([300, 1, 18, 55, 7, 23, 41, 100, 6, 12, 2])
             for case in range(size)]
    shuffled = list(nodes)
    random.Random(42).shuffle(shuffled)
    for count in (1, 2, 8, 16):
        groups = [selected_files(nodes, index, count) for index in range(count)]
        assert groups == [selected_files(shuffled, index, count) for index in range(count)]
        assert set.union(*groups) == {node.split('::', 1)[0] for node in nodes}
        assert all(sum(node.split('::', 1)[0] in group for group in groups) == 1 for node in nodes)


@pytest.mark.parametrize('value', ['0/8', '9/8', '1/0', '1/65', '1', '1/2/3', 'x/8'])
def test_invalid_shard_cannot_silently_skip_the_suite(value):
    with pytest.raises(ValueError):
        parse_shard(value)


def test_cli_shard_numbers_are_one_based():
    assert parse_shard('1/8') == (0, 8)
    assert parse_shard('8/8') == (7, 8)


def test_only_the_test_suite_holds_files_pytest_collects():
    """CI runs pytest in api/ with no path, so it collects every test_*.py and *_test.py under it: a product module
    named like one (app/test_lines.py was) is run as tests and fails CI. Only tests/ may hold such files."""
    api = Path(__file__).resolve().parents[1]
    skipped = {'tests', 'node_modules', 'faxdata', '__pycache__', '.venv', '.pytest_cache'}
    found = []
    for folder, children, files in os.walk(api):
        children[:] = [child for child in children if child not in skipped]
        found += [str(Path(folder, name).relative_to(api)) for name in files
                  if name.endswith('.py') and (name.startswith('test_') or name.endswith('_test.py'))]
    assert found == []
