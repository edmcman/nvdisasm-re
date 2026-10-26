"""Explicit operand rows that reference adapters can target in partial classes.

These contracts do not change an instruction's support classification. Adapters
must preserve the constraints and supply native P-code guards for actual words.
Form/layout restrictions still belong to the adapter and remain reported gaps.
"""
ROWS = {
    **{op: 'aligned data registers; unmodelled predicate outputs discarded'
       for op in ('LD', 'LDL', 'LDS', 'ST', 'STG', 'STL', 'STS')},
    'LDG': 'aligned data registers; ordinary completed load, including Pu',
    **{op: 'aligned destination pair/quad and direct constant bank'
       for op in ('LDC', 'LDCU', 'ULDC')},
    'FLO': 'Pu=PT',
    'UFLO': 'UPu=UPT',
    'MOV': 'all four pixel-mask lanes enabled',
    'R2UR': 'noOR class and Pu=PT; executing thread value',
    'IADD': 'ordinary 32-bit add and Pu=PT',
    'VABSDIFF4': 'Pu=PT',
    'VIMNMX': 'Pu=Pv=PT where present',
    'HADD2': 'F32 widening with a negative zero register operand',
    'HFMA2': 'ordinary register factors plus literal half lanes; default literal-move row',
    'HFMA2.MMA': 'ordinary register factors plus literal half lanes; default literal-move row',
}


def operands(k, form):
    """Fixed operand fields, applied before reference inputs are constructed."""
    op = k.mnemonic
    fields = {}
    predicates = ('UPu',) if op == 'UFLO' else ('Pu', 'Pv', 'Pq')
    if op in {'FLO', 'UFLO', 'R2UR', 'IADD', 'VABSDIFF4', 'VIMNMX'} | {
            'LD', 'LDL', 'LDS', 'ST', 'STG', 'STL', 'STS'}:
        fields.update({n: 'UPT' if n.startswith('UP') else 'PT'
                       for n in predicates if n in k.operand_types})
    if op == 'MOV':
        if 'PixMaskU04' in k.operand_types:
            fields['PixMaskU04'] = 15
    if op in ('HADD2', 'HFMA2', 'HFMA2.MMA'):
        from tests.harness import autocase_packed_fp
        if autocase_packed_fp.constant_move(op, form, k.name):
            fields.update(Ra='RZ', Rb='RZ')
        elif op == 'HADD2' and form.get('ofmt') == 'F32':
            for n in ('Ra', 'Rc'):
                if n in k.operand_types and k.operand_types[n].type in ('Register', 'ZeroRegister') and form.get(n+'@negate') == 'set' and form.get(n+'@absolute', 'unset') == 'unset':
                    fields[n] = 'RZ'
                    break
    return fields
