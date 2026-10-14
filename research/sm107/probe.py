"""Compile one-line PTX candidates for sm_107a with ptxas 13.4 and print the SASS each lowers to
(or ptxas's rejection, which shows the accepted syntax). Each candidate runs inside a kernel that
loads f32 %f1-%f4, b32 %r1-%r4, b64 %d2-%d5, f16 halves %h1-%h4 of %r1/%r2 and a b8 %s1, and
stores %r10-%r13, %d10/%d11, %h9 and %f10.

    uv run python research/sm107/probe.py PTXAS research/sm107/*.txt

PTXAS: the `nvidia-cuda-nvcc` 13.4.92 wheel's nvidia/cu13/bin/ptxas (13.3 has no sm_107)."""
import re, subprocess, sys, tempfile
from pathlib import Path
from sass.paths import NVDISASM

KERNEL = """.version 9.4
.target sm_107a
.address_size 64
.visible .entry k(.param .u64 p){
 .reg .b64 %d<20>; .reg .b32 %r<40>; .reg .f32 %f<40>; .reg .b16 %h<10>; .reg .pred %p<4>; .reg .b8 %s<4>;
 ld.param.u64 %d1,[p];
 ld.global.v4.f32 {%f1,%f2,%f3,%f4},[%d1];
 ld.global.v4.b32 {%r1,%r2,%r3,%r4},[%d1+16];
 ld.global.v2.b64 {%d2,%d3},[%d1+32];
 ld.global.v2.b64 {%d4,%d5},[%d1+48];
 cvt.u8.u32 %s1, %r3; mov.b32 {%h1,%h2},%r1; mov.b32 {%h3,%h4},%r2;
 LINE
 st.global.v4.b32 [%d1],{%r10,%r11,%r12,%r13};
 st.global.v2.b64 [%d1+16],{%d10,%d11};
 st.global.b16 [%d1+32],%h9;
 st.global.f32 [%d1+40],%f10;
 ret;}
"""
PLUMBING = re.compile(r"(LDG|STG|LDC|LDCU|ULDC|MOV|UMOV|NOP|EXIT|BRA|PRMT|IMAD\.MOV|S2R|CS2R)\b")


def probe(ptxas, line, work):
    src, out = work / "t.ptx", work / "t.cubin"
    src.write_text(KERNEL.replace("LINE", line))
    p = subprocess.run([ptxas, "-arch=sm_107a", "-O3", str(src), "-o", str(out)], capture_output=True, text=True)
    if p.returncode: return "REJECT " + re.sub(r"^ptxas .*?line \d+; ", "", (p.stdout + p.stderr).strip().splitlines()[0])
    sass = subprocess.run([NVDISASM, str(out)], capture_output=True, text=True).stdout
    ops = [re.sub(r"\s*/\*[^*]*\*/\s*$", "", re.sub(r"^\s*/\*[0-9a-f]{4}\*/\s*", "", l)).rstrip(" ;")
           for l in sass.splitlines() if re.match(r"\s+/\*[0-9a-f]{4}\*/", l)]
    return "\n".join("    " + o for o in ops if o[:1].isupper() and not PLUMBING.match(o))


def main():
    ptxas, files = sys.argv[1], sys.argv[2:]
    with tempfile.TemporaryDirectory() as d:
        for f in files:
            for line in Path(f).read_text().splitlines():
                if line.strip() and not line.startswith("#"): print(line.strip()); print(probe(ptxas, line.strip(), Path(d)))


if __name__ == "__main__": main()
