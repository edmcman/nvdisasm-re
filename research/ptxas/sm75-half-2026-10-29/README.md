# SM75 half-source compiler evidence — 2026-10-29

This is compiler evidence, with no SM75 GPU execution. CUDA 13.0.88 ptxas
compiled 262 runtime-input entries at both `-O0` and `-O3`; nvdisasm 13.4.92
disassembled the actual cubins. `metadata.json` pins versions, hashes, the
source revision and artifact checksums. Compressed files preserve disassembly
and decoded instruction words/settings. The generator reproduces the full PTX;
the large matrix cubins are regenerated rather than committed. Reproduce with:

```sh
nice uv run python research/ptxas/sm75_half_lowering.py /tmp/sm75-half.ptx
nice .venv/lib/python3.13/site-packages/nvidia/cu13/bin/ptxas \
    -O3 -arch=sm_75 /tmp/sm75-half.ptx -o /tmp/sm75-half.cubin
```

The matrix varies RN/RZ/RM/RP conversion, scalar/packed consumers, each
operand position, low-half stores, observable broadcasts, arithmetic before
half conversion, half arithmetic before widening, float arithmetic rounding
and all fourteen ordered/unordered/numeric/NaN comparisons. Both float
comparison inputs are also converted in the PTX.

At `-O3`, 106 instructions have `.F32` source selectors: 42 HADD2 conversions
with the other operand `-RZ`, 32 HSET2 and 32 HSETP2. No such source instruction
appears at `-O0`. The optimized comparison traces remove explicit RZ conversion
instructions and consume binary32 registers directly, in either operand
position and with both operands converted. The observable packed broadcast
stores the full HADD2 result immediately, without PRMT/SHF or another operation
to duplicate the low lane. Its high-lane duplication is now compiler-backed.

HMUL2/HFMA2 retain an explicit HADD2 conversion followed by half arithmetic.
Float arithmetic before conversion remains FADD/FMUL/FFMA followed by
conversion. HADD2.F32 widening uses a negative-zero operand. This search
emitted no mixed `.F32` HMUL2/HFMA2 source, no nonzero-operand HADD2.F32 sum,
and no fused mixed HADD2 arithmetic. These are bounded negative findings for
the recorded compiler and patterns, not statements that hardware forbids the
encodings or that no compiler input can emit them.

Stable regression PTX is in `tests/ptx/fp16/sm75_*.ptx`.
`tests/test_ptx_sm75_half.py` checks actual selector fields, conversion removal,
the direct packed store and the preserved mul/FMA conversion stage.
References and ordinary instruction inputs belong in `tests/refs/float.py`
and the packed autocase adapter/profiles, rather than in a manual GPU matrix.

## Live compiler reverse engineering

`internal/` preserves three compact GDB captures, their small output cubins and
the encoding-validation result. `internal/metadata.json` pins the compiler,
image base, GDB version, instrumentation/input hashes and artifact checksums.
The maintained scripts and PTX generator reproduce the inputs and tracing.
Thirteen functions were annotated and saved in the connected Ghidra `ptxas`
program; their addresses and roles are documented in `ptxas-finalizer-notes.md`.

The legacy SM75 emission path runs through `0xc5dbb0` and `0x1bb27e0`.
HADD2, HMUL2 and HFMA2 shape selection is at `0xa3ed80`, `0xa3f250` and
`0xa3ef80`; HMUL2/HFMA2 field builders are at `0xc529c0`/`0xc52190`.
The clean control capture contains 34 half-emission records: 26 HADD2, four
HMUL2 and four HFMA2. Every captured 128-bit word appears exactly in the
cubin from that run, including control bits. This is a partial capture with
repeated sizing/emission records, not an ordered capture of the entire text.
`internal/validation.json` records this check; it gives no value-semantics
verification credit.

Paired single-entry watchpoint captures distinguish the optimizer's treatment
of the same RZ conversion. The HSET2 consumer leaves destination analysis
flags `0x3`; HMUL2 leaves `0x2`, with identical weights `(1.0, 1.0)`.
`0x820c70` calls eligibility predicate `0x820960`, and HMUL2's rejected use
clears bit 0 at store `0x821035` (watchpoint next PC `0x821039`). At gate
`0x821370`, HSET2's conversion is promoted through `0x820ba0`, changes type
7 to 6 and becomes removable; HMUL2's stays and lowers through `0x821570`
to HADD2. These fields are phase-local scratch metadata, so unrelated writes
are not evidence of a semantic transformation.

The rejection identifies a compiler restriction. It does not distinguish
whether a directly encoded mixed HMUL2/HFMA2 instruction narrows an F32 input
before arithmetic or retains its precision until result rounding. The encoder
also cannot establish this. Those forms remain opaque.

Reproduce the clean control capture using a fresh output path:

```sh
SM75_HALF_CAPTURE=/tmp/sm75-controls.jsonl nice gdb -q -nx -batch \
    -ex 'set debuginfod enabled off' \
    -ex 'source research/ptxas/capture_sm75_half.py' -ex run --args \
    .venv/lib/python3.13/site-packages/nvidia/cu13/bin/ptxas \
    -O3 -arch=sm_75 tests/ptx/fp16/sm75_precision_controls.ptx \
    -o /tmp/sm75-controls.cubin
.venv/bin/python research/ptxas/validate_sm75_half_capture.py \
    /tmp/sm75-controls.jsonl /tmp/sm75-controls.cubin
```

Generate the two single-entry inputs for paired watchpoint captures:

```sh
.venv/bin/python - <<'PY'
from pathlib import Path
from research.ptxas import sm75_half_lowering
for name in ("set_packed_0_rz", "mul_packed_0_rz"):
    entry = next(text for text in sm75_half_lowering.matrix()
                 if f".entry {name}(" in text)
    Path("/tmp", name + ".ptx").write_text(sm75_half_lowering.HEADER + entry)
PY
```

Add `SM75_HALF_WATCH_CVT=1` to the GDB invocation above and pass either
single-entry input. The watch mode uses two hardware watchpoints. These
commands instrument the compiler and launch no GPU process. The maintained
control fixture subsequently received whitespace cleanup; the input hash
identifies the original capture input, while the emitted instructions are
unchanged.

## Implementation verification

The follow-up passed 230 unit/compiler tests,
693 Python tests (48,440 shared cases), 690 native tests, 690 Ghidra tests
(48,449 cases), and 8,484 SM89 GPU scenarios with 43 emulator-only exclusions.
SM75/SM89 exact and readable operation-count changes were reviewed, updated
and checked again. All ten architecture plans reproduced successfully; the
coverage refresh completed and its final consistency check passed.

The SM89 hardware run and rejudged observations validate the affected SM89
cases. They do not validate SM75-only F32 selectors. The compiler matrix,
live RE and software agreement remain separate from GPU verification.

The full investigation archive, including validation logs, Ghidra exports,
duplicate source snapshots and regenerated binaries, is preserved locally at
`.cache/pr106-research-archive-2026-10-29/` and excluded from the PR.
