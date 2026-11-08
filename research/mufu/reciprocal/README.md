# SM89 reciprocal recovery, 2026-11-03

The captures come from the RTX 4070 Laptop GPU. `reciprocal_observe.py` generates
single-instruction inputs through the MUFU autocase adapter and calls
`device_cases.observe`; it never consults expected outputs. Discovery captures do
not earn instruction-form verification credit.

F16 H0/H1 files contain every 16-bit input, with different poison values in the
unselected half. Each operation's two captures agree completely. F32 and 64H
files include normalized grids, every finite exponent transition, exceptional
values and independently seeded random mantissas.

The SIN investigation supplied the retained-partial-product square. RCP uses 128
mantissa segments; RSQ uses 64 and separate exponent-parity tables. Ordinary
squaring and nearby retained-bit counts cannot satisfy the rounding intervals
of the observed words. The common 15-bit square can.

Sparse coefficient fits initially missed independent random inputs. `fit.json`
and `joint.json` preserve those exploratory results, including failed candidates.
`reciprocal_refine.py` constructs feasible coefficient sets, scans the normalized
domain and observes inputs on which those sets disagree. The refined captures
record the actual selected input words; their selection reports record the
source captures, seed and segment counts.

`models.json` is the selected model data. The runtime uses grid `[57, 42, 40]`:
accumulator fraction bits, constant fraction bits and quadratic fraction bits.
Linear coefficients have `57-local_width` fraction bits. These are sufficient
representation widths, not evidence of the physical SFU ROM layout. F32 uses
nearest-even output rounding; 64H truncates to the high word. The model's
normalized-unit power exception is established separately by GPU observations.

`*-full.json` describes a complete normalized domain; the accompanying gzip file
contains little-endian signed 32-bit differences between consecutive output words,
starting from zero and continuing across exponent parities. Input words are
`((exponent_bias + parity) << mantissa_bits) + offset`, in metadata order.
SHA-256 covers the compressed payload.

`validation.json` compares each complete capture against both the fitted arithmetic
and the production C++ header. It records the tested architecture, precision,
parities, counts and mismatches. Accuracy here means emulator versus GPU. The
hardware's error relative to mathematical reciprocal or reciprocal square root
is a separate measurement.

All 28,311,552 complete-domain outputs match both models and the C++ runtime
exactly: 25,165,824 F32 words and 3,145,728 64H words. The four complete F16
captures add 262,144 observations, covering both operations and half selectors.

Reproduce a raw domain from the repository root:

```sh
nice uv run python -m research.mufu.reciprocal_observe \
  --arch SM89 --op RCP --precision F16 --extract H0 \
  --output research/mufu/reciprocal/SM89-rcp-H0.json.gz

nice uv run python -m research.mufu.reciprocal_observe \
  --arch SM89 --op RSQ --precision F32 --full-normalized \
  --output research/mufu/reciprocal/SM89-rsq-F32-full.json
```

Use `--count 4096 --offsets 0 1 17 127 511` for the offset grids,
`--count 32768 --random 117` for the first holdout,
`--count 65536 --random 118` for the second and
`--count 65536 --random 121` for the blinded final holdout.
Change the operation, precision or half selector for the other files.
RCP's full capture covers one normalized exponent; its exponent scaling is also
checked with the parity-paired random captures and exponent-transition inputs.
RSQ's full capture covers both parities.

Fitting and refinement use NumPy 1.26.4 and SciPy 1.17.1:

```sh
nice uv run --no-project --python 3.12 \
  --with numpy==1.26.4 --with scipy==1.17.1 \
  env PYTHONPATH=.:src OPENBLAS_NUM_THREADS=1 \
  python research/mufu/reciprocal_fit.py \
  research/mufu/reciprocal/*-offsets.json.gz \
  research/mufu/reciprocal/*-holdout.json.gz \
  research/mufu/reciprocal/*-holdout2.json.gz \
  research/mufu/reciprocal/*-refined.json.gz \
  research/mufu/reciprocal/*-refined2.json.gz \
  --join --model-only --integer --grid 51 36 34 --grid 57 42 40 \
  --validate research/mufu/reciprocal/*-holdout3.json.gz \
  --output research/mufu/reciprocal/models.json \
  --export-header src/sass/emulate/include/sass/reciprocal_coefficients.hpp

nice uv run --no-project --python 3.12 \
  --with numpy==1.26.4 --with scipy==1.17.1 \
  env PYTHONPATH=.:src OPENBLAS_NUM_THREADS=1 \
  python research/mufu/reciprocal_verify.py \
  research/mufu/reciprocal/*-full.json \
  --output research/mufu/reciprocal/validation.json
```

Refinement uses the same scientific environment. Pass the offset and first two
holdout capture sets to `reciprocal_refine.py --output-dir research/mufu/reciprocal
--limit 256 --seed 119`; include the resulting refined captures for the second
round, with `--seed 120 --tag refined2`. Exact selected inputs can always be
re-observed directly through `reciprocal_observe.capture`.

Run GPU commands outside the sandbox. Routine Python tests sample large captures
while retaining exponent transitions and exceptional inputs; `--exhaustive`
checks every saved grid/random/refinement word. Complete normalized-domain
verification uses the bulk C++ checker, avoiding millions of Python callback
invocations.

Hardware claims apply to SM89 and the tested forms. SM90 software checks do not
establish SM90 hardware behavior. BF16 reciprocals remain explicit unresolved
runtime errors. The SM103 SIMD MUFU classes permit only EX2, whose numerical
behavior remains unresolved; they do not provide SIMD reciprocal forms.
No tolerance is borrowed from F32 for unresolved precisions.
