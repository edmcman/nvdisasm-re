"""Hardware-valid shared semantics cases against their independent reference outputs."""
from collections import Counter
from functools import cache
import pytest
from tests.harness import device_cases, device_evidence, sampling
from tests.harness.select import families, selected, skipped


@cache
def scenarios(fams, sample): return device_cases.selection(fams, sample)


def selection(config): return tuple(families(config)), sampling.configured(config)


def pytest_generate_tests(metafunc):
    if 'device_class' not in metafunc.fixturenames: return
    if not selected(metafunc.config, 'gpu'): return skipped(metafunc, 'device_class', 'gpu')
    cases = scenarios(*selection(metafunc.config))
    params = sorted({(s.family, s.case.cls) for s in cases})
    metafunc.parametrize('device_class', params, ids=['-'.join(p) for p in params])


@pytest.fixture(scope='session')
def device_results(request):
    try: device_cases.Driver().close()
    except (OSError, RuntimeError) as error:
        if request.config.getoption('--require-gpu'): raise
        pytest.skip(f'no SM89 GPU: {error}')
    fams, sample = selection(request.config)
    ss = scenarios(fams, sample)
    # Reject duplicate scenario identities with incompatible hardware expectations.
    known = {}
    for s in ss:
        if s.kind == 'emulator': continue
        expected = device_evidence.hardware_expected(s.case)
        if s.id in known and known[s.id] != expected: raise ValueError(f'{s.id}: conflicting hardware expectations')
        known[s.id] = expected
    results, binaries, device = {}, [], None
    batches = device_cases.batches(device_cases.kernels(ss))
    reporter = request.config.pluginmanager.getplugin('terminalreporter')
    for i, batch in enumerate(batches):
        response = device_cases.isolated(batch)
        results.update(response['results']); binaries += response.get('kernels', [])
        device = response['device'] or device
        if reporter: reporter.write_line(f'GPU batch {i + 1}/{len(batches)}: {len(results)} scenarios executed')
    device_evidence.record(ss, results, sample, device, binaries)
    counts = Counter('failed' if results.get(s.id, ['missing result']) else 'passed' for s in ss if s.kind != 'emulator')
    counts['emulator-only'] = sum(s.kind == 'emulator' for s in ss)
    request.config._device_case_summary = counts, Counter(s.reason for s in ss if s.kind == 'emulator')
    return results


@pytest.mark.gpu
def test_cases_on_gpu(device_class, device_results, request):
    ss = [s for s in scenarios(*selection(request.config)) if (s.family, s.case.cls) == device_class]
    runnable = [s for s in ss if s.kind != 'emulator']
    if not runnable: pytest.skip('; '.join(sorted({s.reason for s in ss})))
    failed = [(s, device_results.get(s.id, ['GPU worker returned no result'])) for s in runnable
              if device_results.get(s.id, ['GPU worker returned no result'])]
    assert not failed, '\n'.join(f'{s.id}: {problems}\n  request={s.case.request}\n  expected={s.case.expected}' for s, problems in failed[:8])


@pytest.mark.gpu
def test_generated_probe_rejects_wrong_reference(request):
    """An intentional wrong reference must be reported, never accepted as a hardware pass."""
    try: device_cases.Driver().close()
    except (OSError, RuntimeError) as error:
        if request.config.getoption('--require-gpu'): raise
        pytest.skip(str(error))
    from tests.harness.case import Cases
    c = Cases('SM89')
    c.add('mov__RI', dict(Rd=8, Sb=19, PixMaskU04=15), {}, dict(R8=20))
    s = device_cases.Scenario('runner-self-test', c[0], 'scalar')
    result = device_cases.isolated(device_cases.kernels([s]))
    assert result['results'][s.id] == ['R8: GPU 0x13, reference 0x14']


@pytest.mark.gpu
def test_raw_generated_probes(request, tmp_path):
    try: device_cases.Driver().close()
    except (OSError, RuntimeError) as error:
        if request.config.getoption('--require-gpu'): raise
        pytest.skip(str(error))
    from tests.harness.case import Cases
    from tests.harness import device_evidence
    before = device_evidence.PASSES.read_bytes() if device_evidence.PASSES.exists() else None
    c = Cases('SM89')
    probe = c.probe('mov__RI', dict(Rd=8, Sb=19, PixMaskU04=15), {}, observe=['R8', 'R12'])
    # Even an intentionally wrong existing reference must not affect raw results.
    c[0] = probe._replace(expected={'R8':20})
    c.probe('stg__sImmOffset', dict(Ra=2, Ra_offset=0, Rb=4, sz='128', e='E'),
            dict(R2=0x100, R3=0, R4=1, R5=2, R6=3, R7=4), observe=['R4'],
            observe_memory=[dict(space='ram', address=0x100, size=16)], hardware={'adapter':'memory'})
    result = device_cases.observe(c)
    assert result['observations'][0]['registers'] == {'R8':19, 'R12':0}
    assert result['observations'][1]['memory'][0]['hex'] == '01000000020000000300000004000000'
    assert (device_evidence.PASSES.read_bytes() if device_evidence.PASSES.exists() else None) == before

    import json, subprocess, sys
    input = tmp_path / 'probes.json'; output = tmp_path / 'observations.json'
    input.write_text(json.dumps([dict(cls='mov__RI', fields=dict(Rd=8, Sb=23, PixMaskU04=15), registers={}, observe=['R8'])]))
    subprocess.run([sys.executable, '-m', 'tests.probe', str(input), '--out', str(output)], check=True)
    assert json.loads(output.read_text())['observations'][0]['registers'] == {'R8':23}
