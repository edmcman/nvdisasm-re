"""Hardware evidence for complete shared-case families; recorded passes never skip execution."""
import hashlib
import json
from functools import cache
from sass.paths import ROOT
from tests.harness import device_cases

PASSES = ROOT / 'tests/baseline/gpu_case_passes.json'


@cache
def generator():
    paths = [*sorted((ROOT / 'src/sass').rglob('*.py')),
             *sorted((ROOT / 'tests/refs').glob('*.py')),
             *sorted(p for p in (ROOT / 'out/SM89').rglob('*') if p.is_file()),
             *(ROOT / 'tests/harness' / n for n in ('device_cases.py', 'device_setup.py', 'device_runner.py', 'device_evidence.py', 'gpu.py', 'case.py'))]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path.relative_to(ROOT)).encode()); digest.update(path.read_bytes())
    return digest.hexdigest()[:24]


def hardware_expected(case):
    expected = (case.hardware or {}).get('expected', case.expected)
    return {k: v for k, v in expected.items() if not k.startswith('__') or k in ('__memory', '__counter', '__fp8_nan')}


def signature(scenarios):
    data = [(s.id, {k: v for k, v in s.case.request.items() if k != 'inspect_pcode'}, hardware_expected(s.case), s.case.hardware)
            for s in scenarios if s.kind != 'emulator']
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:24]


def words(scenarios):
    return sorted({s.case.request['word'] for s in scenarios if s.kind != 'emulator'})


def passes(): return json.loads(PASSES.read_text()) if PASSES.exists() else dict(version=1, families={})


def valid(record, scenarios):
    return (record.get('generator') == generator() and record.get('signature') == signature(scenarios)
            and record.get('words') == words(scenarios) and bool(record.get('device')) and bool(record.get('kernels')))


def record(scenarios, results, exhaustive, device, kernels):
    found = passes(); families = found.setdefault('families', {})
    for family in dict.fromkeys(s.family for s in scenarios):
        selected = [s for s in scenarios if s.family == family and s.kind != 'emulator']
        families.pop(family, None)
        if not selected or not device or any(s.id not in results or results[s.id] for s in selected): continue
        families[family] = dict(generator=generator(), signature=signature(selected), exhaustive=exhaustive,
                                cases=len(selected), words=words(selected), device=device, kernels=sorted(set(kernels)))
    # Atomic replacement prevents interruption from leaving a truncated evidence file.
    path = PASSES.with_suffix('.tmp')
    path.write_text(json.dumps(found, indent=1, sort_keys=True) + '\n'); path.replace(PASSES)


def verified_words():
    out = []
    for family, record in passes().get('families', {}).items():
        if record.get('generator') != generator(): continue
        scenarios = device_cases.selection((family,), record.get('exhaustive', False))
        if valid(record, scenarios): out += [(family, w) for w in record['words']]
    return out
