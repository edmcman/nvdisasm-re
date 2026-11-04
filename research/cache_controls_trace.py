import argparse
import json
from pathlib import Path

from sass import paths
from sass.sleigh.build import ARCHES
from research.additional_opcodes import tool
from research.compiler_additional_opcodes import trace

OPERATIONS = {
    'global_l1': 'prefetch.global.L1 [%rd0];',
    'global_l2': 'prefetch.global.L2 [%rd0];',
    'local_l1': 'prefetch.local.L1 [%rd0];',
    'uniform_l1': 'prefetchu.L1 [%rd0];',
    'evict_last': 'prefetch.global.L2::evict_last [%rd0];',
    'evict_normal': 'prefetch.global.L2::evict_normal [%rd0];',
    'apply_priority': 'applypriority.global.L2::evict_normal [%rd0], 128;',
    'discard': 'discard.global.L2 [%rd0], 128;',
    'valid_32b': 'prefetch.global.L1::32B.valid_addr [%rd0];',
}


def source(operation, standalone=False):
    return f'''// sass: CCTL
.version 9.4
.target sm_89
.address_size 64
.visible .entry cache_control(.param .u64 address, .param .u64 out) {{
.reg .b32 %r<2>;
.reg .b64 %rd<4>;
ld.param.u64 %rd0, [address];
ld.param.u64 %rd1, [out];
mov.u32 %r0, %tid.x;
mul.wide.u32 %rd2, %r0, 128;
add.u64 %rd0, %rd0, %rd2;
{'mov.u32 %r1, %r0;' if standalone else 'ld.global.u32 %r1, [%rd0];'}
{operation}
mul.wide.u32 %rd2, %r0, 4;
add.u64 %rd1, %rd1, %rd2;
st.global.u32 [%rd1], %r1;
ret;
}}
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--sources', type=Path, required=True)
    parser.add_argument('--standalone', action='store_true')
    args = parser.parse_args()
    probes = args.sources / 'input'
    probes.mkdir(parents=True, exist_ok=True)
    for name, operation in OPERATIONS.items():
        (probes / f'{name}.ptx').write_text(source(operation, args.standalone))
    rows = []
    for arch in ARCHES:
        rows.extend(trace(probe, arch, args.sources, '9.4', 'SM110' if arch == 'SM101' else None,
                          include_all=True)
                    for probe in sorted(probes.glob('*.ptx')))
        args.out.write_text(json.dumps(dict(compiler=tool(paths.PTXAS),
            kind='compiler encoding evidence; no functional verification',
            target_mapping={'SM101': 'SM110'}, probes=rows), indent=1) + '\n')
        selected = [row for row in rows if row['architecture'] == arch]
        print(arch, [(Path(row['source']).stem, row['status'],
                      [(i['cls'], i['settings']) for i in row['instructions']
                       if i['opcode'] == 'CCTL']) for row in selected], flush=True)


if __name__ == '__main__':
    main()
