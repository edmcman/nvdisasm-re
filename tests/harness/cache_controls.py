import struct

from sass import launch
from sass.assemble import assemble_cubin
from tests.harness import gpu, synchronization
from tests.refs import cache


def fixtures(arch, cross_block=True):
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
        for operation in cache.OPERATIONS for predicate in ('P0', '!P0', 'PT')] + (
            invalidations(arch) if cross_block else [])


def invalidation_image(arch, predicate):
    address = lambda offset: f"[R20+{offset}]" if arch == "SM89" else f"desc[UR4][R20.64+{offset}]"
    output = "[R24]" if arch == "SM89" else "desc[UR4][R24.64]"
    return assemble_cubin(f""".target {arch}
.kernel cache_invalidation
.param input, 8, 8
.param output, 8, 8
.registers 32, 8
.schedule usched_info=WAIT15_END_GROUP, req_bit_set=0
MOV R20, c[0x0][$input];
MOV R21, c[0x0][$input+4];
MOV R24, c[0x0][$output];
MOV R25, c[0x0][$output+4];
ULDC.64 UR4, c[0x0][{launch.GLOBAL_DESCRIPTOR[arch]}];
S2R R0, SR_TID.X @sched(dst_wr_sb=0);
S2R R1, SR_CTAID.X @sched(dst_wr_sb=1);
IMAD R2, R1, 32, R0 @sched(req_bit_set=3);
IMAD.WIDE.U32 R24, R2, 8, R24;
MOV R8, RZ;
MOV R13, RZ;
ISETP.EQ.U32.AND P0, PT, R1, RZ, PT;
@P0 BRA producer;
LDG.E R8, {address(256)} @sched(dst_wr_sb=0);
IADD3 R10, R8, 1, RZ @sched(req_bit_set=1);
ATOMG.E.ADD.STRONG.GPU PT, R11, {address(128)}, R10 @sched(dst_wr_sb=0);
MOV R11, R11 @sched(req_bit_set=1);
consumer:
IADD3 R13, R13, 1, RZ;
ISETP.GE.U32.AND P2, PT, R13, 16384, PT;
@P2 BRA limited;
LDG.E.STRONG.GPU R9, {address(0)} @sched(dst_wr_sb=0);
ISETP.NE.U32.AND P1, PT, R9, RZ, PT @sched(req_bit_set=1);
{'@' + predicate + ' ' if predicate != 'PT' else ''}CCTL.IVALL;
@!P1 BRA consumer;
LDG.E R12, {address(256)} @sched(dst_wr_sb=0);
BRA done;
producer:
IADD3 R13, R13, 1, RZ;
ISETP.GE.U32.AND P2, PT, R13, 16384, PT;
@P2 BRA limited;
LDG.E.STRONG.GPU R9, {address(128)} @sched(dst_wr_sb=0);
ISETP.GE.U32.AND P1, PT, R9, 32, PT @sched(req_bit_set=1);
@!P1 BRA producer;
MOV R12, 42;
STG.E {address(256)}, R12;
MEMBAR.SC.GPU;
MOV R10, 1;
STG.E.STRONG.GPU {address(0)}, R10;
done:
MOV R9, R12 @sched(req_bit_set=1);
STG.E.64 {output}, R8;
EXIT;
limited:
MOV R12, 0xffffffff;
BRA done;
.endkernel
""")


def invalidations(arch):
    return [gpu.Fixture(f"cache_invalidation_{predicate}", invalidation_image(arch, predicate),
        "cache_invalidation", bytes(12 * 64), cache.released_payload(64, 42),
        output_size=8, block=32, scope="launch", exit=True, targets=("CCTL.IVALL",),
        modes=("exact", "readable")) for predicate in ("PT", "!P0", "P1")]
