import itertools
import random

import pytest

from sass.sleigh import lower


def exhaustive_keys(values, assignments):
    universe = sorted({k for a in assignments for k in a}, key=repr)
    for size in range(len(universe) + 1):
        for keys in itertools.combinations(universe, size):
            outputs = {}
            for value, assignment in zip(values, assignments):
                for projection in itertools.product(*((
                        assignment[k],) if k in assignment else (0, 1) for k in keys)):
                    outputs.setdefault(projection, set()).add(value)
            if all(len(v) == 1 for v in outputs.values()):
                return keys
    raise AssertionError


def test_partial_rows_can_require_modifier_keys():
    values, assignments = [], []
    for n in range(7):
        values.append(n)
        assignments.append({f'k{i}': 1 for i in range(n)} |
                           ({f'k{n}': 0} if n < 6 else {}) |
                           {f'unused{n}': 0})
    assert lower.determining_keys(values, assignments) == tuple(f'k{i}' for i in range(6))


def test_modifier_key_search_matches_exhaustive_wildcard_expansion():
    rng = random.Random('modifier-key-search')
    for _ in range(100):
        rows = []

        def branches(keys, assignment):
            if not keys or rng.random() < .25:
                rows.append((rng.randrange(4), assignment))
                return
            key = rng.choice(keys)
            for value in (0, 1):
                branches([k for k in keys if k != key], assignment | {key: value})

        branches(list('abcdef'), {})
        values, assignments = zip(*rows)
        assert lower.determining_keys(values, assignments) == exhaustive_keys(values, assignments)


def test_conflicting_wildcard_rows_are_rejected():
    with pytest.raises(AssertionError):
        lower.determining_keys([0, 1], [{'a': 0}, {'b': 1}])
