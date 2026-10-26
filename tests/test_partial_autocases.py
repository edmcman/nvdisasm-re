"""Partial classes earn coverage only through explicit reference-backed operand rows."""
import pytest
from sass.assemble import encoding
from sass.decode import pydecode
from sass.sleigh import gen, modifiers, verification
import tests.autocases
import tests.harness.autocases
from tests.harness import autocase_profiles, checks, lifted


@pytest.mark.parametrize('arch,cls,fields', [
    ('SM89', 'flo__RRR_RRR', {'fmt': 'S32', 'sh': 'SH'}),
    ('SM89', 'uflo__URURUR_URURUR', {'fmt': 'U32'}),
    ('SM89', 'r2ur__noOR', {'Pu': 'PT'}),
    ('SM120', 'iadd_noimm__RRR_RRR', {'Ra@negate': 1}),
    ('SM89', 'vabsdiff4__RRR_RRR', {'fmt': 'S8', 'red': 'ACC'}),
    ('SM100', 'vimnmx__RRR_RRR', {'fmt': 'S32', 'Pu': 'PT', 'Pv': 'PT'}),
    ('SM89', 'mov__RR', {'PixMaskU04': 15}),
    ('SM89', 'ldg__sImmOffset', {'sz': '128'}),
    ('SM89', 'ldl__sImmOffset', {'sz': '128'}),
    ('SM89', 'lds__sImmOffset', {'sz': '128'}),
    ('SM89', 'stg__sImmOffset', {'sz': '128'}),
    ('SM89', 'stl__sImmOffset', {'sz': '128'}),
    ('SM89', 'sts__sImmOffset', {'sz': '128'}),
    ('SM89', 'ldc__RaNonRZ', {'sz': '64', 'Sa_bank': 2, 'Ra': 4, 'Ra_offset': 0}),
    ('SM89', 'uldc_const__RCR', {'sz': '64', 'Sa_bank': 2, 'Sa_addr': 0}),
    ('SM100', 'ldcu_const_RCR_', {'sz': '128', 'Sa_bank': 2, 'Sa_offset': 16, 'URa': 'URZ'}),
    ('SM89', 'hfma2_mma__RRI_norelu', {'Ra@negate': 1, 'Sc': 0, 'Sc1': 0}),
    ('SM89', 'hfma2__RRI', {'Ra@negate': 1, 'Sc': 0, 'Sc1': 0}),
    ('SM89', 'hfma2__RRI', {'Ra@negate': 1, 'Ra@absolute': 1, 'iswzA': 'H1_H1', 'Sc': 0, 'Sc1': 0}),
    ('SM89', 'hfma2_mma__RRI_norelu', {'Ra@negate': 1, 'Rb@absolute': 1, 'Sc': 0, 'Sc1': 0}),
    ('SM75', 'hadd2__RR', {'ofmt': 'F32', 'Ra@negate': 1}),
])
@pytest.mark.parametrize('fp', ['exact', 'readable'])
def test_partial_rows_have_independent_values_and_native_guards(arch, cls, fields, fp):
    form = modifiers.settings(arch, pydecode.decode(arch, bytes.fromhex(encoding.encode(arch, cls, **fields))))
    cases = tests.harness.autocases.generate(arch, cls, form, fp, sample=100)
    assert cases and all(case.request['inspect_pcode'] for case in cases)
    for case in cases:
        assert '__native' in case.expected or isinstance(case.expected.get('__max_ops'), dict)
        status, errors = lifted.run(arch, fp, case)
        assert status == 'native' and not errors, (case, status, errors)
        # Even correct values cannot excuse an opaque P-code fallback.
        failures = checks.problems(case.expected, dict(
            registers={n: v for n, v in case.expected.items() if not n.startswith('__')},
            pcode_ops={'CALLOTHER': 1}, pcode_userops=['sass_opaque_' + cls]))
        assert {'native p-code', 'p-code op count'} & set(failures)


def test_partial_planner_uses_normal_profiles_and_native_flag(monkeypatch):
    monkeypatch.setattr(tests.autocases, 'emulator_cases', lambda *args, **kwargs: {})
    row = dict(cls='flo__RRR_RRR', opcode='FLO', combinations=dict(
        labels={'fmt': ['S32', 'U32'], 'sh': ['nosh', 'SH'], 'Rb@invert': ['unset', 'set']},
        decides=[], table=[['partially native']], free=8))
    plan, report = tests.autocases.plan('SM89', [row])
    assert len(plan['forms']) == 8
    assert all(entry['native'] and entry['profile_mode'] == 'full' for entry in plan['forms'])
    assert report['counts']['blocked'] == 0
    assert plan['version'] == 3


def test_adapter_cannot_overwrite_declared_predicate_constraint():
    c = tests.harness.autocases.candidates('SM89', 'flo__RRR_RRR', {'fmt': 'U32', 'sh': 'nosh', 'Rb@invert': 'unset'})
    s = tests.harness.autocases.Inputs(c, c.classes['flo__RRR_RRR'], {})
    s.fields['Pu'] = 0
    with pytest.raises(tests.harness.autocases.Unsupported, match='declared partial operand row'):
        s.add({'R8': 0}, {})


def test_migrated_profiles_keep_bit_and_half_boundaries():
    assert {0, 1, 2, 0x80000000, 0xffffffff, 0xfffffffe, 0x01000000, 0x00ffffff} <= set(
        autocase_profiles.values('flo_bits'))
    spec = autocase_profiles.PROFILES['half_forms']
    assert {0x03ff0400, 0x040003ff, 0x00010001, 0x80010001} <= set(spec['widening'])
    words = (0, 0xffffffff, 0x807f01ff, 0xff010080, 0x7f80ff01, 0x01020304)
    assert {(a, words[(i+2) % 6], words[(i+4) % 6])
            for i, a in enumerate(words)} <= {
                vector[:3] for vector in autocase_profiles.vectors('byte_difference')}


def test_partial_candidate_failure_remains_a_gap(monkeypatch):
    monkeypatch.setattr(tests.autocases, 'emulator_cases', lambda *args, **kwargs: {})
    row = dict(cls='flo__RRR_RRR', opcode='FLO', combinations=dict(
        labels={'fmt': ['UNKNOWN'], 'sh': ['nosh']}, decides=[], table=[['partially native']], free=1))
    plan, report = tests.autocases.plan('SM89', [row])
    assert not plan['forms']
    assert report['counts']['covered_requirements'] == 0
    assert report['counts']['blocked'] == 1
    assert any('unsupported integer format' in str(g.get('attempts')) for g in report['gaps'])


def test_ld_shared_encoding_field_has_only_decoded_modifier_tuples():
    g = gen.Gen('SM89', coverage=True)
    k = encoding.klass('SM89', 'ld__sImmOffset')
    g.build(k, None, [], {})
    row = g.coverage[k.name]
    combinations = row['combinations']
    controls = {tuple(form[n] for n in ('sem', 'sco', 'private'))
                for form, _ in verification.forms(row)}
    # These spellings map to a different first decoding row of TABLES_mem_1.
    assert ('WEAK', 'CTA', 'PRIVATE') not in controls
    assert ('WEAK', 'nosco', 'noprivate') in controls
    # Hex words are stored in little-endian byte order.
    word = int.from_bytes(bytes.fromhex(encoding.encode('SM89', k.name, e='E', Rd=8, Ra=2, Ra_offset=0)), 'little')
    field = next(f for f, rule in k.rules if rule[0:2] == ('table', 'TABLES_mem_1'))
    decoded = set()
    for raw in range(1 << field.width):
        env, _ = pydecode.decode_env(g.arch, k, (word & ~field.mask) | field.put(raw))
        decoded.add(tuple(modifiers.label(g.arch, k.operand_types[n].type, env[n])
                          for n in ('sem', 'sco', 'private')))
    assert controls <= decoded
    assert combinations['table']
