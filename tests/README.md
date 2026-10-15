# Tests

Each test checks one artifact against one oracle, so a failure points at one component. Tests are
grouped by what they need to run; markers say only that: `ghidra` (compiled SLEIGH languages and
Ghidra's pyghidra venv) and `gpu` (an SM89 device).

### Python tests: `uv run pytest`

Need Python and the target nvdisasm only. Run them constantly. CI (`.github/workflows/ci.yml`) runs them on every architecture and lowering for every push; on a runner the real corpus is empty (no CUDA libraries), so `test_pydecode_disassembly` checks synthetic and mutated words only.

| File | Question | Under test | Oracle |
|---|---|---|---|
| `test_refs.py`, `test_softfloat.py` | are the references right? | `refs/`, softfloat | libm, brute force, hand values |
| `test_mdexpr.py`, `test_fold.py`, `test_cubin.py` | are the helpers right? | md expressions, decode-time folding, cubin reader | hand values, evaluation |
| `test_pydecode_disassembly.py` | does pydecode decode like nvdisasm? | `sass.decode.pydecode` | nvdisasm (ratchet `baseline/SMxx.json`; CLI `sass-compare-pydecode`) |
| `test_family_lowering.py` | does every family lower on every class it covers? | `sass.semantics` families under the SLEIGH enumeration | no crash |
| `test_lifted_semantics.py` | do the semantics compute the right values? | `sass.semantics.lifter` + `evaluate` | `refs/`, through the emulator cases |

### Ghidra tests: `uv run pytest -m ghidra`

Need languages built by `uv run sass-build-languages` (outside the sandbox). Run them when the
SLEIGH generator, a lowering or a case changes.

| File | Question | Under test | Oracle |
|---|---|---|---|
| `test_ghidra_disassembly.py` | does the SLEIGH disassemble like pydecode? | `.sla` display | pydecode (ratchet `baseline/ghidra_SMxx.json`; CLI `sass-compare-ghidra`) |
| `test_pcode_dataflow.py` | does the p-code read and write the right registers? | `.sla` dataflow | md roles/spans, BRANCH_TYPE (+ op-count ratchet `baseline/pcode_<variant>.json`) |
| `test_pcode_semantics.py` | does the p-code compute the right values? | `.sla` p-code in Ghidra's emulator | `refs/`, through the emulator cases (CLI `uv run python -m tests.semantics`) |
| `test_fixtures_emulated.py` | do whole GPU-fixture kernels compute their references in the emulator? | emulated SM89 kernels | `refs/`, through the GPU fixtures |
| `test_coverage.py` | is `processor/SASS/semantics/coverage.json` current? | committed coverage | the languages' manifests and `tests/evidence.py`'s evidence |

### GPU tests: `uv run pytest -m gpu --require-gpu`

Need an SM89 device (outside the sandbox), but not Ghidra. Run them only when GPU fixtures or
references are added or changed: they check the references against hardware, and everything
else is checked against the references.

The fixtures compile 100 SM89 CUDA kernels (and patched variants), run them on the GPU, and
execute the same kernel text in Ghidra's p-code emulator (`test_fixtures_emulated.py`): 12,800
random and edge output values, including address setup, constant parameters, memory operations,
predication and loops. Patched fixtures select specific FP16 lane/MMA modes and FMNMX NaN modes;
both the GPU and the emulator execute the resulting cubin. A 64-bit PTX comparison checks the
ptxas ISETP.EX lowering. F2FP fixtures cover packed F16/BF16 RN/RZ, E4M3/E5M2 FP8 packing with
H1 C merges, and E4M3/E5M2 FP8-to-F16 UNPACK_B H0/H1 across all byte encodings. The verified
modes and excluded warp/concurrency operations are in `docs/semantics.md`.

| File | Question | Under test | Oracle |
|---|---|---|---|
| `test_refs_on_gpu.py` | are the references right on hardware? | `refs/`, through the GPU fixtures | SM89 GPU (CLI `uv run python -m tests.observe FAMILY`); records passing fixtures' fingerprints in `baseline/gpu_passes.json`, the source of `gpu` evidence (`tests/evidence.py`, per class and per form: the modifier combinations that ran) |

## Running

```sh
uv run pytest                                          # Python tests, SM89
uv run pytest --arch all --fp all                      # the same on every architecture and lowering
uv run sass-build-languages                            # before the Ghidra tests (outside the sandbox)
uv run pytest -m ghidra --arch all --fp all            # Ghidra tests
uv run pytest -m gpu --require-gpu                     # GPU tests (outside the sandbox)
```

| Option | Meaning |
|---|---|
| `--arch SM89\|SM75,SM90\|all` | architectures (default SM89) |
| `--fp exact\|readable\|all` | floating-point lowering(s) (default exact) |
| `--family F` (repeatable) | limit semantics tests and GPU fixtures to case families (`tests/cases/F.py`) |
| `--exhaustive` | emulate every case of the large F2FP sweeps (the Ghidra tests otherwise run a fixed quarter; the lifted semantics always run all) |
| `--update-baseline` | accept current results into the ratchet baselines |
| `--full` | `test_pydecode_disassembly` and `test_ghidra_disassembly` on the whole real corpus instead of a sample |
| `--require-gpu` | fail instead of skipping without an SM89 device |

While iterating, run what a change touches (`--family conversions --arch SM89`). Before committing,
run the Python and Ghidra tests on every architecture and lowering; run the GPU tests too if
fixtures or references changed.

Rough wall times on a 32-core machine (2026-10-14): Python tests 25 s on SM89 and 6 minutes on
every architecture and lowering; language build 2.5-5 minutes (compilers run as free memory
allows); Ghidra tests on all languages 14 minutes cold and 2.5 minutes when cached (mostly
building the cases); GPU tests 15 s.

**Don't use pytest-xdist.** The expensive tests compute all their results once per session in a
process pool sized to the CPUs, and each test item looks its result up. xdist workers would each
repeat that precomputation with their own pool (32 x 32 processes, load average ~500). The Ghidra
tests likewise run up to 24 JVMs themselves.

## Corpora and current results

The disassembly tests use `nvdisasm -b SMxx` as an oracle over three corpora:

- **real**: every instruction in the cubins inside the CUDA libraries (`cuobjdump -xelf`
  of cuBLASLt, cuSPARSE, ...), about 3–6M unique words per architecture
- **synthetic**: 64 random words per md class that satisfy its encoding constraints
- **mutated**: real words with one or two bits flipped

```sh
uv run sass-compare-pydecode SM89 real 0       # pydecode vs nvdisasm (0 = whole corpus)
uv run sass-compare-ghidra SM89 synthetic 0    # Ghidra (generated SLEIGH) vs pydecode
```

Current results (CUDA 13.4 `nvdisasm`):

- pydecode matches `nvdisasm` on every real word for all eight architectures that have
  cubins (about 38M words), and on all synthetic and mutated words except 17 SM75
  HMMA words. Some valid encodings print as blank lines in `nvdisasm`; the cause is
  still unknown, so those are excluded from the counts.
- The generated SLEIGH matches pydecode on every SM75 and SM89 word in all three corpora.
  The other architectures are still being run.

## Caches

Everything below lives in `.cache/` and is safe to delete.

| What | Keyed by |
|---|---|
| nvdisasm oracle results (`oracle.sqlite`) | nvdisasm hash, word, address |
| parsed md IR (`ir/`) | md files and parser sources |
| real-corpus words (`corpus/`) | the cubin set (paths, sizes, mtimes) |
| Ghidra decode/emulation results (`ghidra/`) | the language's `.sla`, specs and coverage manifest, every `src/sass` source, Ghidra; then the request |
| compiled kernels (`kernels/`), block emulator (`java/`), extracted library cubins (`cubins/`) | sources and flags |

A stale `.sla` fails the Ghidra tests up front (`harness/select.require_languages`): Ghidra would
recompile it on load, and concurrent compilers corrupt it.

## Prerequisites

| What | Default | Override |
|---|---|---|
| target nvdisasm (CUDA 13.4, V13.4.92) | the dev group's `nvidia-cuda-nvdisasm` in `.venv`, `/tmp/.venv` or the uv cache | `NVDISASM` |
| Ghidra 12.1.4 and its pyghidra venv | `~/Ghidra/ghidra_12.1.4_PUBLIC`, `~/.config/ghidra/ghidra_12.1.4_PUBLIC/venv` | `GHIDRA_INSTALL_DIR`, `GHIDRA_PY` |
| CUDA toolkit (GPU-fixture kernels, Ghidra and GPU tests) | `/usr/local/cuda/bin/{nvcc,cuobjdump}` | `NVCC`, `CUOBJDUMP` |
| generated languages | `processor/SASS/data/languages` | `SASS_SLEIGH_OUT` |
| `javac` (cooperative GPU fixtures) | on `PATH` | |

For a scratch build, run `uv run sass-build-languages SM89 --output /tmp/sass-languages` (it
copies the shared definitions), then set `SASS_SLEIGH_OUT=/tmp/sass-languages` for the Ghidra
tests.

## Ratchets

`test_pydecode_disassembly`, `test_ghidra_disassembly` and `test_pcode_dataflow` compare against
committed baselines in `baseline/`: a class that regresses fails, and a newly passing class is
reported until accepted with `--update-baseline`. Review the diff of any baseline update;
op-count increases (`pcode_<variant>.json`) should come only from classes the change touched.

## Writing tests

Each `cases/<family>.py` (listed in `cases/__init__.py` `FAMILIES`) provides:

- `emulator_cases(c)`: single-instruction cases for `test_lifted_semantics` and
  `test_pcode_semantics`. `c.add(cls, fields, registers, expected, ...)` encodes one instruction
  of md class `cls` with operand `fields`, seeds `registers`, and checks `expected`; classes the
  architecture lacks are skipped. Helpers in `harness/case.py`: `put` (place an operand value in
  a register, immediate or constant bank), `cx_source`, `c.kernel(...)` (a one-instruction kernel
  expected to fail), `c.sweep(n)` (indices of a large sweep over codes or values, sampled unless
  `--exhaustive`). Expectation keys are documented in `harness/checks.py`: plain keys are observed
  registers; `__native`, `__max_ops`, `__counter`, `__memory`, `__events`, `__fp8_nan`,
  `__opaque` and `error` are checks.
- `gpu_fixtures()`: `harness/gpu.Fixture`s for `test_refs_on_gpu` and `test_fixtures_emulated`.
  A fixture is a CUDA kernel (`kernels/*.cu`, compiled for sm_89, optionally with patched SASS
  words: `harness/scalar.py`, `harness/templates.py`) and its reference outputs `expected`. Each
  thread reads three inputs and writes one output. The GPU output is compared with `expected`
  using `same`; the kernel, run whole in the emulator from entry to EXIT, is compared with
  `expected` using `emulator_same` (default `same`), with `excused` admitting known emulator
  defects.

References in `refs/` are independent of the code under test: they may import only
`sass.semantics.fp.softfloat` from `sass/` (`test_refs.py` enforces this). Every random block draws
from its own named `random.Random`, so cases are reproducible and adding one block doesn't shift
another.

To derive a reference from hardware, draft a fixture with `expected=None`, run
`uv run python -m tests.observe FAMILY` (`--diff` shows threads that disagree with the current
reference), and write the reference from the observed outputs. A fixture without a reference
fails both fixture tests.

## Multi-thread fixtures

Scalar fixtures emulate every thread alone. `Fixture(cooperative=True, block=N)` emulates each
block as one (`cases/threads.py`, `kernels/threads_semantics.cu`):

- `BlockEmulator.java`, compiled on first use into `.cache/java/`, is a `PcodeEmulator` whose
  threads share every memory space except `localmem`.
- Warps take turns. A turn steps the live lanes at the warp's lowest PC together; they form the
  active mask (guard-predicated lanes excluded). SHFL and VOTE first gather each lane's operand
  into the runtime's warp context.
- `BAR.SYNC` holds a thread until every live thread of the block arrives. Barrier ids and thread
  counts are not modelled; blocks run one after another.

This is one legal interleaving, not the hardware scheduler: it gives the right answer for
correctly synchronized kernels, but doesn't by itself show that a barrier is enforced (see Open in
`AGENTS.md`). Don't create a `tests/java/` directory: on `sys.path` it shadows JPype's `java`
package.

## Comparison tolerances

GPU vs reference is exact, except F16 NaN payload/sign in F16 packs and packed F16 arithmetic,
and the bits outside the converted width of the wide conversion fixtures. Emulator vs reference
tolerates floating-point NaN payload/sign, and excuses Ghidra 12.1.4's minimum-subnormal underflow
(fp16/32/64) only on its exact signature. FP8 emulator cases compare NaN class only (`__fp8_nan`).
