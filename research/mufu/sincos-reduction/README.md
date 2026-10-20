# SM89 MUFU.SIN/COS argument reduction

This investigation checks the argument reduction proposed by sass2cu on
2026-10-18, using new GPU observations rather than fitting the saved MUFU sample.
The later [quantization investigation](../sin-fit/QUANTIZATION.md) provides a
bit-exact evaluator for the complete captured reduced domain. This file describes
the argument-reduction experiment; both investigations remain research code.

Run from the repository root, outside the sandbox for GPU access:

```sh
PYTHONPATH=src:. uv run python research/mufu/sincos_reduction.py run
```

Replay the analysis without a GPU or third-party dependencies:

```sh
python3 research/mufu/sincos_reduction.py analyze
```

Use `--out DIRECTORY` to write a new run elsewhere. `prepare` writes only the
deterministic input artifact. A run saves each completed batch so that a device
failure leaves inspectable partial observations. Analysis rejects incomplete
observations, duplicate inputs, an input-artifact hash mismatch, or GPU errors.

## Method

For each finite binary32 input, derive `F = floor(abs(x) * 2^25) mod 2^25`
directly from its exponent and significand. For COS, add `2^23` modulo `2^25`.
Split `F` into quadrant `q` and 23-bit remainder `r`. In quadrants 1 and 3,
replace `r` with `2^23 - 1 - r`. The sign is negative in quadrants 2 and 3,
and SIN additionally applies the original input sign.

Compare the observed output with **another GPU observation**, SIN at
`r * 2^-25`, with that sign bit applied. This isolates reduction from the
evaluator: no ideal sine, ulp allowance, or fitted gain enters the acceptance
comparison. Ordinary reflection (`2^23 - r`) is measured against its own direct
SIN observation as a control. Nonzero control failures help distinguish the two
rules; they are not failures of the complement model.

The vectors select 4,096 offsets spread across the first quadrant, then generate
all quadrants and both signs for SIN and COS. Inputs are rounded to binary32
*before* deriving the expected coordinate, since some desired phase positions
are not representable. Additional vectors cover:

- Starts, midpoints and final representable floats of selected fixed-point cells.
- Both sides of quadrant boundaries, adjacent input ulps and nearby grid steps,
  translated by 0, 1, 2, 4, 8 and 16 turns.
- Interior offsets translated by whole turns.
- Neighbors of powers of two from `2^22` through `2^127`, and maximum finite inputs.
- Both signed zeros, selected subnormals, infinities and NaN payloads.

The shared `Cases.probe` / `device_cases.observe` harness executes synthetic
`MUFU.SIN R8, R2` and `MUFU.COS R8, R2` instructions in generated native cubins.
These are raw discovery observations, with no expected values passed to the
GPU harness and no regression verification credit. They establish the tested
SM89 register forms only; modifiers and half-precision forms are outside this run.

## Artifacts

- `inputs.json.gz`: operation, input bits and coverage tags for every unique probe.
- `observations.json.gz`: raw output registers, instruction bytes, disassembly,
  case identity, GPU identity, timestamp, base commit and SHA256 hashes. The
  recorded word is the input encoding; the wrapper sets scheduling controls
  while preserving its low 105 instruction bits.
- `report.json`: exact comparisons and counterexamples, plus descriptive sample
  residuals against ideal sine after the proposed reduction.

The report's tag counts overlap, and canonical-only inputs are evaluator
baselines rather than independent tests of reduction. Cell checks compare
multiple inputs against the first observed input in their cell. All comparisons
include signed zeros. Residuals are sample error measurements, not exhaustive
bounds. Passing this finite experiment supports the reduction rule but does not
identify the hardware's physical implementation or prove all binary32 inputs.

## Result (2026-10-19)

NVIDIA GeForce RTX 4070 Laptop GPU, CUDA driver API 13.0; 84,488 unique
observations, no device errors. The generated instructions decode as
`MUFU.SIN R8, R2` and `MUFU.COS R8, R2`. Their scheduled words are respectively
`087308000200000000040000001ef203` and
`087308000200000000000000001ef203` (little-endian bytes).

| Exact comparison | Checked | Mismatches |
| --- | ---: | ---: |
| Complement reduction, SIN | 36,248 | 0 |
| Complement reduction, COS | 36,248 | 0 |
| Ordinary reflection, SIN (control) | 36,248 | 14,866 |
| Ordinary reflection, COS (control) | 36,248 | 16,488 |
| Constancy within selected cells | 1,280 | 0 |
| Predicted sign bit | 72,496 | 0 |
| Predicted signed zero | 720 | 0 |
| Zero/subnormal inputs | 32 | 0 |
| Nonfinite inputs produce canonical NaN | 20 | 0 |

The complement comparison includes direct first-quadrant controls. Its target
counts exclude inputs added solely as canonical baselines. Cell comparisons
span 768 operation/sign/cell groups, covering 192 distinct positive cells.

In particular, SIN inputs `0x32ffffff`, `0x33000000`, and `0x3effffff`
produce `0x00000000`, `0x34491200`, and `0x00000000`, respectively.
COS input `0x3e7fffff` produces `0x00000000`.

The largest evaluator residuals after reduction are about 1.584e-7 for SIN
and 1.571e-7 for COS on this sample. The reduction hypothesis passes these
checks; the later integer model resolves evaluation on the captured reduced
domain. No runtime semantics, shared
regression cases, coverage manifests, or verification evidence were changed.
