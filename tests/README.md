# Tests

The tests are layered. Each layer checks one artifact against one oracle, so a failure points at
one component. Markers only say what a test needs: `ghidra` (compiled SLEIGH languages and Ghidra's
pyghidra venv) and `gpu` (an SM89 device).

| # | Question | Under test | Oracle | File |
|---|---|---|---|---|
| 0 | helpers right? | refs, softfloat, md expressions, cubin; every family runs on every class it covers | libm / hand values | `test_refs.py`, `test_softfloat.py`, `test_mdexpr.py`, `test_cubin.py`, `test_sem_families.py` |
| 1 | decode like nvdisasm? | `sass.decode.pydecode` | nvdisasm | `test_pydecode_disassembly.py` (CLI `sass-compare-pydecode`) |
| 2 | SLEIGH disassembles like pydecode? | `.sla` display | pydecode | `test_ghidra_disassembly.py` (ratchet `baseline/ghidra_SMxx.json`; CLI `sass-compare-ghidra`) |
| 3 | p-code reads/writes the right registers? | `.sla` dataflow | md roles/spans, BRANCH_TYPE | `test_pcode_dataflow.py` (+ op-count ratchet `baseline/pcode_<variant>.json`) |
| 4a | lifted semantics compute the right values? (no Ghidra) | `sass.semantics.lifter` + `evaluate` on families | `tests/refs/` | `test_sem_semantics.py` (same cases as layer 4) |
| 4 | p-code computes the right values? | `.sla` p-code in Ghidra's emulator | `tests/refs/` | `test_pcode_semantics.py` (CLI `uv run python -m tests.semantics`) |
| 5 | refs and emulator right? | refs + emulated p-code | SM89 GPU | `test_refs_against_gpu.py` (CLI `uv run python -m tests.observe FAMILY`) |

`test_coverage.py` (`ghidra`) checks that the committed `processor/SASS/semantics/coverage.json`
matches the generated languages' manifests.

## Running

```sh
uv run pytest                                          # layers 0, 1 and 4a, SM89 exact
uv run pytest --arch all --fp all                      # the same on every architecture and lowering
uv run sass-build-languages                        # before any Ghidra layer (outside the sandbox)
uv run pytest -m ghidra --arch all --fp all            # layers 2-4 and coverage
uv run pytest -m gpu --require-gpu                     # layer 5 (outside the sandbox)
```

| Option | Meaning |
|---|---|
| `--arch SM89\|SM75,SM90\|all` | architectures for the semantics and Ghidra layers (default SM89) |
| `--fp exact\|readable\|all` | floating-point lowering(s) (default exact) |
| `--family F` (repeatable) | limit semantics and GPU tests to case families (`tests/cases/F.py`) |
| `--update-baseline` | accept current results into the ratchet baselines |
| `--full` | layer 1 on the whole real corpus instead of a sample |
| `--require-gpu` | fail instead of skipping without an SM89 device |

While iterating, run only what a change touches (`--family conversions --arch SM89`); run the full
sweep (all architectures, both lowerings, Ghidra and GPU layers) before committing.

Rough wall times on a 32-core machine: default suite on all architectures about 4 minutes, language
build about 10, Ghidra layers on all languages about 15, GPU layer about 3.

**Don't use pytest-xdist.** The expensive layers (4a, the family crash check, pydecode) compute
all results once per session in a process pool sized to the CPUs, and each test looks its result
up. xdist workers each repeat that precomputation with their own pool (32 x 32 processes, load
average ~500). The Ghidra layers likewise run up to 24 JVMs themselves.

## Prerequisites

| What | Default | Override |
|---|---|---|
| target nvdisasm (CUDA 13.4, V13.4.92) | the cu13 wheel in `/tmp/.venv` or the uv cache | `NVDISASM` |
| Ghidra 12.1.4 and its pyghidra venv | `~/Ghidra/ghidra_12.1.4_PUBLIC`, `~/.config/ghidra/ghidra_12.1.4_PUBLIC/venv` | `GHIDRA_INSTALL_DIR`, `GHIDRA_PY` |
| CUDA toolkit (layer 5 kernels) | `/usr/local/cuda/bin/{nvcc,cuobjdump}` | `NVCC`, `CUOBJDUMP` |
| generated languages | `processor/SASS/data/languages` | `SASS_SLEIGH_OUT` |
| `javac` (cooperative layer-5 fixtures) | on `PATH` | |

Results of the nvdisasm oracle, compiled kernels and the compiled block emulator are cached in
`.cache/`. A stale `.sla` fails the Ghidra layers up front (`conftest.require_languages`): Ghidra
would recompile it on load, and concurrent compilers corrupt it.

## Ratchets

Layers 1-3 compare against committed baselines in `baseline/`: a class that regresses fails, and a
newly passing class is reported until accepted with `--update-baseline`. Review the diff of any
baseline update; op-count increases (`pcode_<variant>.json`) should come only from classes the
change touched.

## Writing tests

Each `cases/<family>.py` (listed in `cases/__init__.py` `FAMILIES`) provides:

- `emulator_cases(c)`: layer 4 and 4a cases. `c.add(cls, fields, registers, expected, ...)`
  encodes one instruction of md class `cls` with operand `fields`, seeds `registers`, and checks
  `expected`; classes the architecture lacks are skipped. Helpers in `harness/case.py`: `put`
  (place an operand value in a register, immediate or constant bank), `cx_source`,
  `c.kernel(...)` (a one-instruction kernel expected to fail). Expectation keys are documented in
  `harness/checks.py`: plain keys are observed registers; `__native`, `__max_ops`, `__counter`,
  `__memory`, `__events`, `__fp8_nan`, `__opaque` and `error` are checks.
- `gpu_fixtures()`: layer 5 `harness/gpu.Fixture`s. A fixture is a CUDA kernel
  (`kernels/*.cu`, compiled for sm_89, optionally with patched SASS words: `harness/scalar.py`,
  `harness/templates.py`) launched on the GPU and run whole in the emulator from entry to EXIT.
  Each thread reads three inputs and writes one output; the GPU output is compared with
  `expected` (the reference) and the emulator output with the GPU.

References in `refs/` are independent of the code under test: they may import only
`sass.semantics.fp.softfloat` from `sass/` (`test_refs.py` enforces this). Every random block draws from its
own named `random.Random`, so cases are reproducible and adding one block doesn't shift another.

To derive a reference from hardware, draft a fixture with `expected=None`, run
`uv run python -m tests.observe FAMILY`, write the reference from the observed outputs, and the fixture
becomes a regression test.

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

GPU vs reference is exact, except F16 NaN payload/sign in F16 packs. GPU vs emulator tolerates
floating-point NaN payload/sign, and excuses Ghidra 12.1.4's minimum-subnormal underflow
(fp16/32/64) only on its exact signature. FP8 emulator cases compare NaN class only (`__fp8_nan`).
