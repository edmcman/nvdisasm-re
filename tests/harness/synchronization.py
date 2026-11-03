from sass import launch
from sass.assemble import assemble_cubin
from sass.decode import cubin
from tests.harness import gpu


NESTED = '''
BSSY B0, joined;
@P0 BRA odd;
BSSY B1, nested;
MOV R8, 3;
@P1 BRA inner;
MOV R8, 5;
inner:
BSYNC B1;
nested:
BRA outer;
odd:
MOV R8, 7;
outer:
BSYNC B0;
joined:
VOTE.ANY R9, PT, PT;
BSSY B0, reused;
@P1 MOV R8, 11;
BSYNC B0;
reused:
IADD3 R8, R8, R9, RZ;
'''
EXITED = '''
BSSY B0, joined;
@P0 EXIT;
MOV R8, 3;
BSYNC B0;
joined:
VOTE.ANY R8, PT, PT;
'''
PREDICATED = '''
BSSY P0, B0, joined;
@!P0 BRA skip;
MOV R8, 7;
BSYNC P0, B0;
joined:
VOTE.ANY R8, PT, PT;
BRA done;
skip:
MOV R8, 3;
done:
'''
COUNTED = '''
ISETP.LT.U32.AND P2, PT, R0, 32, PT;
@!P2 BRA upper;
MOV R8, 7;
BAR.SYNC 1, 32;
BRA done;
upper:
MOV R8, 9;
BAR.SYNC 2, 32;
done:
BAR.SYNC 0;
BAR.SYNC 0;
'''
TARGET = '''
BSSY B0, joined;
MOV R8, 3;
BSYNC B0;
MOV R8, 9;
joined:
'''
COMBINED = NESTED + '''
IMAD R6, R0, 4, RZ;
STS [R6], R8 @sched(src_rel_sb=0);
BAR.SYNC.DEFER_BLOCKING 0;
MOV R12, 0x40000000;
IADD3 R11, R0, 1, RZ;
LOP3.LUT R11, R11, 63, RZ, 0xc0, !PT;
IMAD R11, R11, 4, RZ;
MUFU.RCP R10, R12 @sched(dst_wr_sb=0);
LDS R8, [R11] @sched(dst_wr_sb=0);
IADD3 R8, R8, R10, RZ;
'''
BAR_EXITED = '''
@P0 EXIT;
MOV R8, 7;
BAR.SYNC 0;
'''
COUNTED_REGISTERS = 'MOV R14, 1;\nMOV R15, 32;\n' + COUNTED.replace(
    'BAR.SYNC 1, 32;', 'BAR.SYNC R14, 32;').replace('BAR.SYNC 2, 32;', 'BAR.SYNC 2, R15;')
COUNTED_DEFERRED = COUNTED_REGISTERS.replace('BAR.SYNC ', 'BAR.SYNC.DEFER_BLOCKING ')


def image(body, arch='SM89', name='synchronize'):
    address = '[R20.64]' if arch == 'SM89' else 'desc[UR4][R20.64]'
    return assemble_cubin(f'''.target {arch}
.kernel {name}
.param input, 8, 8
.param output, 8, 8
.registers 32, 8
.shared 256
.schedule usched_info=WAIT15_END_GROUP, req_bit_set=63
MOV R1, c[0x0][0x28];
MOV R20, c[0x0][$output];
MOV R21, c[0x0][$output+4];
ULDC.64 UR4, c[0x0][{launch.GLOBAL_DESCRIPTOR[arch]}];
S2R R0, SR_TID.X @sched(dst_wr_sb=0);
IMAD.WIDE.U32 R20, R0, 4, R20;
STG.E {address}, RZ;
LOP3.LUT R3, R0, 1, RZ, 0xc0, !PT;
ISETP.NE.U32.AND P0, PT, R3, RZ, PT;
LOP3.LUT R3, R0, 2, RZ, 0xc0, !PT;
ISETP.NE.U32.AND P1, PT, R3, RZ, PT;
{body}
STG.E {address}, R8;
EXIT;
.endkernel
''')


def request(body, threads=32, block=32, order='round_robin', arch='SM89'):
    kernel = cubin.kernel_image(image(body, arch), 'synchronize')
    return launch.kernel_request(kernel, (gpu.INPUT, gpu.OUTPUT), [], threads, block,
                                 dict(address=gpu.OUTPUT), scope='launch', schedule=order)


def nested_expected(threads):
    return [((1 << min(32, threads - i // 32 * 32)) - 1)
            + (11 if i & 2 else 7 if i & 1 else 5) & 0xffffffff for i in range(threads)]


def fixtures(arch):
    cases = (
        ('nested', NESTED, nested_expected(32), ('BSSY', 'BSYNC', 'VOTE')),
        ('exited', EXITED, [0 if i & 1 else 0x55555555 for i in range(32)], ('BSSY', 'BSYNC', 'VOTE')),
        ('predicated', PREDICATED, [0xaaaaaaaa if i & 1 else 3 for i in range(32)], ('BSSY', 'BSYNC')),
        ('counted', COUNTED, [7] * 32 + [9] * 32, ('BAR',)),
        ('counted_registers', COUNTED_REGISTERS, [7] * 32 + [9] * 32, ('BAR',)),
        ('counted_deferred', COUNTED_DEFERRED, [7] * 32 + [9] * 32, ('BAR.SYNC.DEFER_BLOCKING',)),
        ('target', TARGET, [9] * 32, ('BSSY', 'BSYNC')),
        ('bar_exited', BAR_EXITED, [0 if i & 1 else 7 for i in range(32)], ('BAR',)),
        ('combined', COMBINED, [nested_expected(64)[(i + 1) % 64] + 0x3f000000 & 0xffffffff
                                for i in range(64)], ('BSSY', 'BSYNC', 'BAR.SYNC.DEFER_BLOCKING', 'LDS', 'STS', 'MUFU.RCP')),
    )
    return [gpu.Fixture('sync_' + name, image(body, arch), 'synchronize', bytes(12 * len(expected)),
                        expected, block=len(expected), scope='launch', exit=True,
                        targets=targets, modes=('exact', 'readable'))
            for name, body, expected, targets in cases]
