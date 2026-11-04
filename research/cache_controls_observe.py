import argparse
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

from sass import paths
from sass.decode import cubin, pydecode
from research.additional_opcodes import tool
from research.cache_controls_trace import OPERATIONS, source
from tests.harness import gpu


def probe(operation):
    return (source(operation)
            .replace('.reg .b32 %r<2>;', '.reg .b32 %r<4>;')
            .replace('mov.u32 %r0, %tid.x;', '''mov.u32 %r0, %tid.x;
mov.u32 %r2, %ctaid.x;
mov.u32 %r3, %ntid.x;
mad.lo.u32 %r0, %r2, %r3, %r0;''')
            .replace('ld.global.u32 %r1', 'ld.volatile.global.u32 %r1')
            .replace('mul.wide.u32 %rd2, %r0, 4;', 'mul.wide.u32 %rd2, %r0, 8;')
            .replace('st.global.u32 [%rd1], %r1;', '''ld.volatile.global.u32 %r0, [%rd0];
st.global.v2.u32 [%rd1], {%r1, %r0};'''))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    driver = gpu.Driver('SM89')
    data = b''.join(((lane * 0x1020304 + word * 0x9e3779b9) & 0xffffffff).to_bytes(4, 'little')
                    for lane in range(64) for word in range(32))
    rows = []
    try:
        for name in ('global_l1', 'global_l2', 'uniform_l1', 'evict_last', 'evict_normal', 'apply_priority'):
            text = probe(OPERATIONS[name])
            with tempfile.TemporaryDirectory() as temporary:
                ptx, output = Path(temporary) / 'probe.ptx', Path(temporary) / 'probe.cubin'
                ptx.write_text(text)
                subprocess.run([paths.PTXAS, '-arch=sm_89', str(ptx), '-o', str(output)], check=True)
                image = output.read_bytes()
            instructions = cubin.kernel_image(image, 'cache_control')['code']
            targets = [dict(offset=offset, word=instructions[offset:offset + 16].hex(),
                            text=decoded.text)
                       for offset in range(0, len(instructions), 16)
                       if (decoded := pydecode.decode('SM89', instructions[offset:offset + 16])).klass.mnemonic == 'CCTL']
            fixture = SimpleNamespace(name=name, image=image, kernel='cache_control', arguments=(),
                                      data=data, threads=64, block=32, output_size=8)
            outputs = [driver.run(fixture) for _ in range(3)]
            rows.append(dict(name=name, source=text, targets=targets, input_hex=data.hex(),
                             image_sha256=hashlib.sha256(image).hexdigest(), outputs=outputs))
            args.out.write_text(json.dumps(dict(arch='SM89', device=driver.device,
                compiler=tool(paths.PTXAS), kind='compiler-generated cache discovery; no verification credit',
                observations=rows), indent=1) + '\n')
            print(name, [target['text'] for target in targets],
                  sorted({(value & 0xffffffff) ^ (value >> 32) for run in outputs for value in run}), flush=True)
    finally: driver.close()


if __name__ == '__main__':
    main()
