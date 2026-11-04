import struct

from tests.harness import gpu, synchronization
from tests.refs import cache


def fixtures(arch):
    address = '[R2.64]' if arch == 'SM89' else 'desc[UR4][R2.64]'
    payload = struct.pack('<I', 0x12345678) * (3 * 64)
    return [gpu.Fixture(f'cache_{operation}_{predicate}', synchronization.image(f'''
MOV R2, c[0x0][$input];
MOV R3, c[0x0][$input+4];
LDG.E R8, {address} @sched(dst_wr_sb=0);
{'@' + predicate + ' ' if predicate != 'PT' else ''}CCTL.E.{operation} [R2+128] @sched(src_rel_sb=1);
LDG.E R9, {address} @sched(dst_wr_sb=0);
IADD3 R8, R8, R9, RZ;
''', arch, global_index=True), 'synchronize', payload, [0x2468acf0] * 64,
        block=32, scope='launch', exit=True, targets=(f'CCTL.E.{operation}',),
        modes=('exact', 'readable'))
        for operation in cache.OPERATIONS for predicate in ('P0', '!P0', 'PT')]
