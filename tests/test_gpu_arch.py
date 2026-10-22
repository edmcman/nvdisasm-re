"""Architecture boundaries in GPU selection, generated wrappers and recorded evidence."""
import json
from types import SimpleNamespace
import pytest
from sass import launch
from sass.assemble import elf
from sass.decode import cubin
from tests.cases import FAMILIES, gpu_fixtures
from tests.harness import device_cases, device_evidence, gpu, hardware, templates
from tests.harness.case import Cases, cbank, cx_header, ram


class Config:
    def __init__(self, arch=None, required=False): self.options = {'--arch': arch, '--require-gpu': required}
    def getoption(self, name): return self.options[name]


@pytest.mark.parametrize('selection', [None, 'SM90', 'SM89,SM90', 'all'])
def test_gpu_target_detected_once(monkeypatch, selection):
    calls = []
    def driver():
        calls.append(True)
        return SimpleNamespace(arch='SM90', close=lambda: None)
    monkeypatch.setattr(gpu, 'Driver', driver)
    config = Config(selection)
    assert gpu.target(config) == gpu.target(config) == 'SM90'
    assert len(calls) == 1


def test_gpu_target_mismatch_and_unavailable(monkeypatch):
    monkeypatch.setattr(gpu, 'Driver', lambda: SimpleNamespace(arch='SM90', close=lambda: None))
    with pytest.raises(ValueError, match='does not include'): gpu.target(Config('SM89'))
    def unavailable(): raise OSError('no CUDA')
    monkeypatch.setattr(gpu, 'Driver', unavailable)
    assert gpu.target(Config()) is None
    with pytest.raises(OSError, match='no CUDA'): gpu.target(Config(required=True))


def test_worker_rejects_architecture_before_cuda_load():
    driver = object.__new__(gpu.Driver)
    driver.arch = 'SM90'
    driver.require_arch('SM90')
    with pytest.raises(ValueError, match='does not match'): driver.require_arch('SM89')
    c = Cases('SM89')
    c.add('mov__RI', dict(Rd=8, Sb=19, PixMaskU04=15), {}, dict(R8=19))
    probe = device_cases.kernels([device_cases.Scenario('unit', c.encode_words()[0], 'scalar')])[0]
    # A dummy driver has no CUDA library: the architecture guard must fail first.
    with pytest.raises(ValueError, match='does not match'): device_cases.execute(driver, [probe])


@pytest.mark.parametrize('arch', ['SM89', 'SM90'])
def test_probe_abi_and_cx_relocation(arch):
    c = Cases(arch)
    c.add('bmsk__RCxR_RCxR', dict(Rd=8, Ra=2, URb=4, Sb_offset=16, cw='C'),
          dict(R2=1, **cx_header(arch, 4, 0x1000)), dict(R8=0), memory=[ram(0x1010, 5)])
    case = c.encode_words()[0]
    s = device_cases.Scenario('unit', case, *device_cases.classify(case, arch=arch), arch=arch)
    probe = device_cases.kernels([s])[0]
    image = cubin.kernel_image(device_cases.image([probe]), probe.name)
    assert image['arch'] == arch
    assert image['parameter_base'] == elf.PROFILES[arch].parameter_base
    assert probe.arena[16:20] == bytes.fromhex('05000000')
    instructions = [device_cases.decoded(w.to_bytes(16, 'little').hex(), arch) for w in probe.code.words]
    assert any(d.klass.mnemonic == 'ULDC' and d.env['Sa_addr'] == launch.GLOBAL_DESCRIPTOR[arch] for d in instructions)
    c = Cases(arch)
    c.add('mov__RC', dict(Rd=8, Sb_bank=0, Sb_addr=0x200, PixMaskU04=15), {}, dict(R8=0), memory=[cbank(0, 0x200, 0)])
    assert device_cases.classify(c.encode_words()[0], arch=arch)[0] == ('emulator' if arch == 'SM90' else 'scalar')


@pytest.mark.parametrize('arch', ['SM89', 'SM90'])
def test_every_legacy_fixture_constructs_for_target(arch):
    fixtures = gpu_fixtures(FAMILIES, arch=arch)
    assert len(fixtures) > 1000
    assert all(cubin.kernel_image(f.image, f.kernel)['arch'] == arch for _, f in fixtures)
    for _, f in fixtures:
        code = gpu.Template(f.image, f.kernel).code
        ops = {gpu.mnemonic(gpu.Template(f.emulator_image or f.image, f.kernel).decoded(o)) for o in range(0, len(code), 16)}
        assert all(any(gpu.hits(op, target) for op in ops) for target in f.targets), f.name
        assert all(0 <= offset < len(code) and offset % 16 == 0 for offset in f.visited)
    carry = templates.carry_slots(arch)
    assert len(carry.sites['slots']) == 8
    assert carry.sites['slots'] == list(range(carry.sites['slots'][0], carry.sites['final'], 16))
    with pytest.raises(ValueError, match='exceeds'): templates.slot_image([0] * 9, arch=arch)


def test_evidence_migration_and_architecture_isolation(tmp_path, monkeypatch):
    path = tmp_path / 'passes.json'
    monkeypatch.setattr(device_evidence, 'PASSES', path)
    path.write_text(json.dumps(dict(version=2, families={'old': {'device': 'SM89'}})))
    assert device_evidence.passes()['architectures'] == {'SM89': {'old': {'device': 'SM89'}}}
    c = Cases('SM90')
    c.add('mov__RI', dict(Rd=8, Sb=19, PixMaskU04=15), {}, dict(R8=19))
    s = device_cases.Scenario('unit', c.encode_words()[0], 'scalar', arch='SM90')
    device_evidence.record([s], {s.id: []}, 0, 'H100', ['binary'], arch='SM90')
    records = device_evidence.passes()['architectures']
    assert records['SM89']['old'] == {'device': 'SM89'}
    record = records['SM90']['unit']
    found = device_evidence.summary([s], False)
    assert device_evidence.valid(record, found, 'SM90')
    assert not device_evidence.valid(record, found, 'SM89')
    device_evidence.record([s], {}, 0, 'H100', ['binary'], arch='SM90')
    assert device_evidence.passes()['architectures']['SM90'] == {}
    assert device_evidence.passes()['architectures']['SM89'] == records['SM89']
    monkeypatch.setattr(hardware, 'PASSES', path)
    path.write_text(json.dumps({'old': {'fingerprint': 'SM89'}}))
    assert hardware.passes()['architectures'] == {'SM89': {'old': {'fingerprint': 'SM89'}}}
