"""Text-to-code assembly, native executable metadata, and independent GPU execution."""
import ctypes
from dataclasses import replace
import struct
import subprocess
import sys

import pytest

from sass import oracle, synth
from sass.assemble import AssemblyError, assemble_cubin, assemble_instruction, assemble_raw, encoding, elf, instruction, source
from sass.decode import cubin, ir, pydecode
from sass.paths import NVDISASM
from tests.harness.select import arches


def pytest_generate_tests(metafunc):
    if 'assembler_arch' in metafunc.fixturenames:
        metafunc.parametrize('assembler_arch', arches(metafunc.config))


def decoded(text, arch='SM89', addr=0):
    return pydecode.decode(arch, assemble_instruction(arch, text, addr), addr)


@pytest.mark.parametrize('text,expected', [
    ('NOP', 'NOP'),
    ('@!P0 MOV R2, R3;', '@!P0 MOV R2, R3'),
    ('MOV R2, 0xffffffff', 'MOV R2, 0xffffffff'),
    ('MOV R2, c[0][0x160]', 'MOV R2, c[0x0][0x160]'),
    ('IADD3 R0, R1, -0x80000000, RZ', 'IADD3 R0, R1, -0x80000000, RZ'),
    ('FADD R0, -|R1|, R2', 'FADD R0, -|R1|, R2'),
    ('FADD R0, R2, -|c[0][0x160]|', 'FADD R0, R2, -|c[0x0][0x160]|'),
    ('FADD R0, R1, -0.0', 'FADD R0, R1, -0.0'),
    ('FADD R0, R1, +INF', 'FADD R0, R1, +INF'),
    ('DADD R0, R2, +SNAN', 'DADD R0, R2, +SNAN'),
    ('IMAD.MOV.U32 R1, RZ, RZ, c[0][0x28]', 'IMAD.MOV.U32 R1, RZ, RZ, c[0x0][0x28]'),
    ('LDG.E R2, [R4.64]', 'LDG.E R2, [R4.64]'),
    ('STG.E [R4.64], R2', 'STG.E [R4.64], R2'),
    ('MOV R0, R1, 0xf', 'MOV R0, R1'),
])
def test_instruction_forms(text, expected):
    assert decoded(text).text.strip() == expected


def test_explicit_controls_and_reuse():
    d = decoded('FADD R0, R1.reuse, R2 @sched(usched_info=W1, req_bit_set=3)')
    assert d.env['usched_info'] == 17
    assert d.env['req_bit_set'] == 3
    assert '.reuse' in d.text
    d = decoded('LDG.E R2, [R4.64] @sched(dst_wr_sb=0, src_rel_sb=1)')
    assert d.env['dst_wr_sb'] == 0 and d.env['src_rel_sb'] == 1


def test_known_encoding_and_whitespace():
    # The independently disassembled MOV word used in the README.
    assert assemble_instruction('SM89', 'MOV R5, 4 @sched(usched_info=W1)').hex() == '0278050004000000000f000000e20f00'
    assert decoded('  MOV R2 , c [ 0 ] [ 0x160 ] ;').text == 'MOV R2, c[0x0][0x160]'


def test_unencoded_format_operand_cannot_change_value():
    text = 'TXQ R40, RZ, RZ, TEX_HEADER_TEXTURE_TYPE, UR48, 0x0, 0xe'
    assert decoded(text, 'SM100').text == text
    with pytest.raises(AssemblyError, match='tid8: operand is not encoded'):
        assemble_instruction('SM100', text.replace('0x0,', '0x1,'))


@pytest.mark.parametrize('text,reason', [
    ('MOV R0, 0x100000000', 'outside'),
    ('IADD3 R0, R1, 0x80000000, RZ', 'outside'),
    ('MOV R0, c[0][3]', 'aligned'),
    ('BRA 0x14', 'aligned'),
    ('MOV R999, R0', 'syntax'),
    ('MOV R0 R1', 'syntax'),
    ('MOV R0, R1, R2', 'syntax'),
    ('MOV R0, R1 @sched(typo=1)', 'unknown scheduling'),
    ('NOP @sched(req_bit_set=64)', 'outside'),
    ('NOP @sched(usched_info=TYPO)', 'expected a USCHED_INFO'),
    ('NOP @sched(req_bit_set=1, req_bit_set=2)', 'duplicate'),
    ('FADD R0, R1.reuse, R2', 'no exact row'),
    ('DADD R0, R2, 1.0000000000000002', 'high 32 bits'),
    ('FADD R0, R1, 1e1000', 'not representable'),
    ('UNKNOWN R0', 'unknown opcode'),
])
def test_instruction_errors(text, reason):
    with pytest.raises(AssemblyError, match=reason):
        assemble_instruction('SM89', text)


def test_labels_addresses_and_listings():
    text = '''.target SM89
.schedule usched_info=WAIT15_END_GROUP
start: /*0000*/ MOV R0, 1; /* discarded encoding */
@P0 BRA finish;
BRA `(start);
finish: EXIT;
'''
    data = assemble_raw('SM89', text, base=0x100)
    assert pydecode.decode('SM89', data[16:32], 0x110).text == '@P0 BRA 0x130'
    assert pydecode.decode('SM89', data[32:48], 0x120).text == 'BRA 0x100'
    assert pydecode.decode('SM89', data[:16]).env['usched_info'] == 15
    assert decoded('BRA -0x10.REL').env['sImm'] == (1 << 50) - 16


@pytest.mark.parametrize('text,reason', [
    ('a: NOP; a: EXIT;', 'duplicate symbol'),
    ('BRA $missing;', 'undefined symbol'),
    ('NOP', 'semicolon'),
    ('/* unfinished', 'unterminated'),
    ('.target SM90\nNOP;', 'conflicting targets'),
    ('.typo x\nNOP;', 'unknown directive'),
])
def test_source_errors(text, reason):
    with pytest.raises(AssemblyError, match=reason):
        assemble_raw('SM89', text)


def test_source_location():
    with pytest.raises(AssemblyError, match=r'example.sass:3:3:.*undefined symbol'):
        source.raw('SM89', '// comment\nNOP;\n  BRA $absent;', filename='example.sass')


def test_invalid_targets_have_assembly_diagnostics():
    with pytest.raises(AssemblyError, match='unknown machine-description target'):
        assemble_raw('SM999', 'NOP;')
    with pytest.raises(AssemblyError, match=r'<string>:2:1:.*unknown machine-description target'):
        assemble_raw(None, '\n.target SM999\nNOP;')


def test_labels_do_not_replace_opcodes_or_registers():
    data = assemble_raw('SM89', 'MOV: MOV R0, 1; R0: BRA MOV; BRA R0;')
    assert pydecode.decode('SM89', data[:16]).text == 'MOV R0, 0x1'
    assert pydecode.decode('SM89', data[16:32], 16).text == 'BRA 0x0'
    assert pydecode.decode('SM89', data[32:48], 32).text == 'BRA 0x10'


def test_exact_tables_and_shared_fields():
    md = ir.load('SM89')
    k = next(c for c in md.classes if c.mnemonic == 'MOV' and c.name == 'mov__RR')
    with pytest.raises(ValueError, match='outside'):
        encoding.encode_class(md, k, {'Rd': 256, 'Ra': 0})
    f = ir.Field('shared', ((7, 0),))
    k = replace(k, rules=[(f, ('table', 'IDENTICAL', (('op', 'a', 1, None), ('op', 'b', 1, None))))])
    with pytest.raises(ValueError, match='IDENTICAL'):
        encoding.encode_class(md, k, {'a': 1, 'b': 2})


def test_machine_description_roundtrips(assembler_arch):
    """Every renderable legal sampled form must reassemble, with its explicit controls.

    Random raw words also contain invalid enum values and noninstruction branch addresses;
    those are decoder inputs, not valid assembler source. Their exclusion is explicit here.
    """
    md = ir.load(assembler_arch)
    checked, failures = 0, []
    for word in synth.synthetic(assembler_arch, 1, legal=True):
        try:
            d = pydecode.decode(assembler_arch, word)
        except pydecode.NoMatch:
            continue
        if '???' in d.text or not pydecode.sched_valid(md, d.klass, d.env):
            continue
        if any(a.type == 'RSImm' and (16 + pydecode.sext(d.env[a.name], d.widths[a.name])) % 16
               for a in d.klass.operand_types.values()):
            continue
        controls = {a.name: md.rev_enums[a.type][d.env[a.name]] if a.type in md.enums else str(d.env[a.name])
                    for a in encoding.scheduling(assembler_arch, d.klass.name) if a.name in d.env}
        try:
            data = instruction.assemble(assembler_arch, d.text, schedule=controls)[0]
            got = pydecode.decode(assembler_arch, data)
            assert instruction.normalize(got.text) == instruction.normalize(d.text)
            for name in controls:
                assert got.env[name] == d.env[name]
            checked += 1
        except (ValueError, pydecode.NoMatch, AssertionError) as exc:
            failures.append(f'{d.klass.name}: {d.text}: {exc}')
    assert not failures, '\n'.join(failures)
    assert checked > 900


def test_instruction_oracle(assembler_arch):
    texts = ['NOP', 'MOV R0, 1', 'IMAD R0, R1, R2, R3', 'FADD R0, R1, R2', 'EXIT']
    words = [assemble_instruction(assembler_arch, t) for t in texts]
    for i, result in oracle.disasm_batch(assembler_arch, words).items():
        assert result.error is None, result.error
        assert instruction.normalize(result.text) == instruction.normalize(pydecode.decode(assembler_arch, words[i], result.addr).text)


SIMPLE = '''.target SM89
.kernel write_value
.param output, 8, 8
.param value, 4, 4
.registers 8, 8
.schedule usched_info=WAIT15_END_GROUP, req_bit_set=63
MOV R1, c[0][0x28];
MOV R2, c[0][$output];
MOV R3, c[0][$output+4];
MOV R5, c[0][$value];
ULDC.64 UR4, c[0][0x118];
STG.E [R2.64], R5;
EXIT;
.endkernel
'''


def test_native_cubin_metadata(assembler_arch, tmp_path):
    text = f'''.target {assembler_arch}
.kernel write_value
.param output, 8, 8
.param value, 4, 4
.registers 4
LDC R0, c[0][$value];
EXIT;
.endkernel
'''
    if assembler_arch not in elf.PROFILES:
        with pytest.raises(AssemblyError, match='ABI is not established'):
            assemble_cubin(text)
        return
    data = assemble_cubin(text)
    assert data == assemble_cubin(text)
    image = cubin.kernel_image(data, 'write_value')
    assert image['arch'] == assembler_arch
    assert image['parameter_base'] == elf.PROFILES[assembler_arch].parameter_base
    assert image['parameters'] == [dict(offset=0, size=8), dict(offset=8, size=4)]
    assert len(image['code']) == 256
    path = tmp_path / 'native.cubin'; path.write_bytes(data)
    result = subprocess.run([NVDISASM, str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'write_value' in result.stdout and 'LDC' in result.stdout


def test_parameter_padding_and_multiple_kernels():
    text = '''.target SM89
.kernel first
.param a, 1, 1
.param b, 8, 8
.registers 4
.shared 16
.constant 2, "78563412"
EXIT;
.endkernel
.kernel second
.registers 4
EXIT;
.endkernel
'''
    data = assemble_cubin(text)
    image = cubin.kernel_image(data, 'first')
    assert image['parameters'] == [dict(offset=0, size=1), dict(offset=8, size=8)]
    assert image['constants'][2] == bytes.fromhex('78563412')
    assert set(cubin.text_sections(data)) == {'.text.first', '.text.second'}
    assert cubin.kernel_image(data, 'second')['parameters'] == []


@pytest.mark.parametrize('replacement,reason', [
    (('.registers 8, 8', ''), '.registers is required'),
    (('.registers 8, 8', '.registers 3, 8'), 'exceeding'),
    (('.registers 8, 8', '.registers 8'), 'exceeding'),
    (('.param value, 4, 4', '.param output, 4, 4'), 'duplicate parameter'),
    (('.param value, 4, 4', '.param value, 4, 3'), 'power of two'),
    (('.endkernel', ''), 'missing .endkernel'),
])
def test_kernel_errors(replacement, reason):
    with pytest.raises(AssemblyError, match=reason):
        assemble_cubin(SIMPLE.replace(*replacement))


def test_wide_register_span_validation():
    text = '.target SM89\n.kernel k\n.registers 3\nDADD R2, RZ, RZ;\nEXIT;\n.endkernel'
    with pytest.raises(AssemblyError, match='exceeding declared GPR count 3'):
        assemble_cubin(text)


def test_cta_and_barrier_metadata():
    text = '.target SM89\n.kernel k\n.registers 4\nS2R R0, SR_CTAID.Z;\nBAR.SYNC 3;\nEXIT;\n.endkernel'
    data = assemble_cubin(text)
    records = dict(cubin.info_records(cubin.sections(data)['.nv.info.k']))
    assert records[4] == bytes(2)  # EIATTR_CTAIDZ_USED
    assert records[0x1d] == struct.pack('<I', 0)
    assert records[0x4c] == struct.pack('<H', 4)


def test_cli(tmp_path):
    input = tmp_path / 'input.sass'; output = tmp_path / 'output.cubin'
    input.write_text(SIMPLE)
    result = subprocess.run([sys.executable, '-m', 'sass.assemble', str(input), '-o', str(output)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == assemble_cubin(SIMPLE)
    output.write_bytes(b'keep on failure')
    input.write_text('.target SM89\nNOP')
    result = subprocess.run([sys.executable, '-m', 'sass.assemble', str(input), '-o', str(output), '--format', 'raw'], capture_output=True, text=True)
    assert result.returncode == 1 and 'semicolon' in result.stderr
    assert output.read_bytes() == b'keep on failure'


def test_module_constant_banks():
    kernel = '.target SM89\n.kernel first\n.registers 8\n.constant 3, "78563412"\nMOV R0, c[3][0]; EXIT;\n.endkernel\n'
    other = kernel.replace('first', 'second')
    image = assemble_cubin(kernel + other)
    assert cubin.kernel_image(image, 'first')['constants'][3] == bytes.fromhex('78563412')
    assert cubin.kernel_image(image, 'second')['constants'][3] == bytes.fromhex('78563412')
    with pytest.raises(AssemblyError, match='module-wide'):
        assemble_cubin(kernel + other.replace('78563412', '01000000'))


@pytest.mark.gpu
def test_native_cubin_on_gpu(request):
    """Execute native entry points without compiling or patching any cubin template."""
    from tests.harness import gpu
    if gpu.target(request.config) is None: pytest.skip(request.config._gpu_unavailable)
    try:
        driver = gpu.Driver(gpu.target(request.config))
    except (OSError, RuntimeError) as exc:
        if request.config.getoption('--require-gpu'):
            raise
        pytest.skip(str(exc))
    loop = SIMPLE.replace('write_value', 'loop_value').replace('STG.E [R2.64], R5;', '''
MOV R6, 3;
again:
IADD3 R5, R5, 1, RZ;
IADD3 R6, R6, -1, RZ;
ISETP.NE.U32.AND P0, PT, R6, 0, PT;
@P0 BRA again;
STG.E [R2.64], R5;''')
    constant = SIMPLE.replace('write_value', 'constant_value').replace('.registers 8, 8', '.registers 8, 8\n.constant 2, "78563412"').replace('MOV R5, c[0][$value];', 'MOV R5, c[2][0];')
    shared = SIMPLE.replace('write_value', 'shared_value').replace('.registers 8, 8', '.registers 8, 8\n.shared 4').replace('STG.E [R2.64], R5;', '''
STS [RZ], R5;
BAR.SYNC 0;
LDS R6, [RZ] @sched(dst_wr_sb=0);
STG.E [R2.64], R6;''')
    dynamic = shared.replace('shared_value', 'dynamic_value').replace('.shared 4', '')
    cta = SIMPLE.replace('write_value', 'cta_value').replace('MOV R5, c[0][$value];', '''
S2R R5, SR_CTAID.Z @sched(dst_wr_sb=0);
IMAD.WIDE.U32 R2, R5, 4, R2;''')
    empty = '.kernel empty\n.registers 4\nEXIT;\n.endkernel\n'
    module_constant = constant.replace('constant_value', 'module_constant_value').replace('.constant 2,', '.constant 3,').replace('c[2][0]', 'c[3][0]')
    from sass import launch
    text = (SIMPLE + loop + constant + module_constant + shared + dynamic + cta + empty).replace('0x118', hex(launch.GLOBAL_DESCRIPTOR[driver.arch]))
    if driver.arch == "SM90": text = text.replace(".64]", "]")
    data = assemble_cubin(text.replace(".target SM89", ".target " + driver.arch))
    module = ctypes.c_void_p(); pointer = ctypes.c_uint64()
    try:
        driver.call('cuModuleLoadData', ctypes.byref(module), ctypes.c_char_p(data))
        driver.call('cuMemAlloc_v2', ctypes.byref(pointer), ctypes.c_size_t(12))
        for name, expected, dynamic_bytes in [('write_value', 19, 0), ('loop_value', 22, 0),
                                               ('constant_value', 0x12345678, 0), ('module_constant_value', 0x12345678, 0), ('shared_value', 19, 0),
                                               ('dynamic_value', 19, 4)]:
            fn = ctypes.c_void_p(); value = ctypes.c_uint32(19)
            driver.call('cuModuleGetFunction', ctypes.byref(fn), module, name.encode())
            args = (ctypes.c_void_p * 2)(ctypes.addressof(pointer), ctypes.addressof(value))
            driver.call('cuLaunchKernel', fn, 1, 1, 1, 32, 1, 1, dynamic_bytes, None, args, None)
            driver.call('cuCtxSynchronize')
            result = ctypes.c_uint32()
            driver.call('cuMemcpyDtoH_v2', ctypes.byref(result), pointer, ctypes.c_size_t(4))
            assert result.value == expected, name
        fn = ctypes.c_void_p(); value = ctypes.c_uint32(19)
        driver.call('cuModuleGetFunction', ctypes.byref(fn), module, b'cta_value')
        args = (ctypes.c_void_p * 2)(ctypes.addressof(pointer), ctypes.addressof(value))
        driver.call('cuLaunchKernel', fn, 1, 1, 3, 32, 1, 1, 0, None, args, None)
        driver.call('cuCtxSynchronize')
        result = (ctypes.c_uint32 * 3)()
        driver.call('cuMemcpyDtoH_v2', result, pointer, ctypes.c_size_t(12))
        assert list(result) == [0, 1, 2]
        fn = ctypes.c_void_p()
        driver.call('cuModuleGetFunction', ctypes.byref(fn), module, b'empty')
        driver.call('cuLaunchKernel', fn, 1, 1, 1, 1, 1, 1, 0, None, None, None)
        driver.call('cuCtxSynchronize')
    finally:
        if pointer.value:
            driver.call('cuMemFree_v2', pointer)
        if module.value:
            driver.call('cuModuleUnload', module)
        driver.close()


@pytest.mark.parametrize('permissive', [False, True])
def test_starred_literals_are_written(permissive):
    word = encoding.word('SM89', 'imad__RRR_RRR', permissive=permissive)
    assert word >> 81 & 7 == 7


def test_permissive_truncates_where_strict_raises():
    with pytest.raises(ValueError, match='outside'):
        encoding.encode('SM89', 'mov__RR', Rd=256)
    assert encoding.encode('SM89', 'mov__RR', Rd=256, permissive=True) == encoding.encode('SM89', 'mov__RR', Rd=0, permissive=True)
