"""GPU probe construction, accounting and evidence validity without requiring a GPU."""
import dataclasses
import json
import pickle
import subprocess
from types import SimpleNamespace
import pytest
from sass.decode import cubin, pydecode
from tests.harness import device_cases, device_evidence, sharded
from tests.harness.case import Cases, cbank, ram


def scenario(value=19, expected=None, **kwargs):
    c = Cases('SM89')
    c.add('mov__RI', dict(Rd=8, Sb=value, PixMaskU04=15), {}, dict(R8=value if expected is None else expected), **kwargs)
    case = c.encode_words()[0]
    return device_cases.Scenario('unit', case, *device_cases.classify(case))


def test_target_encoding_and_scratch_preserve_aliases():
    c = Cases('SM89')
    c.add('imad_wide__RRR_RRR', dict(Rd=2, Ra=2, Rb=4, Rc=6, fmt='U32'), dict(R2=3, R4=4, R6=5), dict(R2=17, R3=0))
    s = device_cases.Scenario('unit', c.encode_words()[0], 'scalar')
    p = device_cases.scalar_probe([s], 'probe')
    image = device_cases.image([p]); kernel = cubin.kernel_image(image, 'probe')
    offset, target = p.code.targets[0]
    actual = int.from_bytes(kernel['code'][offset:offset + 16], 'little')
    assert actual & device_cases.ENCODING == int.from_bytes(bytes.fromhex(target), 'little') & device_cases.ENCODING
    assert not {f'R{i}' for i in range(device_cases.scratch(c[0]), device_cases.scratch(c[0]) + 8)} & device_cases.resources(target)
    assert device_cases.resources(target) >= {'R2', 'R3', 'R4', 'R6', 'R7'}


def test_constant_and_uniform_predicate_outputs():
    c = Cases('SM89')
    c.add('mov__RC', dict(Rd=8, Sb_bank=2, Sb_addr=16, PixMaskU04=15), {}, dict(R8=19), memory=[cbank(2, 16, 19)])
    c.add('up2ur__Imm', dict(URd=4, UPR='UPR', URa=4, Sb=0x7f, insert='B0'), dict(UR4=1, UP0=1), dict(UR4=1, UP0=1))
    for case in c.encode_words():
        p = device_cases.scalar_probe([device_cases.Scenario('unit', case, 'scalar')], 'probe')
        kernel = cubin.kernel_image(device_cases.image([p]), 'probe')
        if case is c[0]: assert kernel['constants'][2][16:20] == b'\x13\0\0\0'
        assert p.output_size == 4 * len(p.observations[0][1])


def test_unknown_state_is_an_error_and_backend_checks_are_explicit():
    s = scenario()
    with pytest.raises(ValueError, match='unclassified request'):
        device_cases.classify(s.case._replace(request=dict(s.case.request, surprise=True)))
    with pytest.raises(ValueError, match='context requires'):
        device_cases.classify(s.case._replace(request=dict(s.case.request, context={'future': True})))
    with pytest.raises(ValueError, match='unclassified expectations'):
        device_cases.classify(s.case._replace(expected={'__future': True}))
    assert device_cases.classify(s.case._replace(expected={'error': 'runtime context missing'}))[0] == 'emulator'
    assert device_cases.classify(s.case._replace(hardware={'skip': 'deliberately synthetic'})) == ('emulator', 'deliberately synthetic')


def test_fingerprint_is_what_runs_and_how_it_is_judged(monkeypatch):
    """A pass is tied to the probe and its judgement, not to companions, families or harness sources."""
    first, changed, other = scenario(), scenario(expected=20), scenario(value=20)
    assert first.id == changed.id != other.id
    fingerprint = device_evidence.fingerprint
    assert fingerprint(first) == fingerprint(scenario()) == fingerprint(device_cases.Scenario('elsewhere', first.case, 'scalar'))
    assert len({fingerprint(first), fingerprint(changed), fingerprint(other), fingerprint(dataclasses.replace(first, arch='SM90'))}) == 4
    both = [device_evidence.summary(s) for s in ([first], [other])]
    assert device_evidence.merge(*both) == device_evidence.summary([other, first]) == device_evidence.merge(*both[::-1])
    before, capture = fingerprint(first), device_cases.capture
    monkeypatch.setattr(device_cases, 'nan_lanes', lambda case, arch: (0, 16))
    assert fingerprint(first) != before
    monkeypatch.undo()
    monkeypatch.setattr(device_cases, 'capture', lambda code, *a, **k: (code.emit('nop_'), capture(code, *a, **k))[1])
    assert fingerprint(first) != before


def test_cx_backing_and_bounds_survive_relocation():
    from tests.harness.case import cx_header
    c = Cases('SM89')
    c.add('bmsk__RCxR_RCxR', dict(Rd=8, Ra=2, URb=4, Sb_offset=16, cw='C'),
          dict(R2=1, **cx_header('SM89', 4, 0x1000, units=1)), dict(R8=0), memory=[ram(0x1010, 5)])
    case = c.encode_words()[0]; assert device_cases.classify(case) == ('cx', '')
    p = device_cases.kernels([device_cases.Scenario('unit', case, 'cx')])[0]
    assert int.from_bytes(p.arena[16:20], 'little') == 5
    offset, _ = p.code.targets[0]
    target = pydecode.decode('SM89', p.code.words[offset // 16].to_bytes(16, 'little'))
    assert target.env['Sb_offset'] == 16


def test_sweep_policy_and_all_cases_classified():
    """Classifies minimal shared cases shard by shard, retaining manual regressions."""
    from tests.cases import FAMILIES
    sampled = device_evidence.summaries(FAMILIES, 0)
    manual = [device_evidence.summaries(FAMILIES, e, sharded.manual) for e in (0, 100)]
    assert all(manual[0][f]['ids'] == sampled[f]['ids'] <= manual[1][f]['ids'] for f in FAMILIES)
    assert sum(m['cases'] for m in manual[1].values()) > sum(m['cases'] for m in manual[0].values())
    assert set().union(*(s['kinds'] for s in sampled.values())) >= {'scalar', 'cx', 'memory', 'warp', 'branch', 'qspc', 'emulator'}
    assert not any(s['reasons'][''] for s in sampled.values())


def test_variable_latency_alu_uses_scoreboard():
    from tests.harness.case import word
    for cls in ('dsetp__RRR_RR', 'fchk__RRR_RR', 'brev__RCR_RCR'):
        scheduled = device_cases.scheduled(word('SM89', cls))
        assert scheduled >> 110 & 7 == 0
        assert scheduled >> 116 & 63 == 63


def test_fault_bisection_preserves_healthy_results(monkeypatch):
    good, bad = scenario(19), scenario(20)
    calls = []
    def run(argv, input, **kwargs):
        probes = pickle.loads(input); ss = [s for p in probes for s in p.scenarios]
        calls.append(len(ss))
        result = dict(error='device fault') if any(s.id == bad.id for s in ss) else dict(results={s.id: [] for s in ss}, device='SM89', kernels=['hash'])
        return SimpleNamespace(returncode=0, stdout=pickle.dumps(result), stderr=b'')
    monkeypatch.setattr(subprocess, 'run', run)
    result = device_cases.isolated(device_cases.kernels([good, bad]))
    assert result['results'] == {good.id: [], bad.id: ['device fault']}
    assert calls == [2, 1, 1]


def test_worker_timeout_is_a_failure(monkeypatch):
    def run(*args, **kwargs): raise subprocess.TimeoutExpired('worker', 1)
    monkeypatch.setattr(subprocess, 'run', run)
    s = scenario()
    assert device_cases.isolated(device_cases.kernels([s]), timeout=1)['results'][s.id] == ['GPU worker exceeded 1s']


def test_nan_policy_preserves_merged_half():
    c = Cases('SM89')
    c.add('f2fp_merge_c__RRR', dict(Rd=8, Rb=2, Rc=4, dstfmt='F16', srcfmt='F32', merge='MERGE_C'),
          dict(R2=0x7fc00000, R4=0xbeef), dict(R8=0xbeef7e00))
    assert device_cases.compare(c.encode_words()[0], ['R8'], [0xbeef7fff]) == []
    assert device_cases.compare(c[0], ['R8'], [0xdead7fff])


def test_raw_probe_has_no_expected_values_and_cannot_enter_regression_suite(monkeypatch):
    from tests import cases
    c = Cases('SM89')
    probe = c.probe('mov__RI', dict(Rd=8, Sb=19, PixMaskU04=15), {}, observe=['R8'])
    assert probe.expected == {}
    assert probe.request['observe'] == ['R8']
    with pytest.raises(ValueError, match='needs registers or memory'):
        c.probe('mov__RI', {}, {})
    monkeypatch.setattr(cases, 'module', lambda family: SimpleNamespace(emulator_cases=lambda collection: collection.extend(c)))
    with pytest.raises(ValueError, match='draft probes need reference'):
        cases.emulator_cases('SM89', 'exact', ('draft',))


def test_raw_memory_capture_reassembles_multiple_regions():
    s = scenario()
    case = s.case._replace(request=dict(s.case.request, observe_memory=[dict(space='ram', address=0x100, size=8), dict(space='shared', address=0x20, size=1)]))
    result = device_cases.observation(case, ['R8', '__memory:0:0:4', '__memory:0:4:4', '__memory:1:0:1'], [19, 0x12345678, 0x90abcdef, 0x55])
    assert result == dict(registers={'R8':19}, memory=[dict(space='ram', address=0x100, size=8, hex='78563412efcdab90'), dict(space='shared', address=0x20, size=1, hex='55')])


def test_passes_are_per_scenario(tmp_path, monkeypatch):
    monkeypatch.setattr(device_evidence, 'PASSES', tmp_path / 'passes.json')
    a, b, new = scenario(), scenario(value=20), scenario(value=21)
    fa, fb = map(device_evidence.fingerprint, (a, b))
    device_evidence.record([a, b], {a.id: [], b.id: []}, 'SM89 test')
    assert device_evidence.passed('SM89') == {fa, fb}
    word = lambda s: s.case.request['word']
    found = lambda *ss: lambda families, sample, arch: {'unit': device_evidence.summary(ss)}
    # An unrun case leaves the other words verified; a word with an unrun scenario is not.
    monkeypatch.setattr(device_evidence, 'summaries', found(a, b, new))
    assert device_evidence.verified_words() == sorted([('unit', word(a)), ('unit', word(b))])
    monkeypatch.setattr(device_evidence, 'summaries', found(a, b, scenario(value=20, expected=0)))
    assert device_evidence.verified_words() == [('unit', word(a))]
    # A missing result keeps a pass, a failure drops it, and a run without a device adds nothing.
    device_evidence.record([a, b], {b.id: ['R8: GPU 0x14, reference 0x15']}, 'SM89 test')
    assert device_evidence.passed('SM89') == {fa}
    device_evidence.record([b], {b.id: []}, None)
    assert device_evidence.passed('SM89') == {fa}
    device_evidence.PASSES.write_text(json.dumps(dict(version=3, architectures={})))
    with pytest.raises(ValueError, match='version 3'): device_evidence.passes()
