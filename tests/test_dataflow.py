"""Register dataflow against md roles and exact spans.

SASS_SLEIGH_OUT=/tmp/sass-semantics python3 tests/test_dataflow.py SM89 [N]
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


def expected(g, d, optimized=False):
    reads, writes = set(), set()
    unused=set()
    if optimized and d.klass.mnemonic in ('LDG','STG','LDL','STL') and d.env.get('e_desc')==0:
        # Implicit pointer forms use their address registers directly.
        unused.update(('Ra_URb','Ra_URc'))
    if optimized and d.klass.mnemonic in ('LOP3','ULOP3') and 'imm8' in d.env:
        lut=d.env['imm8']
        for bit,names in [(2,('Ra','Sa','URa')),(1,('Rb','Sb','URb')),(0,('Rc','Sc','URc'))]:
            # A truth table reads an input only if toggling it changes a result.
            if all((lut>>v&1)==(lut>>(v^(1<<bit))&1) for v in range(8)):
                unused.update(names)
        if d.env.get('Pu',d.env.get('UPu'))==7:unused.update(('Pp','UPp'))
    if optimized and d.klass.mnemonic in ('ISETP','UISETP','FSETP'):
        name='icmp' if d.klass.mnemonic in ('ISETP','UISETP') else 'fcomp'
        operand=d.klass.operand_types.get(name)
        if operand and d.env.get(name) in {g.arch.enums[operand.type].get(n) for n in ('F','T')}:
            unused.update(('Ra','Sa','Rb','Sb','URa','URb'))
    # An unused compound value also drops reads of its address registers.
    for atom in d.klass.format:
        if atom.name not in unused or atom.type not in ('C','CX','A','DESC'):continue
        for child in d.klass.format[d.klass.format.index(atom)+1:]:
            if child.kind=='lit' and child.name==',':break
            if child.kind=='operand':unused.add(child.name)
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
            if role=='read' and o['name'] in unused:continue
            needed=cells
            if optimized and role=='read' and o['name']=='Rb' and d.klass.mnemonic in ('STG','STS','STL'):
                atom=d.klass.operand_types.get('sz');enums=g.arch.enums.get(atom.type,{}) if atom else {}
                width=next((width for label,width in [('U8',1),('S8',1),('U16',2),('S16',2)]
                            if enums.get(label)==d.env.get('sz')),None)
                if width:needed=set(range(base+size*index,base+size*index+width))
            (writes if role == 'write' else reads).update(needed)
    if guard:
        file = ENUM_FILE[guard.type]
        base, size, _, zero, _ = REG_FILES[file]
        if d.env[guard.name] != zero:
            reads.update(range(base + size * d.env[guard.name], base + size * (d.env[guard.name] + 1)))
    return reads, writes


def expected_flow(g, d):
    """Ghidra flow type implied by the md BRANCH_TYPE (predicated forms may also fall through)."""
    kind = g.props.get(d.klass.name, {}).get('BRANCH_TYPE', 'BRT_NONE')
    return {'jump': kind == 'BRT_BRANCH', 'call': kind == 'BRT_CALL', 'terminal': kind in ('BRT_RETURN', 'BRT_BRANCHOUT')}


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
        elif (flow := result['flow']) != (want_flow := expected_flow(g, d)):
            failures.append(f'{d.klass.name} {w.hex()} {d.text}: flow {flow}, md {want_flow}')
        elif (got := actual(result['ops'])) != (want := expected(g, d,
                optimized=not any((op.get('userop') or '').startswith(('sass_opaque_','sass_prim_')) for op in result['ops']))):
            failures.append(f'{d.klass.name} {w.hex()} {d.text}: '
                            f'reads missing={sorted(want[0]-got[0])} extra={sorted(got[0]-want[0])}; '
                            f'writes missing={sorted(want[1]-got[1])} extra={sorted(got[1]-want[1])}')
    print(f'{arch}: register dataflow and control flow {len(todo)-len(failures)}/{len(todo)}')
    assert not failures, '\n'.join(failures[:20])


def test_dataflow():
    import pytest
    if os.environ.get('SASS_DATAFLOW') != '1':
        pytest.skip('set SASS_DATAFLOW=1 for Ghidra integration')
    check(os.environ.get('SASS_TEST_ARCH', 'SM89'))


if __name__ == '__main__':
    check(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 2000)
