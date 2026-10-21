"""Generated forms must exercise independent references and remain reproducible."""
import json
import pytest
from sass.decode import pydecode
from sass.sleigh.modifiers import settings
import tests.autocases
import tests.harness.autocases
from tests.harness import lifted, sharded
from tests.harness.case import Cases


def test_factored_legal_forms():
    row = dict(cls='example', combinations=dict(labels={'mode':['A','B'], 'flag':['unset','set']},
               decides=['mode'], table=[['B','native']], free=2))
    assert list(tests.autocases.forms(row)) == [({'mode':'B','flag':'unset'}, 'native'),
                                         ({'mode':'B','flag':'set'}, 'native')]


@pytest.mark.parametrize('cls,form', [
    ('uprmt__URURUR', {'idx':'IDX'}),
    ('popc__RRR_RRR', {'Rb@invert':'set'}),
    ('popc__RCR_RCR', {'Sb@invert':'set'}),
    ('sgxt__RRR_RRR', {'fmt':'S32','cw':'W'}),
    ('brev__RCxR_RCxR', {}),
    ('prmt__RRCx_RRCx', {'pmode':'IDX'}),
    ('prmt__RCxR_RCxR', {'pmode':'F4E'}),
    ('isetp__RCxR_RCxR_EX', {'icmp':'NE', 'fmt':'S32', 'bop':'XOR', 'ex':'EX', 'Pp@not':'set', 'Pr@not':'set'}),
    ('uisetp_optional_upr__URsIUR_URIR', {'icmp':'LT', 'fmt':'U32', 'bop':'AND', 'ex':'EX', 'UPp@not':'unset', 'UPr@not':'set'}),
    ('imnmx__RCxR_RCxR', {'fmt':'S32', 'Pp@not':'set'}),
    ('sel__RCR_RCR', {'Pp@not':'set'}),
    ('usel__URuIUR_URIR', {'UPp@not':'set'}),
    ('idp_2a__CXb', {'mode':'2A.HI', 'SrcAFmt':'S16', 'SrcBFmt':'S8', 'Rc@negate':'set'}),
    ('idp_4a__CXb', {'mode':'4A', 'SrcAFmt':'S8', 'SrcBFmt':'U8', 'Rc@negate':'set'}),
    ('shf__RRCx_RRCx', {'fmt':'S64', 'dir':'R', 'cw':'C', 'hilo':'HI'}),
    ('ushf__URuIUR_URIR', {'fmt':'U32', 'dir':'L', 'cw':'W', 'hilo':'LO'}),
])
def test_generated_reference_cases(cls, form):
    cases = tests.harness.autocases.generate('SM89', cls, form)
    assert cases
    for case in cases:
        assert settings('SM89', pydecode.decode('SM89', bytes.fromhex(case.request['word']))) == form
        status, problems = lifted.run('SM89', 'exact', case)
        assert status == 'native' and not problems, (status, problems)
    # A wrong reference must be detected, rather than accepted as coverage.
    case = cases[0]
    name = next(iter(case.expected))
    wrong = case._replace(expected={**case.expected, name:case.expected[name] ^ 1})
    assert lifted.run('SM89', 'exact', wrong)[1]


def test_unsupported_form_never_enters_suite():
    with pytest.raises(tests.harness.autocases.Unsupported, match='unhandled modifiers'):
        tests.harness.autocases.generate('SM89', 'popc__RRR_RRR', {'Rb@invert':'set', 'mystery':'set'})


def test_saved_plan_joins_existing_family(tmp_path, monkeypatch):
    monkeypatch.setattr(tests.harness.autocases, 'PLANS', tmp_path)
    (tmp_path / 'SM89.json').write_text(json.dumps(dict(version=2, arch="SM89", forms=[
        dict(family='int_alu', cls='popc__RRR_RRR', settings={'Rb@invert':'set'}, profiles=['bits'], native=True)])))
    c = Cases('SM89')
    tests.harness.autocases.append(c, 'memory')
    assert not c
    tests.harness.autocases.append(c, 'int_alu')
    assert len(c) == len(tests.harness.autocases.generate('SM89', 'popc__RRR_RRR', {'Rb@invert':'set'}))


def test_plan_reproducible_and_reports_blocked(monkeypatch):
    monkeypatch.setattr(tests.autocases, 'emulator_cases', lambda *a, **kw: {})
    rows = [dict(cls='popc__RRR_RRR', opcode='POPC', combinations=dict(
        labels={'Rb@invert':['unset','set']}, decides=[], table=[['native']], free=2)),
        dict(cls='other', opcode='OTHER', combinations=dict(
            labels={}, decides=[], table=[['native']], free=1))]
    first, report = tests.autocases.plan('SM89', rows)
    assert first == tests.autocases.plan('SM89', rows)[0]
    assert len(first['forms']) == 2
    assert report['counts']['generated'] == 2
    assert report['counts']['blocked'] == 1
    assert report['gaps'][0]['cls'] == 'other'


def test_manual_regressions_do_not_suppress_generated_coverage(monkeypatch):
    cases = tests.harness.autocases.generate('SM89', 'popc__RRR_RRR', {'Rb@invert':'set'})
    monkeypatch.setattr(tests.autocases, 'emulator_cases', lambda *a, **kw: {'int_alu':cases})
    row = dict(cls='popc__RRR_RRR', opcode='POPC', combinations=dict(
        labels={'Rb@invert':['unset','set']}, decides=[], table=[['native']], free=2))
    generated, report = tests.autocases.plan('SM89', [row])
    assert [e['settings'] for e in generated['forms']] == [{'Rb@invert':'unset'}, {'Rb@invert':'set'}]
    assert report['counts']['generated'] == 2


def test_unresolved_forms_are_reported_without_generating(monkeypatch):
    monkeypatch.setattr(tests.autocases, 'emulator_cases', lambda *a, **kw: {})
    row = dict(cls='flo__RRR_RRR', opcode='FLO', combinations=dict(
        labels={}, decides=[], table=[['partially native']], free=1))
    generated, report = tests.autocases.plan('SM89', [row])
    assert not generated['forms']
    assert report['counts']['unresolved_semantics'] == 1
    assert report['gaps'] == [dict(cls='flo__RRR_RRR', count=1, reason='unresolved semantics')]


def test_simd_plan_uses_its_own_family(monkeypatch):
    monkeypatch.setattr(tests.autocases, 'emulator_cases', lambda *a, **kw: {})
    row = dict(cls='idp_4a__CXb', opcode='IDP', combinations=dict(
        labels={'mode':['4A'], 'SrcAFmt':['S8'], 'SrcBFmt':['U8'], 'Rc@negate':['set']},
        decides=[], table=[['native']], free=1))
    plan, report = tests.autocases.plan('SM89', [row])
    assert report['counts']['generated'] == 1
    assert plan['forms'][0]['family'] == 'int_simd'


def unexplained(job):
    """Manual cases of generated opcodes that record no reason to stay manual."""
    from tests.cases import emulator_cases
    arch, fp, family, exhaustive, _ = job
    cases = emulator_cases(arch, fp, (family,), exhaustive, generated=False)[family]
    # Whole-kernel termination tests have no class.
    return [(arch, case.cls) for case in cases if case.cls in cases.classes
            and cases.classes[case.cls].mnemonic in tests.harness.autocases.OPS and case.id not in cases.manual_reasons]


def test_retained_adapter_cases_have_reasons():
    families = ('int_alu', 'int_simd', 'carry', 'bmsk', 'fp32', 'fp64', 'fp16', 'fp32x2', 'f2fp', 'predicates', 'conversions', 'memory', 'control', 'threads')
    variants = tuple((arch, 'exact') for arch in ('SM89', 'SM107', 'SM120'))
    assert not [found for part in sharded.run(unexplained, variants, families, False, shards=sharded.manual) for found in part]


def test_unknown_or_empty_profile_is_rejected():
    for profiles in ([], ['missing']):
        with pytest.raises(tests.harness.autocases.Unsupported, match='profile'):
            tests.harness.autocases.generate('SM89', 'sel__RRR_RRR', {}, profiles=profiles)


def test_declarative_profiles_keep_boundary_and_random_inputs():
    import random
    from tests.harness import autocase_profiles
    assert {0, 1, 7, 8, 31, 32, 33, 255, 0xffffffff} <= set(autocase_profiles.PROFILES['sgxt']['widths'])
    rng = random.Random('int_alu')
    expected = tuple(rng.getrandbits(32) for _ in range(3))
    assert autocase_profiles.vectors('select_random')[0][:3] == expected
    assert autocase_profiles.values('absolute')[6] == expected[1]
    assert len(autocase_profiles.vectors('carry_select')) == 34
    assert set(range(16)) | {0xffff7654, 0x89ab, 0x6420} <= set(autocase_profiles.PROFILES['permutation']['other'])
    form = {'fmt':'U32', 'Pp@not':'unset'}
    assert 'minmax_random' in autocase_profiles.names('IMNMX', 'imnmx__RRR_RRR', form)
    assert 'minmax_random' not in autocase_profiles.names('IMNMX', 'imnmx__RRR_RRR', dict(form, fmt='S32'))


def test_missing_plan_cannot_silently_drop_migrated_coverage(tmp_path, monkeypatch):
    monkeypatch.setattr(tests.harness.autocases, 'PLANS', tmp_path)
    with pytest.raises(ValueError, match='missing generated coverage plan'):
        tests.harness.autocases.append(Cases('SM89'), 'int_alu')


def test_overlapping_profiles_are_unioned_and_conflicts_rejected():
    cases = tests.harness.autocases.generate('SM89', 'popc__RRR_RRR', {'Rb@invert':'set'})
    first = cases[0]
    cases.append(first)
    size = len(cases) - 1
    assert len(tests.harness.autocases.finish(cases, True)) == size
    cases.append(first._replace(expected={**first.expected, 'R2':-1}))
    with pytest.raises(ValueError, match='conflicting generated expectations'):
        tests.harness.autocases.finish(cases, True)


@pytest.mark.parametrize('arch', ['SM100', 'SM103', 'SM107'])
def test_newer_shift_retains_explicit_noxor_form(arch):
    form = dict(fmt='S32', dir='R', cw='C', hilo='HI', xor='noxor')
    cases = tests.harness.autocases.generate(arch, 'shf__RRR_RRR', form)
    assert cases
    for case in cases:
        assert not lifted.run(arch, 'exact', case)[1]
    with pytest.raises(tests.harness.autocases.Unsupported, match='XOR funnel shifts'):
        tests.harness.autocases.generate(arch, 'shf__RRR_RRR', dict(form, xor='XOR'))


@pytest.mark.parametrize('arch,cls,form', [
    ('SM89', 'iabs__RRR_R', {}),
    ('SM89', 'iabs__RCR_C', {}),
    ('SM89', 'iabs__RCxR_Cx', {}),
    ('SM89', 'umov__UI', {}),
    ('SM89', 'umov__UR', {}),
    ('SM89', 'bmsk__RRR_RRR', {'cw':'W'}),
    ('SM89', 'bmsk__RCxR_RCxR', {'cw':'C'}),
    ('SM107', 'mov_64__RR', {'size':'64'}),
    ('SM107', 'mov_64__RU', {'size':'64'}),
    ('SM120', 'umov_64__UR', {'size':'64'}),
])
def test_expanded_scalar_adapters(arch, cls, form):
    cases = tests.harness.autocases.generate(arch, cls, form)
    assert cases
    for case in cases:
        assert settings(arch, pydecode.decode(arch, bytes.fromhex(case.request['word']))) == form
        status, problems = lifted.run(arch, 'exact', case)
        assert status == 'native' and not problems, (case, status, problems)


def test_mask_profile_preserves_the_dense_domain_and_seed():
    import random
    from tests.harness import autocase_profiles
    pairs = {(a, b) for a, b, *_ in autocase_profiles.vectors('mask_domain')}
    assert pairs == {(a, b) for a in range(34) for b in range(34)}
    rng = random.Random(18)
    expected = (rng.getrandbits(32), rng.getrandbits(32))
    assert autocase_profiles.vectors('mask_edges')[144][:2] == expected


@pytest.mark.parametrize('arch,cls,form', [
    ('SM107', 'vimnmx__RRR_RRR', {'fmt':'S8x4', 'relu':'RELU', 'Pp@not':'set'}),
    ('SM120', 'vimnmx__RIR_RsIR', {'fmt':'S16x2', 'relu':'norelu', 'Pp@not':'unset'}),
    ('SM107', 'vimnmx__RUR_RUR', {'fmt':'U32', 'relu':'norelu', 'Pp@not':'unset'}),
    ('SM107', 'viset__RRR_RRR', {'fmt':'S8x4', 'icmp':'LT'}),
    ('SM107', 'viset__RUR_RUR', {'fmt':'U16x2', 'icmp':'GE'}),
    ('SM107', 'viset__RuIR_RIR', {'fmt':'S32', 'icmp':'NE'}),
])
def test_packed_generated_cases(arch, cls, form):
    cases = tests.harness.autocases.generate(arch, cls, form)
    assert cases
    for case in cases:
        status, problems = lifted.run(arch, 'exact', case)
        assert status == 'native' and not problems, (status, problems)


def test_packed_partial_predicate_outputs_stay_explicit():
    with pytest.raises(tests.harness.autocases.Unsupported, match='predicate outputs'):
        tests.harness.autocases.generate('SM100', 'vimnmx__RRR_RRR',
            {'fmt':'U32', 'relu':'norelu', 'Pp@not':'unset'})


@pytest.mark.parametrize('profile,seed,count,edges', [
    ('packed_minmax', 'vimnmx', 8, (0, 0xffffffff, 0x80007fff, 0x7fff8000, 0x12345678, 0xdeadbeef)),
    ('packed_compare', 'viset', 6, (0, 0xffffffff, 0x80007fff, 0x7fff8000, 0x7f80ff01, 0x12345678)),
])
def test_packed_profiles_preserve_manual_corpora(profile, seed, count, edges):
    import random
    from tests.harness import autocase_profiles
    rng = random.Random(seed)
    words = [*edges, *(rng.getrandbits(32) for _ in range(count))]
    expected = {(a, words[(j + 3) % len(words)] if profile == 'packed_minmax' else
                 a if j % 4 == 0 else words[(j * 5 + 1) % len(words)], 0, pred, 0)
                for j, a in enumerate(words) for pred in ((0, 1) if profile == 'packed_minmax' else (0,))}
    assert set(autocase_profiles.vectors(profile)) == expected


@pytest.mark.parametrize('arch,cls,form', [
    ('SM90', 'viaddmnmx__RCxR_RCxR', {'fmt':'S16x2', 'relu':'RELU', 'Sb@negate':'set', 'Pp@not':'set'}),
    ('SM90', 'viaddmnmx__RRCx_RRCx', {'fmt':'U32', 'relu':'norelu', 'Rb@negate':'set', 'Pp@not':'unset'}),
    ('SM100', 'viaddmnmx__RUR_RUR', {'fmt':'S32', 'relu':'RELU', 'URb@negate':'set', 'Pp@not':'set'}),
    ('SM103', 'viaddmnmx__RuIR_RIR', {'fmt':'U16x2', 'relu':'norelu', 'Pp@not':'unset'}),
    ('SM90', 'vimnmx3__RCxR_RCxR', {'fmt':'S16x2', 'relu':'RELU', 'Pp@not':'set'}),
    ('SM103', 'vimnmx3__RuIR_RIR', {'fmt':'S32', 'relu':'norelu', 'Pp@not':'unset'}),
])
def test_three_input_packed_generated_cases(arch, cls, form):
    cases = tests.harness.autocases.generate(arch, cls, form)
    assert cases
    for case in cases:
        status, problems = lifted.run(arch, 'exact', case)
        assert status == 'native' and not problems, (status, problems)


@pytest.mark.parametrize('profile,seed,count,offsets', [
    ('packed_add_minmax', 'vimnmx', 8, (1, 4)),
    ('packed_minmax3', 'vimnmx3', 6, (2, 4)),
])
def test_three_input_profiles_preserve_manual_corpora(profile, seed, count, offsets):
    import random
    from tests.harness import autocase_profiles
    rng = random.Random(seed)
    words = [0, 0xffffffff, 0x80007fff, 0x7fff8000, 0x12345678, 0xdeadbeef]
    words += [rng.getrandbits(32) for _ in range(count)]
    expected = {(a, words[(j + offsets[0]) % len(words)], words[(j + offsets[1]) % len(words)], pred, 0)
                for j, a in enumerate(words) for pred in (0, 1)}
    assert set(autocase_profiles.vectors(profile)) == expected


@pytest.mark.parametrize('arch,cls,form', [
    ('SM89', 'lop3_lut__RRR_RRR', {'pop':'POR', 'Pp@not':'unset'}),
    ('SM89', 'lop3_lut__RCxR_RCxR', {'pop':'PAND', 'Pp@not':'set'}),
    ('SM89', 'lop3_lut__RUR_RUR', {'pop':'POR', 'Pp@not':'set'}),
    ('SM120', 'lop3_lut__RuIR_RIR', {'pop':'PAND', 'Pp@not':'unset'}),
    ('SM89', 'ulop3_lut__URURUR_URURUR', {'pop':'POR', 'UPp@not':'set'}),
    ('SM120', 'ulop3_lut__URuIUR_URIR', {'pop':'PAND', 'UPp@not':'unset'}),
])
def test_generated_logic_domain(arch, cls, form):
    cases = tests.harness.autocases.generate(arch, cls, form)
    tables = set()
    for case in cases:
        decoded = pydecode.decode(arch, bytes.fromhex(case.request['word']))
        tables.add(decoded.env['imm8'])
        status, problems = lifted.run(arch, 'exact', case)
        assert status == 'native' and not problems, (status, problems)
    assert tables == set(range(256))


def test_logic_seeded_profiles_preserve_manual_inputs():
    import random
    from tests.harness import autocase_profiles
    for profile, seed, count in (('logic_random', 'int_alu', 20), ('uniform_logic_random', 'uniform', 12)):
        rng = random.Random(seed)
        expected = {(*(rng.getrandbits(32) for _ in range(3)), 1, 0) for _ in range(count)}
        assert set(autocase_profiles.vectors(profile)) == expected
    assert autocase_profiles.vectors('logic_legacy') == ((0x81234567, 0x76543210, 0xa5a5f0f0, 1, 0),)


@pytest.mark.parametrize('arch,cls,form', [
    ('SM89','iadd3_x_noimm__RCxR_RCxR',{'X':'X','Ra@invert':'set','Sb@invert':'unset','Rc@invert':'set','Pp@not':'set','Pq@not':'unset'}),
    ('SM120','uiadd3_x__URURUR_URURUR',{'X':'X','URa@invert':'unset','URb@invert':'set','URc@invert':'unset','UPp@not':'set','UPq@not':'set'}),
    ('SM89','lea_hi_noimm__RCR_RCR',{'hilo':'HI','Ra@negate':'set','Sb@negate':'unset'}),
    ('SM89','ulea_hi_noimm_sx32_x__URURUR_URURUR',{'hilo':'HI','sx32':'SX32','X':'X','URa@invert':'set','URb@invert':'unset','UPp@not':'set'}),
    ('SM75','imad_hi__RRC_RRC',{'fmt':'S32','Sc@negate':'set'}),
    ('SM89','imad__RsIR_RIR',{'fmt':'U32','Rc@negate':'unset'}),
    ('SM120','uimad_wide_x__URURUR_URURUR',{'fmt':'S32','X':'X','URc@invert':'set','UPp@not':'set'}),
])
def test_generated_carry_arithmetic(arch,cls,form):
    cases=tests.harness.autocases.generate(arch,cls,form)
    assert cases
    for case in cases:
        status,problems=lifted.run(arch,'exact',case)
        assert status=='native' and not problems,(status,problems)


def test_imad_alias_requires_explicit_permission():
    adapter=tests.harness.autocases
    c=Cases('SM89')
    form={'fmt':'U32','Rc@negate':'unset'}
    s=adapter.Inputs(c,c.classes['imad__RsIR_RIR'],form)
    s.operand(('Ra',),3,4);s.operand(('Sb',),0,6);s.operand(('Rc',),7,8)
    expected={s.output(('Rd',),2):7}
    with pytest.raises(adapter.Unsupported,match='does not preserve'):
        s.add(expected,form)
    s.add(expected,form,imad_alias=True)
    assert len(c)==1
    assert pydecode.decode('SM89',bytes.fromhex(c[0].request['word'])).klass.name=='imad_pseudo__RsIR_RIR'


@pytest.mark.parametrize('profile,seed,count', [('imad_forms','imad-forms',4),('imad_negated','imad-negate',24)])
def test_imad_profiles_preserve_wide_random_inputs(profile,seed,count):
    import random
    from tests.harness import autocase_profiles
    rng=random.Random(seed)
    expected={(rng.getrandbits(32),rng.getrandbits(32),rng.getrandbits(64)) for _ in range(count)}
    assert expected <= {v[:3] for v in autocase_profiles.vectors(profile)}


def test_carry_profile_preserves_both_carry_inputs():
    from tests.harness import autocase_profiles
    vectors=autocase_profiles.vectors('carry_words')
    assert len(vectors)==17*4
    assert {(v[3],v[4]) for v in vectors}=={(0,0),(0,1),(1,0),(1,1)}


@pytest.mark.parametrize('cls,form',[
    ('ubrev__URURUR_URUR',{}),
    ('ubrev__URuIUR_URI',{}),
    ('upopc__URURUR_URUUR',{'URb@invert':'set'}),
    ('upopc__URuIUR_URIR',{}),
    ('usgxt__URURUR_UUU',{'cw':'W','fmt':'S32'}),
    ('usgxt__URuIUR_URIR',{'cw':'C','fmt':'U32'}),
])
def test_uniform_bit_adapters(cls,form):
    cases=tests.harness.autocases.generate('SM89',cls,form)
    assert cases
    for case in cases:
        status,problems=lifted.run('SM89','exact',case)
        assert status=='native' and not problems,(status,problems)


def test_carry_seed_and_pair_addends_are_preserved():
    import random
    from tests.harness import autocase_profiles
    rng=random.Random('carry')
    words=[(0,0,0),(0xffffffff,0xffffffff,0xffffffff),(0x80000000,0x80000000,0),(0xffffffff,1,0),(0,0xffffffff,1)]
    words += [tuple(rng.getrandbits(32) for _ in range(3)) for _ in range(12)]
    assert {v[:3] for v in autocase_profiles.vectors('carry_words')}==set(words)
    assert {v[:3] for v in autocase_profiles.vectors('imad_carry_words')}=={(a,b,(b<<32)|c) for a,b,c in words}


def test_packed_unresolved_gaps_preserve_counts(monkeypatch):
    monkeypatch.setattr(tests.autocases,'emulator_cases',lambda *a,**kw:{})
    row=dict(cls='half_example',opcode='HADD2',combinations=dict(
        labels={'mode':['A','B']},decides=[],table=[['opaque']],free=2))
    plan,report=tests.autocases.plan('SM89',[row])
    assert not plan['forms']
    assert report['counts']['unresolved_semantics']==2
    assert report['gaps']==[dict(cls='half_example',count=2,reason='unresolved semantics')]
