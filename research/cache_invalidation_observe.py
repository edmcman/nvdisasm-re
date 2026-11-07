import argparse
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

from sass import paths
from sass.assemble import encoding
from sass.decode import ir
from sass.sleigh import modifiers
from research.additional_opcodes import tool
from tests.harness import gpu

SOURCE = """.version 9.4
.target sm_89
.address_size 64
.visible .entry cache_invalidation(.param .u64 flag, .param .u64 out) {
.reg .pred %p<3>;
.reg .b32 %r<8>;
.reg .b64 %rd<4>;
ld.param.u64 %rd0, [flag];
ld.param.u64 %rd1, [out];
add.u64 %rd2, %rd0, 128;
mov.u32 %r0, %tid.x;
ld.global.ca.u32 %r1, [%rd2];
mov.u32 %r4, 0;
bar.sync 0;
setp.eq.u32 %p0, %r0, 0;
@%p0 bra producer;
loop:
add.u32 %r4, %r4, 1;
ld.acquire.gpu.global.u32 %r2, [%rd0];
setp.ne.u32 %p1, %r2, 0;
@%p1 bra consumer_done;
setp.lt.u32 %p2, %r4, 1024;
@%p2 bra loop;
consumer_done:
ld.global.ca.u32 %r3, [%rd2];
bra done;
producer:
mov.u32 %r3, 42;
st.global.u32 [%rd2], %r3;
mov.u32 %r2, 1;
st.release.gpu.global.u32 [%rd0], %r2;
done:
mul.wide.u32 %rd3, %r0, 8;
add.u64 %rd1, %rd1, %rd3;
st.global.v2.u32 [%rd1], {%r1, %r3};
ret;
}
"""

SOURCE_CROSS = """.version 9.4
.target sm_89
.address_size 64
.visible .entry cache_invalidation(.param .u64 flag, .param .u64 out) {
.reg .pred %p<3>;
.reg .b32 %r<10>;
.reg .b64 %rd<5>;
ld.param.u64 %rd0, [flag];
ld.param.u64 %rd1, [out];
add.u64 %rd2, %rd0, 256;
add.u64 %rd3, %rd0, 128;
mov.u32 %r0, %tid.x;
mov.u32 %r7, %ctaid.x;
mov.u32 %r8, %ntid.x;
mad.lo.u32 %r6, %r7, %r8, %r0;
mov.u32 %r9, %nctaid.x;
add.u32 %r9, %r9, -1;
mul.lo.u32 %r9, %r9, %r8;
mov.u32 %r1, 0;
mov.u32 %r4, 0;
setp.eq.u32 %p0, %r7, 0;
@%p0 bra producer;
ld.global.ca.u32 %r1, [%rd2];
add.u32 %r9, %r1, 1;
atom.global.add.u32 %r5, [%rd3], %r9;
loop:
add.u32 %r4, %r4, 1;
ld.acquire.gpu.global.u32 %r2, [%rd0];
setp.ne.u32 %p1, %r2, 0;
@%p1 bra consumer_done;
setp.lt.u32 %p2, %r4, 16384;
@%p2 bra loop;
consumer_done:
ld.global.ca.u32 %r3, [%rd2];
bra done;
producer:
add.u32 %r4, %r4, 1;
ld.relaxed.gpu.global.u32 %r2, [%rd3];
setp.ge.u32 %p1, %r2, %r9;
@%p1 bra producer_done;
setp.lt.u32 %p2, %r4, 16384;
@%p2 bra producer;
producer_done:
mov.u32 %r3, 42;
st.global.u32 [%rd2], %r3;
mov.u32 %r5, 1;
st.release.gpu.global.u32 [%rd0], %r5;
done:
mul.wide.u32 %rd4, %r6, 16;
add.u64 %rd1, %rd1, %rd4;
st.global.v4.u32 [%rd1], {%r1, %r3, %r2, %r4};
ret;
}
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cross-block", action="store_true")
    args = parser.parse_args()
    text = SOURCE_CROSS if args.cross_block else SOURCE
    with tempfile.TemporaryDirectory() as temporary:
        source, output = Path(temporary) / "probe.ptx", Path(temporary) / "probe.cubin"
        source.write_text(text)
        subprocess.run([paths.PTXAS, "-arch=sm_89", str(source), "-o", str(output)], check=True)
        template = gpu.Template(output.read_bytes(), "cache_invalidation")
    targets = [(offset, template.decoded(offset)) for offset in range(0, len(template.code), 16)
               if template.decoded(offset).klass.mnemonic == "CCTL"]
    assert targets and all(d.klass.name == "cctl__IVALL_WBALL_D_U_noSrc" and
                           modifiers.settings("SM89", d) == dict(cache="D", cop="IVALL") for _, d in targets)
    rows = []
    driver = gpu.Driver("SM89")
    try:
        for mode in ("original", "nop", "false_guard"):
            patches = {}
            for offset, decoded in targets:
                word = (bytes.fromhex(encoding.encode("SM89", "nop_")) if mode == "nop" else
                        encoding.encode_environment(ir.load("SM89"), decoded.klass,
                                                    {("Pg", "not"): 1}, base=decoded.env)[0])
                patches[offset] = int.from_bytes(word, "little"), template.control(offset)
            image = template.image if mode == "original" else template.patch(patches)
            for grid, block in ((16, 32), (32, 32)) if args.cross_block else ((1, 64), (1, 128)):
                fixture = gpu.Fixture(f"cache_invalidation_{mode}_{grid}_{block}", image, "cache_invalidation",
                                      bytes(12 * grid * block), None, output_size=16 if args.cross_block else 8,
                                      grid=grid, block=block, scope="launch")
                outputs = [driver.run(fixture) for _ in range(3)]
                rows.append(dict(mode=mode, grid=grid, block=block, outputs=outputs,
                                 image_sha256=hashlib.sha256(image).hexdigest(),
                                 patches=[dict(offset=offset, before=template.code[offset:offset+16].hex(),
                                               after=gpu.Template(image, "cache_invalidation").code[offset:offset+16].hex())
                                          for offset, _ in targets]))
                print(mode, grid, block, sorted({(value & 0xffffffff, (value >> 32) & 0xffffffff)
                                                for values in outputs for value in values}), flush=True)
    finally: driver.close()
    args.out.write_text(json.dumps(dict(arch="SM89", device=driver.device, compiler=tool(paths.PTXAS),
        kind="compiler-generated cooperative discovery; no verification credit",
        source=text, source_sha256=hashlib.sha256(text.encode()).hexdigest(), observations=rows), indent=1) + "\n")


if __name__ == "__main__": main()
