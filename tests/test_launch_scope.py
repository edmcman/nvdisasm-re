import pytest
from sass import launch
from sass.assemble import assemble_cubin
from sass.decode import cubin
from sass.emulate import kernel, reference


def request(arch, scope='launch', order='round_robin'):
    image = cubin.kernel_image(assemble_cubin(f'''.target {arch}
.kernel publish
.registers 8, 0
S2R R0, SR_CTAID.X;
ISETP.EQ.U32.AND P0, PT, R0, RZ, PT;
MOV R2, 0x200000;
MOV R3, 7;
@!P0 STG [R2], R3;
@P0 LDG R4, [R2];
EXIT;
.endkernel
'''), 'publish')
    return launch.kernel_request(image, (), [], 2, 1, dict(address=0x200000, stride=0), scope=scope, schedule=order)


@pytest.mark.parametrize('arch', ['SM89', 'SM90'])
@pytest.mark.parametrize('order', ['round_robin', 'reverse'])
def test_launch_global(arch, order):
    r = request(arch, order=order)
    expected = reference.run_kernel(r, arch)
    assert expected.get('outputs') == ['07000000'] * 2, expected
    assert reference.run_kernel(r, arch) == expected
    assert kernel.compiled(*kernel.key(r, arch, 'exact')).run(r, workers=1) == expected
    assert kernel.compiled(*kernel.key(r, arch, 'exact')).run(r, workers=4) == expected


def test_isolated_blocks():
    r = request('SM89', scope='block')
    expected = reference.run_kernel(r, 'SM89')
    assert expected.get('outputs') == ['00000000', '07000000'], expected
    assert kernel.run_kernel(r, 'SM89') == expected


@pytest.mark.parametrize('options', [dict(cooperative=True), dict(scope='bad'), dict(schedule='bad')])
def test_invalid_scope(options):
    r = request('SM89')
    r['kernel'].update(options)
    assert 'error' in reference.run_kernel(r, 'SM89')
    assert kernel.run_kernel(r, 'SM89') == reference.run_kernel(r, 'SM89')


@pytest.mark.parametrize('order', ['round_robin', 'reverse'])
def test_polling_consumer(order):
    image = cubin.kernel_image(assemble_cubin('''.target SM89
.kernel communicate
.registers 8, 0
S2R R0, SR_CTAID.X;
ISETP.EQ.U32.AND P0, PT, R0, RZ, PT;
MOV R2, 0x200000;
MOV R3, 7;
@!P0 BRA producer;
poll:
LDG R4, [R2];
ISETP.EQ.U32.AND P1, PT, R4, RZ, PT;
@P1 BRA poll;
EXIT;
producer:
STG [R2], R3;
EXIT;
.endkernel
'''), 'communicate')
    r = launch.kernel_request(image, (), [], 2, 1, dict(address=0x200000, stride=0), schedule=order)
    expected = reference.run_kernel(r, 'SM89')
    assert expected.get('outputs') == ['07000000'] * 2, expected
    assert kernel.run_kernel(r, 'SM89') == expected
    r['kernel']['max_steps'] = 1
    assert 'instruction limit' in reference.run_kernel(r, 'SM89')['error']
    assert kernel.run_kernel(r, 'SM89') == reference.run_kernel(r, 'SM89')


@pytest.mark.parametrize('space,block,want', [('S', 1, ['00000000', '07000000']), ('S', 2, ['07000000'] * 2), ('L', 2, ['00000000', '07000000'])])
def test_storage_isolation(space, block, want):
    image = cubin.kernel_image(assemble_cubin(f'''.target SM89
.kernel isolation
.registers 8, 0
S2R R0, SR_{'CTAID' if block == 1 else 'TID'}.X;
ISETP.EQ.U32.AND P0, PT, R0, RZ, PT;
MOV R2, 0x200000;
MOV R3, 7;
@!P0 ST{space} [RZ], R3;
LD{space} R4, [RZ];
@P0 STG [R2], R4;
@!P0 STG [R2+0x4], R4;
EXIT;
.endkernel
'''), 'isolation')
    r = launch.kernel_request(image, (), [], 2, block, dict(address=0x200000))
    expected = reference.run_kernel(r, 'SM89')
    assert expected.get('outputs') == want, expected
    assert kernel.run_kernel(r, 'SM89') == expected


def test_unsupported_ordering():
    image = cubin.kernel_image(assemble_cubin('''.target SM89
.kernel ordering
.registers 4, 0
MEMBAR.SC.GPU;
EXIT;
.endkernel
'''), 'ordering')
    r = launch.kernel_request(image, (), [], 1, 1, dict(address=0x200000))
    expected = reference.run_kernel(r, 'SM89')
    assert 'unsupported ordering' in expected['error']
    assert kernel.run_kernel(r, 'SM89') == expected


def test_launch_workers_and_concurrent_reset():
    from concurrent.futures import ThreadPoolExecutor
    r = request('SM89')
    r['kernel'].update(threads=2048, trace=True, trace_threads=2)
    program = kernel.compiled(*kernel.key(r, 'SM89', 'exact'))
    expected = program.run(r, workers=1)
    assert expected['outputs'] == ['07000000'] * 2048
    assert program.run(r, workers=4) == expected
    with ThreadPoolExecutor(2) as pool:
        assert list(pool.map(lambda _: program.run(r, workers=2), range(2))) == [expected] * 2
    small = request('SM89')
    small['kernel'].update(trace=True, trace_threads=2)
    assert kernel.run_kernel(small, 'SM89') == reference.run_kernel(small, 'SM89')


def test_block_barrier():
    image = cubin.kernel_image(assemble_cubin('''.target SM89
.kernel barrier
.registers 8, 0
S2R R0, SR_TID.X;
ISETP.EQ.U32.AND P0, PT, R0, RZ, PT;
MOV R2, 0x200000;
MOV R3, 7;
@!P0 STS [RZ], R3;
BAR.SYNC 0;
LDS R4, [RZ];
S2R R5, SR_CTAID.X;
IMAD R2, R5, 4, R2;
@P0 STG [R2], R4;
EXIT;
.endkernel
'''), 'barrier')
    r = launch.kernel_request(image, (), [], 128, 64, dict(address=0x200000, stride=0),
                              context=dict(synchronization=dict(bar=True)))
    for order in ('round_robin', 'reverse'):
        r['kernel']['schedule'] = order
        expected = reference.run_kernel(r, 'SM89')
        assert expected.get('outputs') == ['07000000'] * 128, expected
        assert kernel.run_kernel(r, 'SM89') == expected


def test_blocked_synchronization():
    image = cubin.kernel_image(assemble_cubin('''.target SM89
.kernel blocked
.registers 4, 0
S2R R0, SR_TID.X;
ISETP.EQ.U32.AND P0, PT, R0, RZ, PT;
@P0 BRA barrier;
WARPSYNC 0x3;
EXIT;
barrier:
BAR.SYNC 0;
EXIT;
.endkernel
'''), 'blocked')
    r = launch.kernel_request(image, (), [], 2, 2, dict(address=0x200000),
                              context=dict(synchronization=dict(bar=True, warpsync=True)))
    expected = reference.run_kernel(r, 'SM89')
    assert 'no thread can proceed' in expected['error'], expected
    assert kernel.run_kernel(r, 'SM89') == expected
