import pytest
from sass.emulate import kernel, reference
from tests.harness import synchronization

request = synchronization.request
INVALID = (
    ('BSYNC B0;', 'no matching BSSY'),
    ('BSSY B0, joined; BSSY B0, joined; BSYNC B0; joined:', 'active convergence ID'),
    ('ISETP.LT.U32.AND P2, PT, R0, 32, PT; @P2 BAR.SYNC 1, 64; @!P2 BAR.SYNC 1, 32;', 'incompatible barrier'),
    ('ISETP.LT.U32.AND P2, PT, R0, 32, PT; @P2 BAR.SYNC 1; @!P2 BAR.SYNC 2;', 'no thread can proceed'),
    ('BAR.SYNC R0, R0;', 'unresolved packed-register barrier'),
)

def compare(r, expected, arch):
    python = reference.run_kernel(r, arch)
    assert python.get('outputs') == [x.to_bytes(4, 'little').hex() for x in expected], python
    native = kernel.run_kernel(r, arch)
    assert native == python
    return python


@pytest.mark.parametrize('arch', ['SM89', 'SM90'])
@pytest.mark.parametrize('order', ['round_robin', 'reverse', 'seeded'])
@pytest.mark.parametrize('threads', [7, 32, 37])
def test_nested_reused_convergence(arch, order, threads):
    r = request(synchronization.NESTED, threads, 64, order, arch)
    expected = synchronization.nested_expected(threads)
    compare(r, expected, arch)


@pytest.mark.parametrize('order', ['round_robin', 'reverse', 'seeded'])
def test_exited_convergence_members(order):
    r = request(synchronization.EXITED, order=order)
    compare(r, [0 if i & 1 else 0x55555555 for i in range(32)], 'SM89')


@pytest.mark.parametrize('order', ['round_robin', 'reverse', 'seeded'])
def test_predicated_convergence(order):
    r = request(synchronization.PREDICATED, order=order)
    compare(r, [0xaaaaaaaa if i & 1 else 3 for i in range(32)], 'SM89')


@pytest.mark.parametrize('order', ['round_robin', 'reverse', 'seeded'])
@pytest.mark.parametrize('body', [synchronization.COUNTED, synchronization.COUNTED_REGISTERS,
                                 synchronization.COUNTED_DEFERRED])
def test_independent_counted_barriers(order, body):
    r = request(body, threads=64, block=64, order=order)
    compare(r, [7] * 32 + [9] * 32, 'SM89')


@pytest.mark.parametrize('order', ['round_robin', 'reverse', 'seeded'])
def test_combined_replay_blockers(order):
    expected = synchronization.nested_expected(64)
    compare(request(synchronization.COMBINED, 64, 64, order),
            [expected[(i + 1) % 64] + 0x3f000000 & 0xffffffff for i in range(64)], 'SM89')


@pytest.mark.parametrize('order', ['round_robin', 'reverse', 'seeded'])
def test_exited_block_barrier_members(order):
    compare(request(synchronization.BAR_EXITED, order=order),
            [0 if i & 1 else 7 for i in range(32)], 'SM89')


@pytest.mark.parametrize('body, error', INVALID)
def test_invalid_synchronization(body, error):
    r = request(body, threads=64, block=64)
    python = reference.run_kernel(r, 'SM89')
    assert error in python.get('error', ''), python
    assert kernel.run_kernel(r, 'SM89') == python


@pytest.mark.ghidra
@pytest.mark.parametrize('arch', ['SM89', 'SM90'])
def test_ghidra_synchronization_orders(arch, pytestconfig):
    from sass import ghidra_bridge
    from tests.harness import gpu, hardware, select
    if arch not in select.arches(pytestconfig): pytest.skip(f'{arch} not selected')
    modes = select.modes(pytestconfig)
    select.require_languages([(arch, mode) for mode in modes])
    fixtures = synchronization.fixtures(arch)
    requests = [dict(r, kernel=dict(r['kernel'], schedule=order))
                for order in ('round_robin', 'reverse', 'seeded') for r in map(gpu.request, fixtures)]
    for order in ('round_robin', 'reverse', 'seeded'):
        requests.extend(synchronization.request(synchronization.NESTED, n, 64, order, arch) for n in (7, 37))
    requests.extend(synchronization.request(body, 64, 64, arch=arch) for body, _ in INVALID)
    expected = [reference.run_kernel(r, arch) for r in requests]
    for f, result in zip(fixtures * 3, expected): assert not hardware.emulated_problems(f, result)
    for n, result in zip((7, 37) * 3, expected[len(fixtures) * 3:]):
        assert result.get('outputs') == [x.to_bytes(4, 'little').hex() for x in synchronization.nested_expected(n)]
    for (_, error), result in zip(INVALID, expected[-len(INVALID):]): assert error in result.get('error', '')
    results = ghidra_bridge.emulate_sharded({mode: (arch, mode, requests) for mode in modes}, workers=len(modes))
    assert all(found == expected for found in results.values())
