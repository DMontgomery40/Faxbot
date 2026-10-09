"""CI partitioning must retain every collected case exactly once."""
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
