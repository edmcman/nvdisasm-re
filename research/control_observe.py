import argparse
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from sass import paths
from sass.assemble import encoding
from sass.decode import ir
from tests.harness import gpu


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    source = (paths.ROOT / 'research/additional-opcodes/control-acquire.ptx').read_text()
    rows = []
    driver = gpu.Driver('SM89')
    try:
        with tempfile.TemporaryDirectory() as temporary:
            observe(source, driver, rows, Path(temporary))
    finally: driver.close()
    args.out.write_text(json.dumps(dict(
        kind='bounded compiler-generated whole-kernel discovery; no verification credit', device=driver.device,
        compiler_sha256=hashlib.sha256(Path(paths.PTXAS).read_bytes()).hexdigest(), observations=rows), indent=1)+'\n')


def observe(source, driver, rows, directory):
    for producer in (0, 15, 31, 63):
        ptx = directory / f'producer-{producer}.ptx'
        ptx.write_text(source.replace('setp.eq.u32 %p0, %r0, 0;', f'setp.eq.u32 %p0, %r0, {producer};'))
        image_path = ptx.with_suffix('.cubin')
        subprocess.run([paths.PTXAS, '-arch=sm_89', '-O3', str(ptx), '-o', str(image_path)], check=True, capture_output=True)
        template = gpu.Template(image_path.read_bytes(), 'acquire_loop')
        offset = next(o for o in range(0, len(template.code), 16) if template.decoded(o).klass.mnemonic == 'YIELD')
        decoded = template.decoded(offset)
        for mode in ('original', 'nop', 'false_operand', 'false_guard'):
            match mode:
                case 'original': image = template.image
                case 'nop':
                    word = int.from_bytes(bytes.fromhex(encoding.encode('SM89', 'nop_')), 'little')
                    image = template.patch({offset: (word, template.control(offset))})
                case _:
                    fields = {('Pp' if mode == 'false_operand' else 'Pg', 'not'): 1}
                    word = encoding.encode_environment(ir.load('SM89'), decoded.klass, fields, base=decoded.env)[0]
                    image = template.patch({offset: (int.from_bytes(word, 'little'), template.control(offset))})
            for block in (64, 128):
                fixture = gpu.Fixture(f'yield_{producer}_{mode}_{block}', image, 'acquire_loop', bytes(12 * block), None, block=block, scope='launch')
                outputs = driver.run(fixture)
                rows.append(dict(producer=producer, mode=mode, block=block, outputs=outputs,
                                 image_sha256=hashlib.sha256(image).hexdigest()))
                print(producer, mode, block, sorted(set(outputs)), flush=True)

if __name__ == '__main__': main()
