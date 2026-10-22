"""Shared single-instruction cases on SM89 and SM90, using native cubins rather than CUDA wrappers.

The target encoding is copied verbatim below bit 105. Wrapper instructions are encoded
from md classes; no semantics or reference implementation is used to construct a probe.
"""
import ctypes
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from functools import cache

from sass import launch
from sass.assemble import elf, source
from sass.decode import ir, pydecode
from sass.md import expression
from tests.cases import stream
from tests.harness.case import Case, word
from tests.harness.gpu import Driver, ENCODING, encode
from tests.refs.fpformat import same_fp8

BATCH_CASES = 64
BATCH_KERNELS = 32
# Wait 15 cycles and drain scoreboards before every instruction. Variable-latency
# writers claim scoreboard 0 and stores claim read scoreboard 1.
CONTROL = (15 << 105) | (63 << 116) | (7 << 110) | (7 << 113)
NAME = re.compile(r'(UR|UP|R|P)(\d+)$')
BACKEND_CHECKS = {'__native', '__max_ops', '__counter', '__events', '__opaque', '__fp8_nan', '__memory'}


@cache
def decoded(hexword, arch="SM89"):
    return pydecode.decode(arch, bytes.fromhex(hexword))


@cache
def resources(hexword, arch="SM89"):
    """Declared operand register cells, including guards and spans."""
    d = decoded(hexword, arch)
    files = {'GPR': 'R', 'UGPR': 'UR', 'PRED': 'P', 'UPRED': 'UP'}
    out = set()
    for operand in source.roles(arch)[d.klass.name]['operands']:
        prefix = files.get(operand['resource'])
        if prefix is None: continue
        n = d.env.get(operand['name'])
        if n is None: continue
        span = operand.get('span') or 1
        if isinstance(span, str): span = int(expression.evaluate(span, ir.load(arch), d.env))
        zero = ir.load(arch).enums.get(operand['type'], {}).get('URZ' if prefix == 'UR' else 'RZ' if prefix == 'R' else 'UPT' if prefix == 'UP' else 'PT', 63 if prefix == 'UR' else 255 if prefix == 'R' else 7)
        if n == zero: continue
        out.update(f'{prefix}{i}' for i in range(n, n + span))
    # Include guards even when a role file omits them.
    for a in d.klass.format:
        if a.kind == 'guard':
            n = d.env.get(a.name, 7)
            if n != 7: out.add(('UP' if a.type == 'UniformPredicate' else 'P') + str(n))
    return frozenset(out)


def constants(case):
    return tuple(sorted((m['address'], m['hex']) for m in case.request.get('memory', []) if m['space'] == 'cbank'))


def classify(case, klass=None, arch="SM89"):
    """Return a backend kind and reason; unfamiliar state fails rather than disappearing."""
    hw = case.hardware or {}
    if hw.get('skip'): return 'emulator', hw['skip']
    unknown = set(case.expected) & {k for k in case.expected if k.startswith('__')} - BACKEND_CHECKS
    if unknown: raise ValueError(f'{case.id}: unclassified expectations {unknown}')
    if 'error' in case.expected or '__opaque' in case.expected:
        return 'emulator', 'emulator error or opaque-fallback assertion'
    if 'kernel' in case.request:
        raise ValueError(f'{case.id}: kernel case requires an explicit hardware classification')
    extra = set(case.request) - {'word', 'registers', 'observe', 'inspect_pcode', 'memory', 'observe_memory', 'context', 'address'}
    if extra: raise ValueError(f'{case.id}: unclassified request fields {extra}')
    if hw.get('adapter'):
        if hw['adapter'] == 'memory':
            from tests.harness.device_setup import memory_reason
            if reason := memory_reason(case, arch): return 'emulator', reason
        return hw['adapter'], ''
    if case.request.get('context'): raise ValueError(f'{case.id}: context requires a hardware adapter')
    if '__counter' in case.expected or '__events' in case.expected:
        raise ValueError(f'{case.id}: control assertion requires a hardware adapter')
    names = set(case.request.get('registers', {})) | set(case.request.get('observe', []))
    if any(not NAME.fullmatch(n) and n not in ('RZ', 'URZ', 'PT', 'UPT') for n in names):
        raise ValueError(f'{case.id}: special registers require a hardware adapter')
    if any(m['space'] == 'cbank' and m['address'] >> 32 == 0 and m['address'] < elf.PROFILES[arch].parameter_base for m in case.request.get('memory', [])):
        return 'emulator', 'synthetic values in driver-owned launch constants'
    spaces = {m['space'] for m in case.request.get('memory', []) + case.request.get('observe_memory', [])}
    if spaces - {'cbank'}:
        if any(a.type == 'CX' for a in (klass or decoded(case.request['word'], arch).klass).operand_types.values()): return 'cx', ''
        raise ValueError(f'{case.id}: memory requires a hardware adapter')
    return 'scalar', ''


def selected_shard(job):
    """Collect only selected scenarios in a worker; never materialize a full language there."""
    arch, family = job[0], job[2]
    return [Scenario(family, case, *classify(case, arch=arch), arch=arch) for case in stream(*job)]


@cache
def selection(families, sample=0, arch="SM89"):
    from tests.harness import sharded
    # GPU collection used to generate an entire language serially. Reuse the
    # consumers' form shards, bounding the replicated md/plan caches to eight.
    return [scenario for part in sharded.run(selected_shard, ((arch, 'exact'),), families,
                                             sample, workers=min(8, os.cpu_count() or 1))
            for scenario in part]


@dataclass
class Scenario:
    family: str
    case: Case
    kind: str
    reason: str = ''
    arch: str = 'SM89'

    @property
    def id(self): return self.family + '/' + self.case.id


@cache
def instruction(cls, fields=(), arch="SM89"):
    return encode(cls, arch=arch, **dict(fields))


def op(cls, *, arch="SM89", **fields): return instruction(cls, tuple(sorted(fields.items())), arch)


@cache
def scheduled(value, arch="SM89"):
    d = pydecode.decode(arch, value.to_bytes(16, 'little'))
    controls = CONTROL
    for f, rule in d.klass.rules:
        if rule[0] == 'table' and f.name in ('src_rel_sb', 'dst_wr_sb'):
            # Only variable-latency encodings admit scoreboard assignment. In
            # particular constant-source ALU, FCHK and FP64 compares need it too.
            shift, board = (110, 0) if f.name == 'dst_wr_sb' else (113, 1)
            controls = (controls & ~(7 << shift)) | (board << shift)
    return (value & ENCODING) | controls


class Code:
    def __init__(self, arch="SM89"): self.arch = arch; self.words = []; self.targets = []
    def emit(self, cls, **fields): self.raw(op(cls, arch=self.arch, **fields))
    def raw(self, value): self.words.append(scheduled(value, self.arch))
    def mov(self, r, value):
        self.emit('umov__UI' if r.startswith('UR') else 'mov__RI',
                  **({'URd': int(r[2:]), 'Sb': value & 0xffffffff} if r.startswith('UR') else {'Rd': int(r[1:]), 'Sb': value & 0xffffffff, 'PixMaskU04': 15}))
    def copy(self, dest, src):
        if src.startswith('UR'):
            self.emit('mov__RU', Rd=dest, URb=int(src[2:]) if src != 'URZ' else 'URZ', PixMaskU04=15)
        else: self.emit('mov__RR', Rd=dest, Rb=int(src[1:]) if src != 'RZ' else 'RZ', PixMaskU04=15)
    def parameter(self, dest, offset): self.emit('mov__RC', Rd=dest, Sb_bank=0, Sb_addr=elf.PROFILES[self.arch].parameter_base + offset, PixMaskU04=15)


def scratch(case, count=8, arch="SM89"):
    used = resources(case.request['word'], arch) | set(case.request.get('registers', {})) | set(case.request.get('observe', [])) | {n for n in case.expected if NAME.fullmatch(n)}
    for base in range(16, 248, 2):
        if not any(f'R{i}' in used for i in range(base, base + count)): return base
    raise ValueError(f'{case.id}: no scratch register block')


def predicates(code, regs, temp):
    code.mov(f'R{temp}', sum((regs.get(f'P{i}', 0) & 1) << i for i in range(7)))
    code.emit('r2p__RIR', Ra=temp, Sb=0x7f, a_bsel='B0')
    for i in range(7):
        code.emit('uisetp__URURUR_URURUR', UPu=i, UPv='UPT', URa='URZ', URb='URZ', UPp='UPT',
                  icmp='EQ' if regs.get(f'UP{i}', 0) else 'NE', fmt='U32', bop='AND')


def initialize(code, case, temp):
    arch = code.arch
    regs = case.request.get('registers', {})
    for n in sorted(resources(case.request['word'], arch) | set(regs) | set(case.request.get('observe', []))):
        if NAME.fullmatch(n) and n.startswith(('R', 'UR')): code.mov(n, regs.get(n, 0))
    predicates(code, regs, temp)


def capture(code, case, temp, output, raw=False):
    """Capture before restoring UR4/5: the instruction may itself write those registers."""
    names = list(case.request.get('observe', [])) if raw else [n for n in case.expected if not n.startswith('__') and n != 'error']
    # Use scratch registers to save predicate packs and the uniform descriptor if observed.
    code.emit('p2r__RsIR_RIR', Rd=temp + 4, Pr='PR', Ra='RZ', Sb=0x7f, insert='B0')
    used_ur = {int(n[2:]) for n in names if n.startswith('UR') and n != 'URZ'}
    for i in (4, 5):
        if i in used_ur: code.copy(temp + 5 + i - 4, f'UR{i}')
    # Pack UP into UR scratch chosen outside the observed set, then immediately copy it.
    ur = next(i for i in range(63) if i not in used_ur and i not in (4, 5))
    code.emit('up2ur__Imm', URd=ur, UPR='UPR', URa='URZ', Sb=0x7f, insert='B0')
    code.copy(temp + 7, f'UR{ur}')
    code.parameter(temp, 64); code.parameter(temp + 1, 68)
    code.emit('uldc_const__RCR', URd=4, Sa_bank=0, Sa_addr=launch.GLOBAL_DESCRIPTOR[code.arch], sz='64')
    if not raw and '__counter' in case.expected: names.append('__counter')
    for n in names:
        if n == '__counter': code.copy(temp + 2, f'R{temp + 3}')
        elif n in ('PT', 'UPT'): code.mov(f'R{temp + 2}', 1)
        elif n in ('RZ', 'URZ'): code.mov(f'R{temp + 2}', 0)
        elif n.startswith('UP') or n.startswith('P'):
            packed = temp + 7 if n.startswith('UP') else temp + 4
            bit = int(n[2:] if n.startswith('UP') else n[1:])
            code.emit('shf__RuIR_RIR', Rd=temp + 2, Ra=packed, Sb=bit, Rc='RZ', dir='R', fmt='U32', hilo='LO')
            code.emit('lop3_lut__RuIR_RIR', Rd=temp + 2, Ra=temp + 2, Sb=1, Rc='RZ', imm8=0xc0, Pp='PT')
        elif n in ('UR4', 'UR5'): code.copy(temp + 2, f'R{temp + 5 + int(n[2:]) - 4}')
        else: code.copy(temp + 2, n)
        code.emit('stg__sImmOffset', Ra=temp, Ra_offset=output, Rb=temp + 2, sz='32', e='E')
        output += 4
    return names, output


@dataclass
class Probe:
    name: str
    scenarios: list
    code: Code
    constants: tuple
    observations: list
    output_size: int
    shared: int = 0
    threads: int = 1
    arena: bytes = b''
    frame_size: int = 0
    grid: int = 1
    raw: bool = False


def branches(s):
    """The landing PC is the result: the branch adapter, or a warp-context branch."""
    return s.kind == 'branch' or s.kind == 'warp' and decoded(s.case.request['word'], s.arch).klass.mnemonic == 'BRA'


def scalar_probe(scenarios, name, raw=False):
    from tests.harness import device_setup
    arch = scenarios[0].arch
    if any(s.arch != arch for s in scenarios): raise ValueError("mixed probe architectures")
    code, observations, output = Code(arch), [], 0
    arena = bytearray(); shared = frame = 0
    for s in scenarios:
        case = s.case; temp = scratch(case, arch=arch)
        if s.kind == 'ctaid':
            code.emit('s2r_', Rd=temp + 4, SRa='SR_CTAID.X')
            code.emit('isetp__RsIR_RIR_noEX', Pu=6, Pv='PT', Ra=temp + 4, Sb=case.request['registers']['SR_CTAID.X'], Pp='PT', icmp='EQ', fmt='U32', bop='AND')
            code.emit('exit_', Pg=6, **{'Pg@not': 1})
        initialize(code, case, temp)
        join = None
        if s.kind == 'cx': device_setup.cx(code, case, temp, arena)
        if s.kind == 'warp': join = device_setup.warp(code, case, temp, arena)
        if s.kind == 'qspc':
            device_setup.qspc(code, case, temp, arena)
            shared, frame = max(shared, 256), max(frame, 256)
        if s.kind == 'memory':
            smem, local, base, start = device_setup.memory(code, case, temp, arena)
            shared, frame = max(shared, smem), max(frame, local)
        code.targets.append((len(code.words) * 16, case.request['word']))
        code.raw(int.from_bytes(bytes.fromhex(case.request['word']), 'little'))
        if branches(s):
            displacement = decoded(case.request['word'], arch).env['sImm']
            if displacement != 32: raise ValueError('branch adapter currently requires the 32-byte branch scenario')
            code.mov(f'R{temp + 3}', case.request.get('address', 0x1000) + 16)
            code.emit('bra_', sImm=16)
            code.mov(f'R{temp + 3}', case.request.get('address', 0x1000) + 48)
        if s.kind in ('warp', 'lane'): device_setup.lane_guard(code, temp, case.request['context']['warp']['lane'] if s.kind == 'warp' else case.request['registers']['SR_LANEID'])
        names, end = capture(code, case, temp, output, raw=raw)
        if s.kind == 'memory' and case.request.get('observe_memory'):
            end, memory_names = device_setup.capture_memory(code, case, temp, end, base, start)
            names += memory_names
        if join: join()
        observations.append((output, names)); output = end
    code.emit('exit_')
    return Probe(name, scenarios, code, constants(scenarios[0].case), observations, output, shared=shared, arena=bytes(arena), frame_size=frame, threads=32 if scenarios[0].kind in ('warp', 'lane') else 1,
                 grid=scenarios[0].case.request['registers']['SR_CTAID.X'] + 1 if scenarios[0].kind == 'ctaid' else 1, raw=raw)


def kernels(scenarios, raw=False):
    groups = defaultdict(list)
    for s in scenarios:
        if s.kind == 'emulator': continue
        groups[(s.arch, s.kind, constants(s.case))].append(s)
    out = []
    for (_, kind, _), group in groups.items():
        if kind not in ('scalar', 'cx', 'memory', 'warp', 'branch', 'qspc', 'ctaid', 'lane'): raise ValueError(f'GPU adapter {kind!r} is not implemented')
        size = 1 if kind in ('warp', 'lane', 'ctaid') else BATCH_CASES
        for start in range(0, len(group), size):
            out.append(scalar_probe(group[start:start + size], f'probe_{len(out)}', raw=raw))
    return out


def batches(probes):
    groups = defaultdict(list)
    for p in probes:
        groups[tuple((a, h) for a, h in p.constants if a >> 32 not in (0, 2))].append(p)
    return [group[i:i + BATCH_KERNELS] for group in groups.values() for i in range(0, len(group), BATCH_KERNELS)]


def image(probes):
    arch = probes[0].code.arch
    if any(p.code.arch != arch for p in probes): raise ValueError("mixed cubin architectures")
    module = source.Module(arch, [])
    for p in probes:
        k = source.Kernel(p.name, registers=255, uniform_registers=63, shared=p.shared, frame_size=p.frame_size)
        k.parameters = [source.Parameter('constants', 64, 1, 0), source.Parameter('output', 8, 8, 64), source.Parameter('arena', 8, 8, 72)]
        banks = defaultdict(bytearray)
        for address, hexdata in p.constants:
            bank, offset = address >> 32, address & 0xffffffff
            if not bank: continue
            data = bytes.fromhex(hexdata); buf = banks[bank]
            if len(buf) < offset + len(data): buf.extend(bytes(offset + len(data) - len(buf)))
            buf[offset:offset + len(data)] = data
        k.constants = dict(banks)
        k.code = b''.join(w.to_bytes(16, 'little') for w in p.code.words)
        k.instructions = [(d.klass, d.env, {}, source.Statement(d.text, i + 1, 1))
                          for i, w in enumerate(p.code.words) for d in [pydecode.decode(arch, w.to_bytes(16, 'little'), i * 16)]]
        source.validate_resources(arch, k)
        k.code += op('nop_', arch=arch).to_bytes(16, 'little') * ((-len(k.code) % 256) // 16)
        module.kernels.append(k)
    return elf.link(module)


def nan_lanes(case, arch="SM89"):
    """The existing GPU policy: the F16 lanes (bit offsets) of an output where F16 arithmetic/packing
    permit NaN payload/sign variation.

    A MERGE_C's preserved half is still compared exactly. Other formats and
    non-floating outputs remain bit-exact.
    """
    d = decoded(case.request['word'], arch)
    if d.klass.mnemonic in ('HADD2', 'HMUL2', 'HFMA2'):
        if any(a.name == 'ofmt' and ir.load(arch).rev_enums[a.type].get(d.env[a.name]) != 'F16_V2' for a in d.klass.operand_types.values()): return ()
        return 0, 16
    if d.klass.mnemonic == 'F2FP' and 'dstfmt' in d.env:
        a = d.klass.operand_types['dstfmt']
        if ir.load(arch).rev_enums[a.type].get(d.env['dstfmt']) != 'F16': return ()
        merge = d.klass.operand_types.get('merge')
        mode = ir.load(arch).rev_enums[merge.type].get(d.env['merge']) if merge else ''
        return (0,) if mode == 'MERGE_C' else (0, 16)
    return ()


def half_nan_equal(case, got, want, arch="SM89"):
    from tests.refs.bits import nan16
    lanes = nan_lanes(case, arch)
    if not lanes: return False
    for shift in (0, 16):
        g, w = (got >> shift) & 0xffff, (want >> shift) & 0xffff
        if g != w and not (shift in lanes and nan16(g) and nan16(w)): return False
    return True


def compare(case, names, values, arch="SM89"):
    expected = (case.hardware or {}).get('expected', case.expected)
    out = []
    for n, value in zip(names, values):
        if n.startswith('__memory:'):
            _, index, offset, size = n.split(':')
            if int(index): continue  # __memory asserts the first observed region
            want = int.from_bytes(bytes.fromhex(expected['__memory'])[int(offset):int(offset) + int(size)], 'little')
            value &= (1 << (8 * int(size))) - 1
        else: want = expected[n]
        same = same_fp8(value, want, expected['__fp8_nan']) if '__fp8_nan' in expected and not n.startswith('__memory:') else value == want
        if not same and n.startswith(('R', 'UR')) and not n.startswith('__memory:'): same = half_nan_equal(case, value, want, arch)
        if not same: out.append(f'{n}: GPU {value:#x}, reference {want:#x}')
    return out


def execute(driver, probes, binary=None):
    arch = probes[0].code.arch
    driver.require_arch(arch)
    binary = image(probes) if binary is None else binary
    module = ctypes.c_void_p(); output = ctypes.c_uint64()
    driver.call('cuModuleLoadData', ctypes.byref(module), ctypes.c_char_p(binary))
    results = {}
    try:
        driver.call('cuMemAlloc_v2', ctypes.byref(output), ctypes.c_size_t(max(p.output_size for p in probes) or 4))
        for p in probes:
            fn = ctypes.c_void_p()
            driver.call('cuModuleGetFunction', ctypes.byref(fn), module, p.name.encode())
            constant_data = bytearray(64)
            for address, hexdata in p.constants:
                if address >> 32 == 0:
                    offset = address - elf.PROFILES[arch].parameter_base
                    data = bytes.fromhex(hexdata)
                    if offset < 0 or offset + len(data) > 64: raise ValueError('reserved launch constants cannot be seeded')
                    constant_data[offset:offset + len(data)] = data
            params = ctypes.create_string_buffer(bytes(constant_data)); arena = ctypes.c_uint64()
            driver.call('cuMemAlloc_v2', ctypes.byref(arena), ctypes.c_size_t(len(p.arena) or 16))
            if p.arena: driver.call('cuMemcpyHtoD_v2', arena, ctypes.create_string_buffer(p.arena), ctypes.c_size_t(len(p.arena)))
            args = (ctypes.c_void_p * 3)(ctypes.addressof(params), ctypes.addressof(output), ctypes.addressof(arena))
            driver.call('cuMemsetD8_v2', output, 0xcc, ctypes.c_size_t(p.output_size))
            driver.call('cuLaunchKernel', fn, p.grid, 1, 1, p.threads, 1, 1, 0, None, args, None)
            driver.call('cuCtxSynchronize')
            buffer = ctypes.create_string_buffer(p.output_size)
            driver.call('cuMemcpyDtoH_v2', buffer, output, ctypes.c_size_t(p.output_size))
            for s, (offset, names) in zip(p.scenarios, p.observations):
                values = [int.from_bytes(buffer.raw[i:i + 4], 'little') for i in range(offset, offset + len(names) * 4, 4)]
                results[s.id] = observation(s.case, names, values) if p.raw else compare(s.case, names, values, arch)
            driver.call('cuMemFree_v2', arena)
    finally:
        failed = sys.exception() is not None
        try:
            if output.value: driver.call('cuMemFree_v2', output)
            driver.call('cuModuleUnload', module)
        except RuntimeError:
            if not failed: raise
    return results


def isolated(probes, timeout=30):
    """Execute a batch in a disposable process; bisect faults down to individual cases."""
    import pickle
    import subprocess
    import sys
    try:
        process = subprocess.run([sys.executable, '-m', 'tests.harness.device_runner'], input=pickle.dumps(probes),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        response = pickle.loads(process.stdout) if process.returncode == 0 else dict(error=process.stderr.decode(errors='replace'))
    except subprocess.TimeoutExpired:
        response = dict(error=f'GPU worker exceeded {timeout}s')
    if 'results' in response: return response
    if len(probes) > 1:
        groups = (probes[:len(probes) // 2], probes[len(probes) // 2:])
    elif len(probes[0].scenarios) > 1:
        ss = probes[0].scenarios; mid = len(ss) // 2
        groups = (kernels(ss[:mid], raw=probes[0].raw), kernels(ss[mid:], raw=probes[0].raw))
    else:
        return dict(results={probes[0].scenarios[0].id: [response['error']]}, device=None)
    result, device, binaries = {}, None, []
    for group in groups:
        part = isolated(group, timeout)
        result.update(part['results']); device = part['device'] or device; binaries += part.get('kernels', [])
    return dict(results=result, device=device, kernels=binaries)


def observation(case, names, values):
    """Decode captures without consulting expected values or comparison tolerances."""
    memory = [bytearray(m['size']) for m in case.request.get('observe_memory', [])]
    registers = {}
    for name, value in zip(names, values):
        if name.startswith('__memory:'):
            _, index, offset, size = name.split(':')
            memory[int(index)][int(offset):int(offset) + int(size)] = value.to_bytes(4, 'little')[:int(size)]
        else: registers[name] = value
    return dict(registers=registers,
                memory=[dict(m, hex=bytes(data).hex()) for m, data in zip(case.request.get('observe_memory', []), memory)])


def observe(cases, timeout=30, arch=None):
    """Run hardware probes without a reference; return raw observations and device identity.

    Uses the same setup, scheduling, capture and fault isolation as regression
    tests. Results never update hardware-pass records or verification coverage.
    """
    arch = arch or cases.arch
    driver = Driver(arch)
    try: device = driver.device
    finally: driver.close()
    scenarios = []
    for case in cases:
        # A known regression can be re-observed with its original request; its
        # expected values must have no influence on execution or returned data.
        case = case._replace(expected={})
        kind, reason = classify(case, arch=arch)
        if kind == 'emulator': raise ValueError(f'{case.id}: cannot probe emulator-only state: {reason}')
        if not case.request.get('observe') and not case.request.get('observe_memory'):
            raise ValueError(f'{case.id}: a probe needs explicit observations')
        scenarios.append(Scenario('probe', case, kind, arch=arch))
    ids = [s.id for s in scenarios]
    if len(set(ids)) != len(ids): raise ValueError('duplicate probe requests')
    results = {}
    for batch in batches(kernels(scenarios, raw=True)):
        response = isolated(batch, timeout)
        results.update(response['results'])
    return dict(arch=arch, device=device, observations=[dict(id=s.case.id, word=s.case.request['word'],
                instruction=decoded(s.case.request['word'], s.arch).text,
                **(results[s.id] if isinstance(results[s.id], dict) else dict(error='\n'.join(results[s.id]))))
                for s in scenarios])
