# Open questions and roadmap

- Cooperative scheduling covers one interleaving: round-robin lockstep keeps warps in step, so `shared_exchange` likely passes even with BAR.SYNC as a no-op. Run cooperative kernels under adversarial schedules (warp-greedy until blocked, reverse, seeded random) and require identical results: that tests barrier enforcement and that fixture kernels are race-free. Timing proper matters only for racy code, fence-based ordering (memory model, not cycles) and scoreboards: the emulator completes instructions instantly, trusting control bits, and GPU fixtures keep the original controls on patched words, so a patched slot with longer or variable latency could read stale values on the GPU.
- Trace the `-plr` liveness code that consumes connectors (start at the "Cannot %s as flow analysis is disabled" check, Ghidra 0x10a327).
- Parse md + latencies into a per-opcode table (operands, role, width, pipe, latency).
- Why nvdisasm prints some decodable instructions blank (GATHER, SCATTER, `__HIR*`, BPT.PAUSE, E6M9 immediates, SM75 HMMA .BF16/.TF32) and silently drops some modifier values (SM75 HMMA `altfmt=INVALID3`). Not a name table in the binary; never seen in real code.
- SLEIGH generation from `src/sass/decode/ir.py` (plan phase 3).
- Semantics roadmap, ordered by `src/sass/check/census.py` (2026-10-08; share = average fraction of all real-corpus instructions across SM75-SM120). After the control-flow fix: native 58-76% per arch, opaque 16-36%, primitive 6-8%.
  1. Done: control flow on every fallback branch; native SM90+ BRA.
  2. Done: cheap coverage (~10%): `URb`/`URc` operand names in IMAD/ISETP/IADD3/LEA emitters (~3.5%, ~7% on SM90); alias uniform/variant ops to existing emitters (UIADD3, UIMAD, USHF, ULEA, UISETP, ULOP3, USEL, UMOV, VIADD, IADD; ~3.5%); native PRMT (1.6%); FSEL (1.1%), FMNMX (0.6%, NaN/±0 rules).
  3. Done: native RN fp64 DFMA/DADD/DMUL and ordered/unordered DSETP (~5%); DFMA via Ghidra's 16-byte float, where the double product is exact. Directed rounding and DSETP MIN/MAX stay opaque.
  4. Done: native packed fp16x2 HADD2/HFMA2 (~2%) with lane broadcasts/modifiers and SAT, including the `HFMA2.MMA Rd, -RZ, RZ, imm` constant move. FTZ/FMZ, alternate formats, RELU and other MMA forms stay opaque.
  5. Done: carry chains IADD3.X, LEA.HI.X, IMAD.X and ordinary predicate outputs (SM89 hardware-established; all-architecture emulator/dataflow checks).
  6. Kernel parameters as named constant-bank-0 variables in the decompiler (offsets/sizes from `.nv.info`, `src/sass/decode/cubin.py`).
  Stays primitive: SHFL, VOTE, BAR, BSSY/BSYNC, MUFU (multi-thread or hardware approximation). Ghidra's emulator FloatFormat underflow bug (see below) could be reported upstream.
