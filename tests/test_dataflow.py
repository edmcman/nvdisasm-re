"""Tier A register dataflow against md roles and exact spans.

SASS_SLEIGH_OUT=/tmp/sass-phase4 python3 tests/test_dataflow.py SM89 [N]
Integration pytest is opt-in with SASS_DATAFLOW=1 (requires compiled languages and pyghidra).
"""
import os
import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).parent.parent)]
import compare
import compare_ghidra
import synth
from sass import mdexpr, pydecode
from sass.gen_sleigh import ENUM_FILE, REG_FILES, RESOURCE_FILE, Gen


def expected(g, d):
    reads, writes = set(), set()
    guard = next((a for a in d.klass.format if a.kind == 'guard'), None)
    for o in g.roles.get(d.klass.name, {}).get('operands', []):
        file = ENUM_FILE.get(o['type'])
        if (not file or file != RESOURCE_FILE.get(o['resource']) or not o['role']
                or o['name'] not in d.env or guard and o['name'] == guard.name):
            continue
        span = o['span']
        if isinstance(span, str): span = int(mdexpr.evaluate(span, g.arch, d.env))
        index = d.env[o['name']]
        base, size, count, zero, _ = REG_FILES[file]
        if index == zero or index + span > count: continue
        cells = set(range(base + size * index, base + size * (index + span)))
        for role in o['role']:
            (writes if role == 'write' else reads).update(cells)
    if guard:
        file = ENUM_FILE[guard.type]
        base, size, _, zero, _ = REG_FILES[file]
        if d.env[guard.name] != zero:
            reads.update(range(base + size * d.env[guard.name], base + size * (d.env[guard.name] + 1)))
    return reads, writes


def actual(ops):
    reads, writes = set(), set()
    for op in ops:
        for nodes, cells in ((op['inputs'], reads), ([op['output']], writes)):
            for n in nodes:
                if n and n['space'] == 'register' and n['offset'] < 0x810:
                    cells.update(range(n['offset'], n['offset'] + n['size']))
    return reads, writes


def check(arch, n=2000):
    g = Gen(arch)
    words = list(synth.synthetic(arch, 8)) + compare.sample(arch, 'real', n)
    todo = []
    for w in words:
        try: todo.append((w, pydecode.decode(arch, w)))
        except pydecode.NoMatch: pass
    results = compare_ghidra.ghidra(arch, [(w, 0) for w, _ in todo], '--dataflow')
    failures = []
    for (w, d), result in zip(todo, results):
        if 'error' in result:
            failures.append(f'{d.klass.name}: {result["error"]}')
        elif (got := actual(result['ops'])) != (want := expected(g, d)):
            failures.append(f'{d.klass.name} {w.hex()} {d.text}: '
                            f'reads missing={sorted(want[0]-got[0])} extra={sorted(got[0]-want[0])}; '
                            f'writes missing={sorted(want[1]-got[1])} extra={sorted(got[1]-want[1])}')
    print(f'{arch}: register dataflow {len(todo)-len(failures)}/{len(todo)}')
    assert not failures, '\n'.join(failures[:20])


def test_dataflow():
    import pytest
    if os.environ.get('SASS_DATAFLOW') != '1':
        pytest.skip('set SASS_DATAFLOW=1 for Ghidra integration')
    check(os.environ.get('SASS_TEST_ARCH', 'SM89'))


if __name__ == '__main__':
    check(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 2000)
