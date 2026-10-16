"""Does ptxas ever emit P2R with a base other than RZ? Compiles kernels with many live
predicates (a loop, and calls to an extern function, so the caller saves them) for several
architectures and options, and counts P2R/R2P and non-RZ P2R bases.

usage: uv run python research/ptxas/p2r_base_probe.py
"""
import re, subprocess, tempfile
from itertools import product
from pathlib import Path
from sass.paths import CUOBJDUMP, PTXAS


def kernel(n, call):
    body = ["{ .param .b32 pa; .param .b32 pr; st.param.b32 [pa],%r100; call.uni (pr), g, (pa); ld.param.b32 %r100,[pr]; }" if call
            else "mul.lo.u32 %r100,%r100,%r100;"]
    return "\n".join([".version 8.0", ".target sm_75", ".address_size 64",
                      *([".extern .func (.param .b32 r) g(.param .b32 x);"] if call else []),
                      ".visible .entry k(.param .u64 o,.param .u64 a,.param .u32 m){",
                      f".reg .pred %p<{n + 2}>;", ".reg .b32 %r<200>;", ".reg .b64 %rd<8>;",
                      "ld.param.u64 %rd1,[a];", "ld.param.u64 %rd2,[o];", "ld.param.u32 %r199,[m];", "mov.u32 %r0,%tid.x;",
                      *(f"ld.global.u32 %r{i + 1},[%rd1+{4 * i}];\nsetp.gt.u32 %p{i},%r{i + 1},%r0;" for i in range(n)),
                      "mov.u32 %r100,0;", "LOOP:",
                      *(f"@%p{i} add.u32 %r100,%r100,{i + 1};\n" + body[0] for i in range(n)),
                      "sub.u32 %r199,%r199,1;", f"setp.ne.u32 %p{n},%r199,0;", f"@%p{n} bra LOOP;",
                      "st.global.u32 [%rd2],%r100;", "ret;", "}"]) + "\n"


def main():
    with tempfile.TemporaryDirectory() as tmp:
        ptx, cubin = Path(tmp, "k.ptx"), Path(tmp, "k.cubin")
        for (n, call), arch, opts in product(((40, False), (21, True)), ("sm_75", "sm_89", "sm_90", "sm_100"),
                                             ([], ["-maxrregcount=24"], ["-maxrregcount=48"], ["-O0"])):
            ptx.write_text(kernel(n, call))
            subprocess.run([PTXAS, "-c", f"-arch={arch}", *opts, "-o", cubin, ptx], check=True, capture_output=True)
            sass = subprocess.run([CUOBJDUMP, "-sass", cubin], check=True, capture_output=True, text=True).stdout
            p2r = re.findall(r"P2R [^;]*;", sass)
            based = [s for s in p2r if ", PR, RZ," not in s]
            print(f"{n:2} predicates {'call' if call else 'loop'} {arch:6} {' '.join(opts):18} {len(p2r):3} P2R {sass.count('R2P'):3} R2P {len(based)} non-RZ {based[:3]}")


if __name__ == "__main__":
    main()
