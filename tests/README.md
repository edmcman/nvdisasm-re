# Tests

Each test checks one artifact against one oracle, so a failure points at one component. Tests are
grouped by what they need to run; markers say only that: `ghidra` (compiled SLEIGH languages and
Ghidra's pyghidra venv), `gpu` (an SM89 or SM90 device), `native` (the C++ emulator),
`native_full` (complete C++ input enumerations), and `k` (K Framework 7.1.337).

- [Running](#running): commands, options, what to run when, cost
- [Suites](#suites): what each test file checks, and against what
- [Writing tests](#writing-tests): shared cases, GPU fixtures, references, tolerances
- [Generated shared cases](#generated-shared-cases): the autocase planner and its adapters
- [Sampling](#sampling): how `--sample` selects shared cases
- [Hardware evidence and probes](#hardware-evidence-and-probes)
- [Disassembly corpora and results](#disassembly-corpora-and-results), [Ratchets](#ratchets),
  [Caches](#caches), [Prerequisites](#prerequisites)

## Running

```sh
uv run pytest                                          # Python tests, SM89
uv run pytest --arch all --fp all                      # the same on every architecture and lowering
uv run sass-build-languages                            # before the Ghidra tests (outside the sandbox)
uv run pytest -m ghidra --arch all --fp all            # Ghidra tests
uv run pytest -m gpu --require-gpu                     # GPU tests (outside the sandbox)
uv run pytest -m native --arch SM89,SM90               # the native emulator: C++ runtime, generated code, kernels
nice uv run pytest -m native_full -s                  # complete C++ enumerations, with benchmark progress
nice uv run pytest -m k --arch SM89,SM90 --fp all       # execute and prove the handwritten K IR model
```

| Option | Meaning |
|---|---|
| `--arch SM89\|SM75,SM90\|all` | architectures (default SM89; GPU tests default to device 0's architecture, SM89 or SM90) |
| `--fp exact\|readable\|all` | floating-point lowering(s) (default exact) |
| `--family F` (repeatable) | limit semantics tests and GPU fixtures to case families (`tests/cases/F.py`) |
| `--sample PERCENT` | shared-case input sampling, integer 0–100; default 0 preserves modifier-form coverage and retained regressions ([Sampling](#sampling)) |
| `--exhaustive` | alias for `--sample 100`; cannot be combined with `--sample` |
| `--update-baseline` | accept current results into the [ratchet](#ratchets) baselines |
| `--full` | `test_pydecode_disassembly` and `test_ghidra_disassembly` on the whole real corpus instead of a sample |
| `--require-gpu` | fail instead of skipping without an SM89 or SM90 device |

### What to run when

- **Python tests:** constantly. While iterating, run what a change touches
  (`--family conversions --arch SM89`).
- **Ghidra tests:** when the SLEIGH generator, a lowering or a case changes.
- **GPU tests:** when shared cases, GPU fixtures, references or the GPU harness change.
- **Before committing:** the GPU tests if shared cases, fixtures, references or the GPU harness
  changed. CI runs the Python and Ghidra tests on every architecture and lowering.

GPU tests detect device ordinal 0 before collecting cases; an H100 NVL selects SM90 and
an RTX 4070 selects SM89. An explicit architecture list must include the detected
device; only its architecture runs on hardware. CPU-only tests still default to SM89.
Both shared cases and compiled whole-kernel fixtures use the selected architecture.
Whole-kernel Ghidra tests support `--arch SM89,SM90 --fp all` without a GPU.
Explicit `--arch` selection also limits numeric-definition discovery and the
barrier, async, cache and matrix runtime checks. Without that option, these
checks retain their full architecture domains; `--arch all` runs every domain.
The MUFU sample fixtures compare the same 1,462 recorded inputs per operation on
both devices; their GPU pass records remain architecture-specific.

CI (`.github/workflows/ci.yml`) runs the Python and Ghidra tests on every architecture and lowering
for every push and pull request (the Ghidra tests on a stock Ghidra 12.2, building each
architecture's languages first), and fails when a generated plan (`tests.autocases --check`) or the committed
coverage and evidence are stale.

The K semantics CI job is opt-in. In GitHub's **Actions → CI → Run workflow**, select the
branch and enable **Run K semantics tests and publish the formal definition**, or run
`gh workflow run ci.yml --ref main -f run_k_tests=true`. This runs normal CI plus the K
tests, example proofs and formal-definition artifact generation. Pushes, pull requests
and manual runs with the checkbox left off skip the K job.

### Cost

The ordered-body feasibility checks (2026-11-05) run independently of opcode
family migration:

```sh
nice uv run pytest tests/test_ordered.py tests/test_ordered_formal.py
nice uv run pytest tests/test_ordered_native.py tests/test_ordered_schedule.py -m native
nice uv run pytest tests/test_ordered_ghidra.py -m ghidra
SASS_K_CONTAINER=nvdisasm-re-k:7.1.337 nice uv run pytest tests/test_ordered_formal.py -m k
nice uv run python -m tests.harness.ordered_perf
```

Python/export checks take under a second, native checks about one second with
the small gate library, and the isolated Ghidra language compile/emulation/
decompiler checks take about seven seconds for both FP modes. The combined
Python/export/native/scheduler checks passed 64 tests in 0.93 seconds with warm
caches. Five ordered and existing K contract/branch checks took 26 seconds with
the pinned container already present.

Rough wall times and peak memory (summed PSS) on a 32-core, 93 GiB machine with other work running
(2026-10-16):

| Run | Time | Memory |
|---|---|---|
| Python tests, SM89 | 25 s | 10 GiB |
| Definition discovery, all 10 architectures and both FP modes (2026-10-27) | 111 s; 893 signatures from manual and generated cases | |
| Four regular backend files plus structural checks, GCC (2026-10-27) | 157 s, 509 tests; includes discovery | |
| Same regular checks, Clang (2026-10-27) | 222 s with full enumeration also running; 509 tests | |
| Definition CUDA and existing FP backend checks, SM89 (2026-10-27) | 254 s with full enumeration also running; 3 tests, 893 discovered signatures | |
| Complete C++ enumerations, all-architecture inventory (2026-10-27) | 608 s, 15 tests; 563 domains, 878,099,506,688 comparisons | |
| `test_emulate_cases.py`, SM89 (2026-10-23) | 2 minutes cold (a C++ library per shard), seconds cached | |
| `test_kernels_native.py`, SM89 and SM90 (2026-10-23) | 4 minutes cold (about 1,000 kernels compile), under a minute cached | |
| N7 focused native checks, 529 tests (2026-10-27) | 55 s without competing test jobs; generated-function discovery 13 s | |
| Combined N6/N7 regular checks, 696 tests (2026-10-27) | 187 s; all-architecture manual/generated discovery 122 s | |
| Combined N6/N7 softfloat integration/random-IR/examples, 26 tests (2026-10-27) | 16 s with warm caches | |
| N7 softfloat integration/random-IR/examples, 22 tests (2026-10-27) | 21 s without competing test jobs | |
| Native shared cases, kernels and equivalence, SM89/SM90 and both FP modes, 5,102 tests (2026-10-27) | 4 min 39 s with cold host/generated-call libraries; kernel execution/compilation phase 212 s | |
| Python tests, every architecture and lowering | 3 minutes | 13 GiB (25 GiB with `--exhaustive`) |
| language build (about 17 compilers at once) | 1.75 minutes | 27 GiB |
| Ghidra tests, all languages | 8 minutes cold, 2.5 minutes cached (mostly building the cases); 2 hours cold for `--arch all --fp all` (2.2 million selected cases, load 55-85 from two other suites, 2026-10-22) | 25 GiB |
| Engine browser, Ghidra (2026-10-29) | about 4 s for 45 synthetic fixtures and four imports; about 16 s including the supplied YOLO engine, two real imports and 65 `nvdisasm` checks; language builds separate | |
| GPU tests | 15 s | |
| K IR execution/proof tests, SM89/SM90 and both FP modes (2026-10-26) | about 4 minutes with base definitions cached; LLVM/Haskell compilation adds a cold setup cost | |

These historical timings precede the current shared-case workload. The
[2026-10-26 partial-form report](../docs/partial-form-planning-2026-10-26.md)
records affected-family validation and an isolated planner/evidence comparison.
The selected SM89 GPU suite took 222 seconds alongside two software suites;
that includes CPU case preparation and is not an isolated hardware timing.

The ordered-IR source-frozen full Python run (2026-11-06) passed 15,759 tests
with two skips and 973,986 selected shared cases in 2,768 seconds. Profiling
identified shared-case preparation, all-language definition discovery, and cold
native matrix/synchronization library rebuilds. This records that run's scope
and costs; it does not replace the historical timing baselines.

After the constant LOP3 dependency and half-FMA `.RELU` fixes, focused working-source
checks passed 252 tests in 12.6 seconds. Affected-family Python checks passed
2,760 tests over 209,634 shared cases in 94.6 seconds; native checks on SM89/SM90
and both modes passed 708 tests in 75.7 seconds. Existing SM89 hardware cases for
the same families passed 12,412 scenarios (174 test items) in 64 seconds, with
two scenarios remaining emulator-only. These are regression checks, not new
opcode-discovery evidence.

Ghidra results are cached per program by the sources it imports (`ghidra_bridge.sources`), so an
edit elsewhere in `src/sass` keeps them. Generator modules (families, operations, operands,
lowering, build) reach Ghidra only through the compiled language, so editing them recomputes only
languages whose `.sla` changes; `test_ghidra_bridge` checks the emulator never loads them.

Since 2026-10-20 the fp32 family alone has about 680,000 cases per language. `test_lifted_semantics`
used to build every selected language's cases in the pytest process and fork its pool from it: the
fp32 family on every architecture and lowering made a 12.5 GB parent, and its 32 copy-on-write
workers exhausted 93 GiB. It now streams instead: shards of about 1,000 planned forms (plus each
family's manual cases) are generated, judged and discarded inside `forkserver` workers, one pool per
architecture, and only per-class summaries come back (`tests.cases.stream`/`shards`). That run takes
19 minutes and 14 GiB, mostly the workers' bounded lift caches. A script that starts these pools
needs an `if __name__ == "__main__":` guard, since `forkserver` workers re-import `__main__`.

**Don't use pytest-xdist.** The expensive tests compute all their results once per session in a
process pool sized to the CPUs, and each test item looks its result up. xdist workers would each
repeat that precomputation with their own pool (32 x 32 processes, load average ~500). The Ghidra
tests likewise run up to 24 JVMs themselves.

## Suites

### Python tests: `uv run pytest`

Need Python and the target nvdisasm only. On a CI runner the real corpus is empty (no CUDA
libraries), so `test_pydecode_disassembly` checks synthetic and mutated words only.

| File | Question | Under test | Oracle |
|---|---|---|---|
| `test_refs.py`, `test_softfloat.py` | are the references right? | `refs/`, softfloat | libm, brute force, hand values |
| `test_mdexpr.py`, `test_fold.py`, `test_cubin.py` | are the helpers right? | md expressions, decode-time folding, cubin reader | hand values, evaluation |
| `test_assemble.py` | does source encode correctly and produce native cubins? | FORMAT parser, strict encoder, labels, directives, ELF writer | decoder round trips, nvdisasm, known words and metadata |
| `test_pydecode_disassembly.py` | does pydecode decode like nvdisasm? | `sass.decode.pydecode` | nvdisasm (ratchet `baseline/SMxx.json`; CLI `sass-compare-pydecode`) |
| `test_bitvec.py`, `test_control_flow.py` | are the IR and lifted control flow right? | `sass.semantics.bitvec`, lifted branch conditions | explicit bit strings, hand cases |
| `test_build.py` | does coverage collection leave the generated languages alone? | `sass.sleigh.gen` with and without coverage | the same build without coverage |
| `test_family_lowering.py` | does every family lower on every class it covers? | `sass.semantics` families under the SLEIGH enumeration | no crash |
| `test_lifted_semantics.py` | do the semantics compute the right values? | `sass.semantics.lifter` + `evaluate` | `refs/`, through the emulator cases |
| `test_mufu.py` | does the MUFU runtime match hardware? | the C++ MUFU model (`include/sass/mufu.hpp`), the runtime's MUFU primitive | `refs/mufu.py`, recorded SM89 outputs |
| `test_emulate_fp.py`, `test_emulate_primitives.py` (`-m native`) | do both explicit CPU floating-point backends and the primitives compute the IR? | `softfloat.hpp`, `fp_host.hpp`, `primitives.hpp` | `bitvec.OPS` (Python softfloat), `refs/primitive.py` |
| `test_fp_backends.py` (`-m native`) | do selection, environment rejection/restoration and FP16 round-to-odd work? | `fp.hpp`, host FMA, library loading | Python softfloat, exact residual/odd assertions, isolated subprocesses |
| `test_definition_functions.py` (structural, `-m native`, `-m gpu`) | do discovered definitions expand over parameters and compile as pure functions? | `sass.emulate.definitions`, generated host/softfloat/CUDA functions, arbitrary arity and 128-bit values | Python `evaluate`, then compiled softfloat |
| `test_native_full.py` (`-m native_full` only) | do complete small input domains agree between C++ backends? | FP16 add/multiply, conversions, discovered definitions with at most 32 operand bits | compiled softfloat against host, enumeration counts and endpoints |
| `test_emulate_ops.py` (`-m native`) | does the generated C++ compute the IR? | `sass.emulate.cxx` on random unsimplified trees over every operator | `evaluate` |
| `test_native_definitions.py` (`-m native`) | do generated calls preserve pre-state, guards, laziness and instruction faults? | `codegen` fragments/kernels, function collection, 128-bit values and overloads | `evaluate`, inline lowering, SM89/SM90 and both FP modes |
| `test_emulate_cases.py` (`-m native`) | does the generated C++ compute the right values? | `sass.emulate.cxx` fragments, a library per shard | `refs/`, through the emulator cases (not `__events`) |
| `test_kernels_native.py` (`-m native`) | do whole kernels compute their references natively? | `sass.emulate.kernel` (model 1 non-cooperative, model 2 cooperative fixtures; `c.kernel` cases) | `refs/`, through the GPU fixtures; the scheduler's errors |
| `test_equivalence.py` (`-m native`) | does equivalence testing find exactly the differing threads? | `sass.emulate.equivalence` | a kernel against itself and a NOP-patched copy |
| `test_autocases.py`, `test_*_autocases.py`, `test_half_comparison_profiles.py` | are generated cases reproducible and reference-backed? | the [planner and adapters](#generated-shared-cases) | saved plans, independent references |
| `test_sampling.py`, `test_evidence.py`, `test_device_cases.py` | do sampling, evidence and GPU-probe construction behave? (no GPU needed) | `harness/sampling.py`, `tests/evidence.py`, `harness/device_*.py` | hand cases, mocked runners |
| `test_export_cases.py` | is the [shared-case export](#exporting-shared-cases) complete and decodable? | `tests/export_cases.py` | `cases.emulator_cases`, pydecode |
| `test_k_export.py` | does the K export preserve executable IR and identify its assumptions? | formal exporter and package data | explicit structures and decoder |
| `test_k_semantics.py` (`-m k`) | do the handwritten K rules execute the IR correctly and prove their example claims? | K bitvectors, IEEE arithmetic and state transitions | Python evaluator, shared cases, passing and deliberately false claims |
| `test_coverage_comment.py` | does CI's PR coverage comment compare and post correctly? | `.github/scripts/coverage_comment.py` | mocked GitHub API |

### Ghidra tests: `uv run pytest -m ghidra`

Need languages built by `uv run sass-build-languages` (outside the sandbox).

| File | Question | Under test | Oracle |
|---|---|---|---|
| `test_ghidra_disassembly.py` | does the SLEIGH disassemble like pydecode? | `.sla` display | pydecode (ratchet `baseline/ghidra_SMxx.json`; CLI `sass-compare-ghidra`) |
| `test_pcode_dataflow.py` | does the p-code read and write the right registers? | `.sla` dataflow | md roles/spans, BRANCH_TYPE (+ op-count ratchet `baseline/pcode_<variant>.json`) |
| `test_pcode_semantics.py` | does the p-code compute the right values? | `.sla` p-code in Ghidra's emulator | `refs/`, through the emulator cases (CLI `uv run python -m tests.semantics`) |
| `test_fixtures_emulated.py` | do whole GPU-fixture kernels compute their references in the emulator? | emulated SM89/SM90 kernels | `refs/`, through the GPU fixtures |
| `test_coverage.py` | is `processor/SASS/semantics/coverage.json` current? | committed coverage | the languages' manifests and `tests/evidence.py`'s evidence |
| `test_ghidra_bridge.py` | does the Ghidra cache key cover what the emulator loads? | `ghidra_bridge.sources` | the modules loaded in a Ghidra emulation |

A stale `.sla` fails the Ghidra tests up front (`harness/select.require_languages`): Ghidra would
recompile it on load, and concurrent compilers corrupt it.

### GPU tests: `uv run pytest -m gpu --require-gpu`

Need an SM89 or SM90 device (outside the sandbox), but not Ghidra.

| File | Question | Under test | Oracle |
|---|---|---|---|
| `test_cases_on_gpu.py` | do shared instruction cases agree with hardware? | `refs/`, through generated native probes | SM89/SM90 GPU; what each scenario returned is recorded in `baseline/gpu_observations/SMxx.jsonl` |
| `test_refs_on_gpu.py` | are the references right on hardware? | `refs/`, through the GPU fixtures | SM89/SM90 GPU (CLI `uv run python -m tests.observe FAMILY`); records passing fixtures' fingerprints in `baseline/gpu_passes.json`, which with the shared-case observations is the source of `gpu` evidence (`tests/evidence.py`, per class and per form: the modifier combinations that ran) |
| `test_assemble.py::test_native_cubin_on_gpu` | do native cubins load and execute? | self-contained kernels assembled without compiled templates | SM89/SM90 Driver API: parameters, branches, constants, shared memory, multiple entries |
| `test_fp_cuda.py` | do all CUDA floating-point templates compile and return exact results? | `fp_cuda.hpp`, shared conversion and scaled-operand helpers | Python softfloat across widths/modes, exceptional values, subnormals, scaled multiply and conversion pairs |

`test_cases_on_gpu.py` runs the hardware-valid cases from `emulator_cases(c)` directly: encoded
words, seeded registers, constants and independent reference outputs are shared with the Python
and Ghidra tests. No CUDA compiler is needed for these generated probes.

- Native cubin wrappers initialize state, execute the original instruction encoding (with
  dependency-safe scheduling), and capture registers, predicates and memory.
- Scalar cases are batched; uniform state is isolated between scenarios, and warp contexts become
  actual lane inputs and active masks.
- A warp-context branch (BRA.DIV/.CONV) records its landing PC like the branch adapter; its
  inactive lanes either all exit or, with the context's `waiting` mask, all wait at a
  `BSSY`/`BSYNC` barrier.
- Memory adapters bind logical addresses to real allocations and runtime shared/local windows.
  Generic LD without `.E` uses a registered low-address host arena on Linux x86-64, requiring
  CUDA's device alias to equal the CPU pointer and fit in 32 bits. Address binding respects
  encoded register widths and 32-bit LD address arithmetic; adjacent registers retain their inputs.
  An unavailable low device alias is a runner failure rather than a truncated high pointer.
- Each invocation executes the selected tests, even when passing evidence exists.
- The terminal summary reports the sampling percentage, passed, failed and emulator-only
  scenarios, including exclusion reasons. Emulator errors, runtime events, impossible synthetic
  state and reserved launch constants remain emulator-only. Unknown case state fails
  classification; an adapter failure is a test failure.
- Worker processes time out after 30 seconds; device faults are bisected to individual cases in
  fresh CUDA contexts.

The CUDA and patched whole-kernel fixtures (`test_refs_on_gpu.py`) are integration tests: they
cover address setup, constants, memory operations, predication, loops and cooperative blocks, and
run both on hardware and in Ghidra (`test_fixtures_emulated.py`).

The backend tests preserve shared instruction evidence cases and do not create GPU opcode
evidence. For the focused A.4 backend checks, use:

```sh
nice uv run pytest -m native tests/test_emulate_fp.py tests/test_fp_backends.py
nice uv run pytest -m native tests/test_emulate_fp.py --exhaustive
nice uv run pytest -m gpu --require-gpu tests/test_fp_cuda.py  # outside the sandbox
```

`test_emulate_fp.py` compares both `native.fp` (softfloat) and `native.fp_host` with Python
softfloat across all widths/modes, including same-width conversions and scale boundaries
−512/512, −513/513, −1024/1024 and −2048/2048. Exhaustive runs enumerate every FP16 source
encoding for conversions, both signs of every finite half conversion midpoint and its
adjacent FP32/FP64 source encodings, and increase the random arithmetic corpus fivefold.
Scaled-product cases move exact products across overflow, normal/subnormal and
zero-rounding boundaries, including extreme signed-int scales. The CUDA smoke kernel
instantiates every template and checks device selection even with `SASS_FP_SOFT` defined;
its exhaustive corpus includes the same half conversion boundaries and a larger half FMA
sample; its build flags retain subnormals and disable contraction. Host environment tests isolate
MXCSR mutation and caller rounding-mode checks in subprocesses, preserving the pytest process.
See [backend requirements](../docs/emulation.md#floating-point-backends-2026-10-27).

On 2026-10-27, the initial exhaustive backend, generated-operator, primitives,
examples and selected FP64 SM89/SM90 native-emulator run passed 472 tests in 109 s
from a cold worktree cache. After removing the accelerated backends' softfloat dependencies,
Clang's complete exhaustive operator/environment checks passed 488 tests in 82 s.
After the final host tiny-result optimization, exhaustive scaled-product, multiply, FMA
and rounding checks passed 343 tests in 35 s; explicitly selecting `CXX=g++` passed
513 default-corpus backend, generated-operator, primitives and example tests in 39 s.
The two exhaustive CUDA selector configurations passed on SM89 in 46 s, including
compilation; the final focused selector checks passed in 10 s. The default compiler
in this checkout was Clang, so GCC must be selected explicitly to reproduce its run.
These results validate the backend operators; CUDA verification on SM90 remains separate.

N7 validation (2026-10-27) switches the native default to the host backend and generated
operator calls. Before integrating N6's expanded inventory, the focused suite below passed
529 tests in 55 s without competing test jobs.
The same run took 180 s alongside cold kernel compilation: discovery alone rose from 13 s
to 55 s and returned to 13 s in the quiet repeat. This is compilation contention, not a new
validation baseline. The SM89/SM90 shared-case, whole-kernel and equivalence run passed
5,102 tests in 279 s in both FP modes, including cooperative fixtures and expected faults.
The softfloat override passed 22 integration, random-IR and example tests in 21 s without
competing test jobs (84 s alongside cold kernel compilation). Definition and generated-function
structural checks passed 159 tests in 20 s, including the 13 s manual-case discovery phase.
After integrating N6, the combined regular suite passed 696 tests in 187 s. Its discovery
took 122 s because it includes generated cases on all ten architectures and both FP modes,
rather than the earlier manual-only SM89 inventory. The shared helper explicitly selects
each backend, and the complete-domain runner includes both backend headers; a regular
regression compiles that runner and checks its first and last FP16 arithmetic/conversion chunks.
The softfloat override passed 26 integration, random-IR and example checks in 16 s with warm
caches after the combined run.

```sh
nice uv run pytest -m native tests/test_native_definitions.py tests/test_fp_backends.py \
    tests/test_emulate_fp.py tests/test_definition_functions.py tests/test_emulate_ops.py \
    tests/test_emulate_primitives.py tests/test_examples.py
nice uv run pytest -m native tests/test_emulate_cases.py tests/test_kernels_native.py \
    tests/test_equivalence.py --arch SM89,SM90 --fp all
SASS_CXXFLAGS=-DSASS_FP_SOFT nice uv run pytest -m native tests/test_native_definitions.py \
    tests/test_emulate_ops.py tests/test_examples.py
```

Backend-selection tests run isolated subprocesses with the actual default or environment
override, check the selected namespace and strict flags, and verify distinct cache paths.
`native.fp` remains explicitly bound to softfloat. `definition_calls=False` on the generators
or their `Fragments`/`Kernel` wrappers selects inline expansion.
The reproducible four-way benchmark is
[`research/native_backend_benchmark.py`](../research/native_backend_benchmark.py);
its methodology and recorded results are in [emulation.md](../docs/emulation.md).

### Generated definitions and complete C++ enumerations (2026-10-27)

Definition discovery streams manual and generated shared cases on every architecture in
`sass.sleigh.build.ARCHES`: SM75, SM80, SM86, SM89, SM90, SM100, SM101, SM103, SM107
and SM120, in both exact and readable modes, always at the existing default sample. Workers
deduplicate words within each shard and return a representative instruction and result
width for each label/signature. The parent lifts representatives to register expansions,
checks widths across streams, and discovers once per test session. Structural tests check
label grammar, parameter expansion and purity, retaining rejection, overload and wide-value
regressions. The ordinary backend corpus includes signed zeros, exceptional values,
subnormals, conversion midpoints, cancellation, arithmetic ties and seeded random tuples.
Softfloat functions compare with Python `evaluate`; host and CUDA functions compare with
the compiled softfloat functions. Compilation batches contain at most 48 definitions.
CUDA dispatch sizes rows to the batch's argument count and carries every operand/result
as two 64-bit limbs, including a five-argument regression with two 128-bit inputs.

On 2026-11-04, isolated `test_real_expansions_accept_parameters` runs took 324 seconds
before packed F16 HMMA outputs and 378 seconds afterward. Discovery setup accounts for
323 and 377 seconds respectively; parameter validation itself takes under a second.
The additional output form and boundary profiles explain the 17% increase. Matrix-only
discovery on SM89/SM90 increased from 21–22 seconds to 28–30 seconds. Running full
discovery alongside coverage refresh and Ghidra took 607 seconds; that contended run
is not the standalone timing baseline.

Adding packed `1688` forms took 465 seconds for discovery and under a second for
parameter validation, with YOLO replay briefly running alongside it. An isolated
SM89 phase comparison against the pushed commit found no regression on the same
7,108 cases: 28.0 seconds before, 27.4 seconds afterward. The expanded 9,964-case
profile took 38.7 seconds: generation increased from 13.9 to 19.0 seconds, lifting
from 14.1 to 19.6 seconds, and definition traversal remained below 0.05 seconds.
Use roughly eight minutes for the expanded full discovery; the 465-second
measurement is not an isolated all-architecture baseline.

The E6M9 extension (2026-11-04) passed 216 matrix/generator checks in 557 seconds
and 82 native checks in 233 seconds while rebuilding native fragments, languages
and case plans. Those contended measurements are not new standalone baselines.
A quiet comparison on the same 9,964 cases took 40.6 seconds on `4e99cda` and
40.0 seconds afterward. The expanded 11,844-case profile took 55.7 seconds:
generation 28.7 seconds, lifting 26.9 seconds, and traversal under 0.1 seconds.
Real parameter discovery and validation took 511 seconds. Matrix-only validation
in all twenty Ghidra languages took 866 seconds, including 180,000 shared cases,
120 whole-kernel fixture tests and full dataflow checks. Plan about fifteen minutes
for that run; its earlier 8–10 minute estimate was too low.

The B2R extension (2026-11-04) passed 1,414 lifted checks and 6,236 shared control
cases in 16 seconds. The broader Python/native barrier, reconvergence and matrix
run took 710 seconds; six async schedule checks took 200 seconds. All-architecture
exact/readable Ghidra control validation took 1,021 seconds, passing 2,008 tests and
6,336 shared cases before updating twenty WARP-mask operation-count baselines.
These runs overlapped other validation and are not isolated timing baselines.
Plan about 2–4 minutes for the focused final barrier Ghidra checks and about
twenty minutes for the broader control run.

The final barrier Ghidra and coverage checks took 323 seconds after changing the
ABI snapshot flag: callback libraries rebuilt, and definition discovery competed
for CPU time. Allow 6–8 minutes with those cold libraries and concurrent discovery.
The twenty operation-count checks passed without updates in nine seconds.

Repeated reduction kernels revealed unnecessary barrier snapshots in ordinary
instructions. Snapshots now occur only for B2R BAR readers. On identical requests,
with CPU affinity fixed and process CPU time measured, 100 Python repetitions took
29.01 seconds before and 29.53 afterward; 1,000 native repetitions took 1.68 and
1.46 seconds. Competing jobs explained the larger initial wall-clock difference.

```sh
nice uv run pytest tests/test_definition_functions.py
CXX=g++ nice uv run pytest -m native tests/test_definition_functions.py tests/test_emulate_fp.py tests/test_fp_backends.py tests/test_emulate_ops.py
CXX=clang++ nice uv run pytest -m native tests/test_definition_functions.py tests/test_emulate_fp.py tests/test_fp_backends.py tests/test_emulate_ops.py
nice uv run pytest -m gpu --require-gpu tests/test_definition_functions.py tests/test_fp_cuda.py  # outside the sandbox
nice uv run pytest -m native_full -s
```

`native_full` is registered separately and excluded by default; `-m native` also omits
it. Marker selection matches whole names so `-m native_full` does not trigger expensive
native-emulator collection. There is no new CLI option. `--exhaustive` and shared-case
sampling retain their existing meanings; complete backend enumerations always enumerate
their full domains.

The full suite compares all 2³² FP16 operand pairs for add and multiply in RN/RZ/RP/RM,
all 2¹⁶ FP16 encodings widened to FP32 and FP64, all 2³² FP32 encodings narrowed to FP16
in each rounding mode, and every tuple of each discovered definition whose operands sum
to at most 32 bits. The FP domains alone contain 51,539,738,624 comparisons. Comparisons
run inside compiled C++ loops, in chunks of at most 2²⁴ tuples, using constant storage per
worker rather than input/result arrays. Operand zero varies fastest. Each operation checks
its exact count and both domain endpoints; failures report the operation/mode, signature,
input tuple and both results, including high limbs.

Before the remaining chunks start, the full runner benchmarks non-overlapping chunks spread
across the domain at the same concurrency used by the full enumeration. It reports the
measured estimate and completed count/time with `-s`. The initial single-thread estimate
divided by 32 overestimated scaling on the 24-core/32-thread hybrid CPU; the parallel
benchmark avoids that assumption. Volatile local results keep each comparison observable,
including when the generated integer bodies are identical, so the compiler cannot replace
enumeration with a proof of equality.

These tests establish backend implementation agreement. SM89 CUDA results validate these
generated functions on SM89; they do not add instruction-form GPU evidence on other
architectures. Autocase plans, references, sampling rules and opcode evidence are unchanged.

The all-architecture regular run used `-m 'not gpu and not native_full'` on those same four
files to include the structural checks as well: GCC 11.4 passed 509 tests in 156.98 s,
Clang 14 passed 509 in 221.87 s. Discovery took 111.43/110.23 s respectively. Clang shared
the CPU with full enumeration; its generated softfloat/host checks took 39.92/29.60 s,
versus GCC's 21.49/10.20 s. This timing difference includes contention and new compilation
and is not an isolated compiler-performance comparison.

The expanded CUDA run passed all 893 discovered signatures plus the synthetic arity/width
regressions on SM89, alongside both existing FP CUDA backend configurations: 3 tests in
254.22 s. Discovery took 192.45 s while full enumeration competed for CPU; generated
function compilation and execution took 40.14 s. Earlier batches were cached, while the
additional architectures introduced new batches. This is a partially cached timing.

The complete all-architecture suite passed 15 tests in 607.61 s with Clang: 563 domains
(14 direct FP domains and 549 generated definitions), totaling 878,099,506,688 comparisons.
Discovery took 119.19 s; generated-definition enumeration and compilation took 401.86 s.
This used cached libraries from the earlier four-architecture inventory and compiled the
additional batches, while the regular Clang and CUDA checks overlapped parts of the run.
The earlier four-architecture full run completed 240 domains and 627,069,557,760
comparisons in 398.62 s. Expanding discovery added 323 eligible domains; the longer
run reflects that extra work and concurrent validation. Representative parallel chunk
benchmarks predicted about 8–10 s for complete FP16 arithmetic/min/max domains and
about 1–2 s for simple integer domains; completed timings stayed near those estimates.

Compilation caches are keyed by the source, headers, compiler identity and flags. A fresh
Clang compile/load of the representative FP16-add enumeration library took 0.6854 s;
loading that same library from cache took 0.0005 s. Enumeration results are not cached:
every invocation compares the complete input domains again. Discovery also runs once in
each pytest session even when the C++ libraries are cached.

## Writing tests

Each `cases/<family>.py` (listed in `cases/__init__.py` `FAMILIES`) provides `emulator_cases(c)`
and, optionally, `gpu_fixtures()`.

### Shared cases: `emulator_cases(c)`

Single-instruction cases for `test_lifted_semantics`, `test_pcode_semantics` and
`test_cases_on_gpu` (hardware expectations use exact mode).

`c.add(cls, fields, registers, expected, ...)` describes one instruction of md class `cls` with
operand `fields` (encoded by `c.complete()`, which the loader `cases.emulator_cases` runs on
the selected cases; an expected value may be `Later(fn, *args)`, computed there too, so a sweep
computes references only for the cases it keeps), seeds `registers`, and checks `expected`; classes the architecture lacks are skipped.
Helpers in `harness/case.py`:

- `put`: place an operand value in a register, immediate or constant bank
- `cx_source`: a bindless `cx[URx][...]` source, with its header in a uniform register pair and
  the loaded word mapped in memory
- `c.kernel(...)`: a one-instruction kernel expected to fail
- `c.sweep(n)`: all indices of a domain, like `range(n)`; sampling happens after generation and
  validity filtering

Expectation keys are documented in `harness/checks.py`: plain keys are observed registers;
`__native`, `__max_ops`, `__counter`, `__memory`, `__events`, `__fp8_nan`, `__opaque` and `error`
are checks.

Ordinary scalar cases automatically run on the GPU:

- `hardware={"adapter": ...}` binds memory, warp, branch or special-register scenarios; the family
  suites demonstrate these adapters.
- `hardware={"skip": "reason"}` explicitly marks model-only state.
- `__native`, `__max_ops` and runtime checks remain emulator assertions. Readable-mode
  approximations never become hardware expectations.
- The runner retains input/output register aliases and compares all expected registers and
  observed memory.

`@retained(reason)` and `@anchored(reason)` (`harness/manual.py`) control how manual cases are
sampled ([Sampling](#sampling)).

### Exporting shared cases

`python -m tests.export_cases SM80 [--generated] [--family F ...] [--fp readable] [--sample PERCENT | --exhaustive] -o cases.jsonl.gz`
writes one language's shared cases as JSON lines, for consumers outside this repository (#50). Each line
has `id` (`Case.id`), `arch`, `fp`, `family`, `class`, the case's request keys (`word` in hex,
`registers` and `memory` (`cbank`/`ram`) to seed, `observe`, and `observe_memory`, `context` or
`kernel`/`output` when present), `expected` (observed registers plus the `__` checks of
`harness/checks.py`) and `hardware` (the GPU directive, or null). Decode a word with
`pydecode.decode(arch, bytes.fromhex(word))`; its class is the line's, except generated
`imad__RsIR_RIR` words that print as their `IMAD.MOV`/`IMAD.IADD` alias (`imad_pseudo__RsIR_RIR`).
`--generated` adds the saved plan's generated cases, built in sharded workers like the streamed
suites. SM80 at the default sample: 15,325 manual cases (10 s), or 123,644 with `--generated` (26 s,
2.5 MB gzipped; 2026-10-23).

### GPU fixtures: `gpu_fixtures()`

`harness/gpu.Fixture`s for `test_refs_on_gpu` and `test_fixtures_emulated`. A fixture is a CUDA
kernel (`kernels/*.cu`, compiled for the selected architecture, sm_89 or sm_90, optionally with patched SASS words:
`harness/scalar.py`, `harness/templates.py`) and its reference outputs `expected`. Each thread reads
three inputs and writes one output. The GPU output is compared with `expected` using `same`; the
kernel, run whole in the emulator from entry to EXIT, is compared with `expected` using
`emulator_same` (default `same`), with `excused` admitting known emulator defects.

`Fixture.arguments` adds `(size, value)` parameters after the input/output pointers.
Values are unsigned integers packed little-endian, or exact-size `bytes`, `bytearray` or
`memoryview` payloads, including aggregates. Pack signed integers and floating-point values
explicitly with `struct.pack`. Both launch paths validate argument counts, sizes and values
against their cubin's metadata; the GPU runner validates before allocating device resources.

Patched carry-kernel slots (`templates.slot_fixture`) must leave UR4/UR5 alone: the kernel loads
its default memory descriptor there (`ULDC.64 UR4, c[0x0][0x118]` at 0x30) and its final `STG.E`
uses it implicitly, so a value with bit 27 set in UR5 (descriptor bit 59) traps with
CUDA_ERROR_ILLEGAL_INSTRUCTION (715).

Prefer shared cases for single-instruction behavior: the same case then runs in the emulator,
Ghidra and on the GPU, and creates evidence.

#### Multi-thread fixtures

Scalar fixtures emulate every thread alone. `Fixture(scope="block", block=N)` emulates each
block as one (`cases/threads.py`, `kernels/threads_semantics.cu`):

- `BlockEmulator.java`, compiled on first use into `.cache/java/`, is a `PcodeEmulator` whose
  threads share every memory space except `localmem`.
- Warps take turns. A turn steps the live lanes at the warp's lowest PC together; they form the
  active mask (guard-predicated lanes excluded). SHFL and VOTE first gather each lane's operand
  into the runtime's warp context.
- `BAR.SYNC` holds a thread until every live thread of the block arrives. Barrier ids and thread
  counts are not modelled; blocks run one after another.

`Fixture.grid` and `Fixture.block` accept integers or one-to-three-element tuples/lists
(omitted axes are 1). Explicit geometry must match the input record count; GPU fixtures
require complete CUDA blocks, including blocks with a partial final warp. Geometry and
argument validation precede allocation. `launch_parts` supplies seven positional
items; `request` forwards `grid` separately. Explicit grid geometry enters the fixture
fingerprint, while existing default fingerprints retain their format.

`kernels/launch_geometry.cu`, `harness/launch_geometry.py` and the independent
`refs/launch.py` cover 2D/3D coordinates and dimensions, lane IDs, and four-block
cooperative launches with block sizes 1, 17, 33 and 45. `ptxas` removes a converged
`__syncwarp` as NOP; the fixture replaces that NOP with WARPSYNC to exercise the
scheduler and hardware barrier. `test_launch.py` checks the Python engine and validation;
`test_launch_backends.py` runs the same references in native/Ghidra and on the GPU.

This is one legal interleaving, not the hardware scheduler: it gives the right answer for
correctly synchronized kernels, but doesn't by itself show that a barrier is enforced (see
`docs/open.md`). Don't create a `tests/java/` directory: on `sys.path` it shadows JPype's `java`
package.

### References

References in `refs/` are independent of the code under test: they may import only
`sass.semantics.fp.softfloat` from `sass/` (`test_refs.py` enforces this). Every random block draws
from its own named `random.Random`, so cases are reproducible and adding one block doesn't shift
another. To derive a reference from hardware, see
[Hardware evidence and probes](#hardware-evidence-and-probes).

### Comparison tolerances

Shared instruction cases compare GPU and emulator register bits exactly, including
ordinary HADD2/HMUL2/HFMA2's canonical `0x7fff` NaN lanes in exact mode.
FP8 cases may compare NaN class only (`__fp8_nan`). Readable arithmetic profiles
skip NaN payload checks. Whole-kernel fixtures declare their own `same` and
`emulator_same` policies; wide conversion fixtures may mask bits outside the
converted width, and arithmetic fixtures excuse Ghidra's minimum-subnormal
underflow (fp16/32/64) only on its exact signature.

## Generated shared cases

### Running the planner

```sh
uv run python -m tests.autocases SM89 --write --report tests/cases/generated/SM89.gaps.json
```

reads the exact-language coverage manifest, expands its legal modifier combinations (including the
factored free modifiers), and selects a deterministic constrained three-way suite before writing
`cases/generated/SM89.json`. A requirement is a tuple with a complete legal, modelled extension;
an adapter must successfully construct that extension before it counts as covered. Declared
operand-row adapters can target native instructions within partially native forms; this does
not change the class's support classification. If manifests are missing, first
run `uv run sass-build-languages SM89 --fp exact --generate-only --coverage`. The committed
`cases/generated/SMxx.gaps.json` are `--report` outputs.

On SM89 (2026-10-24) the manifest takes 35 s and the planner 3 min 5 s on one core: 80% of that is checking
1.15M candidate words by encoding and decoding each (`harness/autocases.checked_word`). All ten
architectures take about 35 minutes.

- `--manifest-dir DIR` selects another manifest directory.
- Omit `--write` for a dry run.
- `--check` fails if regeneration would change the saved plan.
- Rerunning replaces the plan deterministically; prior generated cases are excluded from the
  comparison.

Plans are committed for all ten supported architectures; regenerate each affected plan. After
changing a plan or adapter, run the shared Python, Ghidra and GPU tests and
`uv run sass-refresh-coverage`. Generating a plan gives no GPU evidence; only an actual passing
hardware run does.

### Plans, profiles and retained cases

Version 3 plans record the verification policy and each form's `profile_mode`.
`sass.sleigh.verification.POLICY` defaults to strength 3 for every opcode. Its
`VerificationPolicy(default_strength=3, opcode_strength={"OPCODE": 4})` constructor supports
positive integer strengths and `"exhaustive"` per-opcode overrides; use the shared policy
when generating plans. Strength is capped at the number of varying modifiers. A class with
no varying modifiers still has one anchor requirement.

The variants added by I2I.SAT, SM75 LDS.U and LD/LDG prefetch hints use the normal
three-way policy. Tests require each new size, source/destination form and prefetch
label to have generated coverage. LDG profiles observe the predicate result,
including an old true output and an alias with the zero-fill input. Ordinary
completed loads clear that output (SM89 probes, 2026-10-25). Dual register forms
and reserved memory controls remain explicit adapter gaps. The normal planner also targets
declared operand rows in partially native forms, including aligned SM75 LDS.U pairs/quads.

Partial-form planning (2026-10-26, #80): `harness/autocase_partial.py` declares eligible
operand rows. They include aligned memory pairs/quads and constant loads, full-mask MOV,
PT/UPT predicate-discarding FLO/UFLO, R2UR, ordinary IADD, VABSDIFF4 and VIMNMX, HADD2 F32
widening, and supported HFMA2/HFMA2.MMA literal arithmetic. Actual words keep inspected
native/effect guards; an opaque fallback cannot pass. Unresolved rows remain adapter gaps.
The generated wide-memory profiles contain distinct high words. Migrated ordinary manual
cases are removed; focused cost, constant-predicate, window/descriptor and fallback regressions
remain. `PYTHONPATH=src:. python research/partial_form_audit.py` reports every partial native
class's selected forms and blockers. Modifier combinations decoded from one table field remain
correlated, so encoding aliases cannot inflate the inventory or receive false provenance.

Verification update (2026-10-25): the regenerated plans and aligned memory
profiles were rerun on SM89. On 2026-10-28 the sampled SM90 GPU suite reran
the current selection on an H100 NVL. Relative to the previous recorded evidence,
865 additional LD modifier combinations gained GPU evidence.

Scheduling and reuse fields remain excluded by the modifier inventory. Memory hints and
consistency qualifiers stay in the generated suite. Family-local `modifier_roles` declarations
classify a field as `value`, `hint` or `effect`; undeclared fields are `value`. The first selected
form for each value projection gets full input profiles; further hint/effect variants get minimal
profiles, including an issued, nonzero memory access and exact emulator event arguments.
These CPU event checks do not establish concurrent memory-ordering behavior on hardware.

Plans store class/form choices, named input profiles and native assertions, not captured outputs.
The normal `emulator_cases` loader expands them into shared cases for Python, Ghidra and the GPU,
calculating expectations from the independent references at runtime. Strict encoding and a decode
round trip reject forms that the chosen operand setup cannot preserve. Add an adapter only when an
independent reference and operand setup are available; operations without adapters remain manual
until then.

`harness/autocase_profiles.py` declares edge values, seeded random corpora and representative
supplements. Ordinary cases for adapter-owned operations belong there, rather than in parallel
manual loops. Retained manual regressions do not suppress generation of the same form. Each
retained case for these operations must have a `@retained(reason)` justification, checked by a
test.

### The report

The JSON report counts generated forms, covered and blocked interaction requirements, and exact
legal/unresolved combinations. Unsupported operations are
grouped by class to keep millions of gaps manageable; their exact forms remain in the manifest.
Repeated extended-adapter gap reasons are likewise counted by class. Opaque and partially modelled
combinations remain unresolved in the exhaustive support inventory. Partially native extensions
can satisfy interaction requirements through declared operand rows; missing adapters and failed
construction remain explicit gaps. These counts do not measure all input values or prove correct
behavior.

The coverage page and PR comment retain their existing class and complete-form evidence sections,
cards, filters and diagrams. Three-way selection does not add a separate coverage metric.
Only positive reference checks enter `verification_forms`; raw exact-form evidence, including
error/fallback checks, remains available for provenance. Only recorded exact forms contribute to
the report's evidence counts; unknown evidence settings are never filled with modifier defaults.

### Integer adapters (`harness/autocases.py`)

Cover BREV, POPC, FLO, UFLO, SGXT, PRMT, UPRMT, ISETP, UISETP, IMNMX, SEL, USEL, IDP, SHF, USHF, IABS, UMOV,
BMSK, VIMNMX, VISET, VIADDMNMX, VIMNMX3, LOP3, ULOP3, IADD3, UIADD3, LEA, ULEA, IMAD, UIMAD, UBREV,
UPOPC, USGXT and MOV register-pair forms. The planner routes IDP and packed-integer cases to
`int_simd`, BMSK to `bmsk`, arithmetic/carry adapters to `carry`, and the other operations to
`int_alu`. CX setup uses the same descriptor/header helpers as the manual cases on each
architecture.

- **Comparisons** exercise both predicate outputs, .EX's low-word result, signed boundaries,
  predicate inversion and input/output aliases. ISETP's constant PT input stays retained.
- **Dot products** (IDP) cover lane signedness and negated accumulators; CX descriptor/header
  regressions stay retained.
- **Funnel shifts** (SHF) exercise counts around both 32- and 64-bit wrap/clamp boundaries; p-code
  cost stays retained.
- **BMSK** profiles preserve the complete 0..33 position/width domain and seeded edge pairs. Its
  manual cases retain descriptor bounds, unusual headers, special registers and guard behavior.
- **IABS** profiles include the signed minimum and the former random inputs.
- **MOV/UMOV** register-pair profiles verify both words; UMOV retains explicit p-code cost tests.
  Partial MOV classes use the full pixel mask; other masks retain opaque regressions.
- **PRMT/UPRMT** zero registers and p-code costs stay retained; hand-calculated PRMT examples also
  check the reference directly in `test_refs.py`.
- **FLO/UFLO** discard unresolved predicate outputs with PT/UPT in generated cases.
- **IMNMX** outputs that are opaque on newer architectures stay retained.
- **Packed min/max and comparison** profiles preserve the former seeded lane patterns. They
  exercise every supported lane width, signedness, comparison, predicate inversion and RELU form,
  with destination aliases. Three-input profiles also exercise modular lane addition, B-source
  negation, three-way min/max and aliases with each source. VIMNMX forms with unresolved predicate
  outputs use PT-only generated rows; constant predicate inputs retain focused regressions.
- **LOP3/ULOP3** profiles cover all 256 truth tables with all eight Boolean input combinations
  packed into each byte. Edge and seeded profiles check zero/nonzero predicate results, POR/PAND,
  predicate inversion, register aliases and the former manual selector/random corpora. Constant
  PT/UPT inputs and the XOR p-code cost contract remain explicit manual regressions.
- **Arithmetic** profiles preserve seeded carry vectors, LEA shifts, wide IMAD addends and dense
  negation boundaries. References retain the carry from ordinary negation and distinguish .X
  complement/carry inputs. The IMAD immediate adapter explicitly recognizes its printing alias
  while retaining strict checks for all other class/form changes; printing-only alternate plan rows
  remain explicit adapter gaps. Constant UPT inputs, overlapping predicate outputs, p-code costs
  and hand-calculated width regressions stay manual.

### Floating-point and predicate adapters (`harness/autocase_extended.py`)

Cover FADD/UFADD, FMUL/UFMUL, FFMA/UFFMA, DADD/DMUL/DFMA, FSEL/UFSEL, FMNMX/FMNMX3,
FSET/FSETP/UFSETP/DSETP, FCHK, FRND/UFRND, PLOP3/UPLOP3 and P2R/R2P/UP2UR.

- Floating profiles include signed zeros, subnormal, finite, infinity and NaN inputs. Arithmetic
  skips undefined NaN payload comparisons and the documented Ghidra RN underflow band, matching the
  existing reference policy. Readable formulas have separate expectations.
- Source modifiers, including combinations of absolute and negate on every source, are supported
  for FADD/UFADD, FMUL/UFMUL, FFMA/UFFMA, FSET, FSETP/UFSETP, FMNMX, FSEL/UFSEL, FRND/UFRND, FCHK,
  DSETP and DADD/DMUL/DFMA. Their profiles add negative infinity/NaN, signed subnormals and values
  outside the saturation interval. A passing SM89 fp32 GPU run on 2026-10-20 supplies the hardware
  evidence for the fp32 source-modifier forms.
- FMUL/FFMA `.FMZ` uses the `refs.float.fmz` reference and FMNMX.XORSIGN the encoded-sign
  `fmnmx_xorsign`, both from SM89 probes (`research/fmz_xorsign/`); a dense FMZ zero/Inf/NaN factor
  corpus stays manual.
- FMNMX3 source-sign modifiers remain an explicit gap with a retained manual numerical corpus.
- The named `fchk_boundaries` profile preserves the old manual FCHK exponent/seed corpus, so
  ordinary manual FCHK cases are removed; fixtures retain the identical full 1,792-pair corpus.
- Manual numerical sweeps remain where they cover denser seeded inputs, exponent boundaries,
  exceptional results and costs absent from the form profiles. Basic arithmetic examples, ordinary
  FSET predicate/source-form sweeps and uniform arithmetic immediate examples are removed where
  generated profiles cover them. Constant PT/UPT, same-source/output aliases and p-code-cost checks
  remain explicit.
- Predicate profiles exercise all eight LUT inputs and masked transfers.

### Memory, warp and system adapters (`harness/autocase_remaining.py`)

Cover LD/LDG/LDL/LDS/ST/STG/STL/STS, ELECT/MATCH/REDUX/SHFL/VOTE/VOTEU, BAR/BMOV/BREAK/BSSY/BSYNC/
WARPSYNC and MUFU. These use memory and warp references for values, and supplied synchronization
handlers for event contracts. Synchronization profiles do not claim to validate a multithread
scheduler. Full-width MUFU SIN/COS (`mufu__*` classes) use the SM89 fixed-point reference
(`refs/mufu.py`, profile `mufu_sincos`); the other MUFU operations get error contracts only, which
are diagnostic, and the planner leaves their numerical coverage blocked until an independent
hardware-specific approximation oracle exists.

I2I uses the `integer_narrowing` profile in `autocase_integer_extra.py`: all
available register, uniform, immediate, constant and CX sources, all four saturated
destination formats, boundary neighbours and register aliasing.
Memory profiles retain the size, address, ordering, cache and prefetch modifiers;
SM75 LDS's `.U` sizes use the same independent load reference as their plain sizes.
128-bit operands use aligned register groups, including the aliasing scenario.

`__max_ops` limits in generated system profiles:

- An integer keeps the strict nonempty, branch-free, CALLOTHER-free limit.
- A structured value, `{"count": N, "userops": [...]}`, additionally requires the exact named
  userops and rejects every other runtime call. Structured limits require inspection metadata,
  including the userop names. LDC/LDCU permit only `sass_memory_order`, with one runtime event; NOP
  requires zero p-code operations and zero events.
- An explicit `branches: true` in a structured limit permits generic-memory window routing; its
  exact named-effect guard still rejects value/opaque primitives.

`__userops` requires nonempty inspected p-code and exactly the named runtime calls, including
their multiplicities. DECOUPLED native profiles require
`sass_ordered_block_barrier_wait_deferred`; profiles with an operation bound add one operation
and that exact name to their existing contract. The MD instruction type determines this
expectation, independently of the lifted body. Missing inspection metadata, duplicate waits,
value primitives and opaque fallbacks still fail.

The `__native` assertion stays strict. QSPC values run in Ghidra's explicit provenance context and
remain marked as primitive checks rather than native Python value validation.

### Other adapter modules

| Module | Operations | Family |
|---|---|---|
| `harness/autocase_integer_extra.py` | VIADD, UVIMNMX, UI2IP, MOV64IUR | `int_alu`, `int_simd`, `conversions` |
| `harness/autocase_conversions.py` | F2I/UF2I, I2F/I2FP/UI2F/UI2FP, F2F | `conversions` |
| `harness/autocase_packed_fp.py` | F2IP/I2IP; HADD2/HMUL2/HFMA2/HMNMX2/HSET2/HSETP2 (HSET2/HSETP2 inputs from `autocase_half_profiles.py`'s audited table, `harness/data/half_comparison_inputs.json`) | `conversions`, `fp16` |
| `harness/autocase_wide_fp.py` | FADD2/FMUL2/FFMA2; F2FP/UF2FP | `fp32x2`, `f2fp` |
| `harness/autocase_mixed_fp.py` | FHADD2/FHMUL2/FHFMA2 | `fp32x2` |
| `harness/autocase_system.py` | LDC/LDCU/ULDC, QSPC; S2R/CS2R/S2UR; BRA/CALL/RET/EXIT/NOP | `memory`, `control` |

Their scope and remaining gaps are summarized per batch in [`docs/testing.md`](../docs/testing.md#generated-shared-cases).

## Sampling

`--sample PERCENT` applies to every shared-case family in the lifted Python tests, Ghidra, the GPU
runner and `python -m tests.semantics`. Values are integers from 0 through 100; `--exhaustive`
aliases 100. It does not sample whole-kernel fixtures or the separate disassembly corpus (`--full`).
It also does not expand a three-way plan into every full modifier combination: 100% expands the
declared input corpus, and neutral variants retain their minimal declared corpus.

**0% (the default)** keeps, for each available form, its lowest stable hash-ranked scenario.
Additional representatives preserve lifted-emulator and SM89/SM90 GPU eligibility when necessary.
`@retained` regressions, kernel scenarios and explicit error/opaque assertions always run, even when
a retained function contains an entire sweep, so retained corpora must stay small. `@anchored`
suites (`harness/manual.py`) retain only their anchors, the indices of each sweep (`part.sweep(k, n)`)
near 0, n/2 and n on the all-register class (`__RRR`, `__URURUR`); the rest of their corpus is sampled like other cases
and runs complete at 100%. Thus minimal is a coverage floor, not exactly one case per form. Existing
unsupported legal forms remain gaps.

**Intermediate percentages** admit additional input identities below a fixed SHA-256 threshold.
Generated identities use operand fields and seeded state, so ranking needs no encoding; manual
identities use their fields and state. Expectations and p-code inspection flags do not affect
ranking. They approximate a percentage of additional cases, not an exact case budget. Samples are
deterministic and nested: a higher percentage never removes a case a lower one selected.

**100%** preserves the complete candidate list. Selection never manufactures evidence for a skipped
scenario.

How selection works:

- Generators describe the complete declared corpus, whether their loops use `range` or `c.sweep`.
- Generated adapters first describe inputs with independent expectations and hardware annotations,
  then select descriptions before instruction encoding and decoding. Unselected generated
  descriptions are never encoded or decoded; reference evaluation and lightweight input enumeration
  still precede selection. Selected encodings must preserve the declared form and backend
  eligibility. Small memory-adapter and IMAD-alias paths retain eager decoding where classification
  requires it.
- Manual cases are encoded only when selected too: sampling takes their form from their modifier
  fields (`sampling.declared`, one encoding per combination), and `complete` checks each
  encoded case against it.
- Cases are grouped by class and modifier settings, separately for each architecture/lowering and
  for manual versus generated sources. Each generated planned form is selected independently, so
  worker counts and shard boundaries do not change it.
- Generation is bounded by a manual family or a generated form, without assembling a whole
  language in a worker. GPU collection also uses the shared shards, with at most eight workers; the
  parent collects selected scenarios only.

Reference evidence refresh uses 0%, so its case counts describe this minimum while preserving
available form coverage; `sass-refresh-coverage` keeps the committed artifacts at 0%. For an ad hoc
evidence report at another depth, use
`python -m tests.evidence SM89 --sample 25 --output /tmp/sass-evidence-25`.

## Hardware evidence and probes

### Shared-case hardware evidence

`baseline/gpu_observations/SMxx.jsonl` records what the GPU returned, not whether it passed: per
probe key and device, the captured 32-bit values in the probe's capture order. The key
(`harness/device_evidence.py` `probed`) hashes the scenario's probe built alone (target word,
inputs, setup, scheduling, capture layout, launch shape), never its expectation or the comparison,
so changing a reference, an expectation or `device_cases.compare` re-judges the recorded
observations without a GPU. The first line holds the format version and the device table; every
other line is `[key, device index, "observed" | "inferred", [hex value or null]]`, sorted.
`inferred` observations were reconstructed from the pass fingerprints they replaced (2026-10-23,
`docs/testing.md`); `null` is an unknown capture. A GPU run replaces its device's observation of
each probe it ran; errors and unrun scenarios keep what was recorded. Nothing prunes observations
automatically, since they cannot be re-recorded without the GPU:
`python -m tests.harness.device_evidence unmatched` reports those no current scenario runs (at
`--sample 100`), and `prune` removes them.

A scenario's verdict (`device_evidence.judge`) is `fail` when a recorded capture disagrees with
its current expectation on any device, `pass` when one observation agrees in every compared
capture, `unknown` when every observation lacks one, and `unobserved` without observations.
`tests/evidence.py` credits a target word directly as `cases/FAMILY` when every GPU scenario for it
in the evidence run's selection (its `--sample`) passes, independent of wrapper instructions
and whole-kernel data-path slicing, so new or changed cases leave only their own words unverified
until a GPU run. It prints each `fail` as a hardware disagreement and writes the verdict counts per
family, which `sass-coverage` commits as `processor/SASS/semantics/gpu_verdicts.json`: a change to
a comparison, expectation or reference shows there as flipped verdicts. `test_gpu_observations.py`
(default suite, about 20 s per architecture) fails on any disagreement for the selected `--arch`
and `--family`. Evidence refresh needs neither a GPU nor Ghidra and does not execute recorded
observations. The families' cases are classified, keyed and judged shard by shard
(`harness/device_evidence.py` `summaries`, over `harness/sharded.py`); reference evidence streams
the same shards on every core.

### Raw single-instruction probes

Almost always use autocases for single-instruction GPU discovery: extend or add
the adapter/profile, then re-observe its candidates with `device_cases.observe`.
The runner ignores expectations. Missing adapter support is a reason to fix the
adapter, not to write a parallel manual probe matrix. Whole-kernel compiler
sequences are an exception because they span more than one instruction.

For a concrete documented harness limitation, use
`Cases.probe(cls, fields, registers, observe=[...], ...)` and
`tests.harness.device_cases.observe(cases)` to discover outputs without a reference. It uses the
same generated wrappers and setup adapters, returning raw registers and observed memory without
comparisons or evidence updates. `python -m tests.probe INPUT.json --out OUTPUT.json [--arch SM90]` accepts a JSON
list of the same arguments. Draft probes cannot enter a regression suite until converted to `c.add`
with an independent reference. The investigation skill's
[probe guide](../.agents/skills/sass-opcode-semantics/references/gpu-probes.md) contains runnable
examples and memory/warp setup notes.

### Whole-kernel observation

To derive a reference from a kernel, draft a fixture with `expected=None`, run
`uv run python -m tests.observe FAMILY` (`--diff` shows threads that disagree with the current
reference), and write the reference from the observed outputs. A fixture without a reference fails
both fixture tests.

## Disassembly corpora and results

The disassembly tests use `nvdisasm -b SMxx` as an oracle over three corpora:

- **real**: every instruction in the cubins inside the CUDA libraries (`cuobjdump -xelf` of
  cuBLASLt, cuSPARSE, ...), about 3–6M unique words per architecture
- **synthetic**: 64 random words per md class that satisfy its encoding constraints
- **mutated**: real words with one or two bits flipped

```sh
uv run sass-compare-pydecode SM89 real 0       # pydecode vs nvdisasm (0 = whole corpus)
uv run sass-compare-ghidra SM89 synthetic 0    # Ghidra (generated SLEIGH) vs pydecode
```

Current results (CUDA 13.4 `nvdisasm`):

- pydecode matches `nvdisasm` on every real word for all eight architectures that have cubins
  (about 38M words), and on all synthetic and mutated words except 17 SM75 HMMA words. Some valid
  encodings print as blank lines in `nvdisasm`; the cause is still unknown, so those are excluded
  from the counts.
- The generated SLEIGH matches pydecode on every SM75 and SM89 word in all three corpora. The other
  architectures are still being run.

## Ratchets

`test_pydecode_disassembly`, `test_ghidra_disassembly` and `test_pcode_dataflow` compare against
committed baselines in `baseline/`: a class that regresses fails, and a newly passing class is
reported until accepted with `--update-baseline`. Review the diff of any baseline update; op-count
increases (`pcode_<variant>.json`) should come only from classes the change touched.

The 2026-11-04 cache-control changes were checked with:

```sh
nice uv run pytest tests/test_cache_control.py tests/test_ghidra_bridge.py tests/test_pcode_semantics.py tests/test_pcode_dataflow.py --family control --family conversions --family f2fp -m ghidra --arch all --fp all -q
nice uv run pytest tests/test_pcode_dataflow.py -k pcode_ops -m ghidra --arch all --fp all -q
nice uv run pytest tests/test_cases_on_gpu.py tests/test_refs_on_gpu.py --family control -m gpu --require-gpu --exhaustive -q
nice uv run pytest tests/test_cache_control.py -m gpu --require-gpu -q
nice uv run pytest tests/test_cases_on_gpu.py tests/test_refs_on_gpu.py --family memory --family atomic --family asynchronous -m gpu --require-gpu -q
```

The first command took 681 seconds alongside coverage refresh, passing 4,508
tests and 191,390 shared cases; only the twenty operation-count ratchets needed
their reviewed CCTL/conversion changes. The second passed all twenty ratchets
in six seconds. The GPU commands took 25, nine and 52 seconds respectively.
These contended measurements are validation records, not timing baselines.
The direct cache-profile checks cover every retained input beyond the
planner's interaction coverage; cooperative tests exercise two blocks under
round-robin, reverse and seeded launch schedules.

The subsequent D.IVALL validation uses the same cache-profile and GPU commands,
plus this focused all-language check:

```sh
nice uv run pytest tests/test_cache_control.py tests/test_pcode_semantics.py tests/test_pcode_dataflow.py --family control -m ghidra --arch all --fp all -q
```

The final uninterrupted run passed 1,509 tests and 6,580 shared control cases in
51 seconds with populated caches. All operation-count baselines passed without
updates. The finalized source check passed 66 tests in 113 seconds alongside
coverage work; the exhaustive control GPU suite passed 160 tests in 14 seconds.
Cross-block invalidation fixtures require launch scope. Their readiness counter
orders cache warm-up before the producer's release handoff; all outputs are
compared exactly, without timing or polling-count expectations.

The CI architecture-selection fix was checked with:

```sh
nice uv run pytest tests/test_definition_functions.py tests/test_barrier_result.py tests/test_asynchronous.py tests/test_cache_control.py tests/test_matrix.py tests/test_evidence.py --arch SM89 --fp all -q
```

This passed 128 tests with 173 unselected-architecture skips in 576 seconds,
including native compilation and cooperative matrix checks. The initial
2–4 minute estimate omitted most of that work; this is a validation record,
not a revised timing baseline. Two discovery-contract checks preserve the
default twenty-variant domain and explicit subsets. Three representative
matrix checks pass with explicit SM89 parameters; selecting SM90 skips all
152 SM89-only matrix checks.

The ordered-capture size fix (2026-11-05; `docs/sleigh.md`) rebuilt all twenty
languages with zero dead temporaries. The SM89/SM90/SM107 numerical, memory,
atomic and dataflow run passed 3,528 tests and 246,662 shared cases except four
dataflow checks: SHF's unused constant-memory high word and LOP3's constant
predicate result. After those fixes, the focused fp32/dataflow run passed all
242 tests and 38,383 shared cases in 78 seconds. Existing numerical operation
limits were unchanged; the per-class p-code dictionaries still require review.
The isolated exact/readable ordered gate passed in seven seconds. Integer
checks passed 604 Python tests (17,562 shared cases) in twelve seconds, 604
native tests in 28 seconds, and 108 GPU tests (3,167 scenarios) in 22 seconds.
These are validation records, not timing baselines.

The next ordered-body migrations (2026-11-05) cover control transfers, QSPC,
ELECT, B2R, async-copy issue/commit, pure MUFU, and explicit snapshots for SHFL,
VOTE/VOTEU, MATCH, and REDUX. Encoded commit slots are explicit IR arguments.
Focused collective Python/type/form tests passed 163 checks, including 474
shared instruction cases; later type and retained-wide-snapshot checks passed
106 checks. Native collective instruction/kernel fixtures passed 224 checks in
95 seconds. Aliased collective loops and same-site reuse passed 27 native/Python
checks under all three schedules. All ten ordered K execution checks passed
in 27 seconds, including production VOTE, MATCH, and REDUX contracts.

All twenty languages compiled below the supported limit with zero dead
temporaries. Collective Ghidra checks passed 206 tests and 2,272 shared cases in
108 seconds across all architectures and both modes. GPU regressions passed
137 tests, including 525 shared scenarios; three emulator-only class checks were skipped.
Hardware-invalid and unresolved cases retained their emulator-only classification.
Logs, size measurements, and before/after performance samples are under
`.cache/ordered-gate/`. The operation-count dictionaries and timing baselines
have not been updated.

DEPBAR now uses distinct ordered group-count and slot-mask waits (2026-11-05).
The slot-7 sentinel and combined runtime wait entry point were removed.
Focused Python/type/control checks passed 145 tests and 1,642 shared cases;
native wait, kernel, and retained-collective checks passed 343 tests in 80 seconds.
The wait test covers every encoded slot, counts 0/1/2/63, four masks, copy order,
and preservation of an earlier write when an invalid slot faults. The corrected
ordered control and definition-discovery checks passed 120 tests. Definition
discovery now traverses statement expressions instead of ignoring ordered bodies.

All twenty languages rebuilt with zero dead temporaries; SM90 exact is
16,192,864 bytes. Ghidra asynchronous checks passed 160 tests and 25,024 shared
cases across all architectures and both modes in 55 seconds. Separate dataflow
and collective-loop checks passed eight tests. Mask-only DEPBAR expects one wait
userop and `.LE` expects two; the existing eight-op bound was retained. Ordered
K checks passed twelve tests, and five production contracts passed a further
event-transcript check. GPU checks passed 47 tests and 409 shared scenarios,
with three emulator-only class checks skipped.

Pinned native scheduler comparisons and `nice uvx py-spy` profiles are recorded
in `docs/emulation.md`. Collective performance regressions remain unresolved.
These are verification records; neither timing nor per-class p-code baselines
were changed. Logs are under `.cache/ordered-gate/depbar-*` and
`.cache/ordered-gate/collectives-specialized-*`.

The Ghidra execution wrapper now exposes its p-code position to collective
capture keys. A focused regression interleaves two captures at distinct
positions and checks separate values and released handles. The combined
Python/native/real-Ghidra ordered gate and collective-loop run passed 90 tests
in eleven seconds (`ghidra-capture-position-fixed.log`). Its isolated snapshot
fixture was updated to the runtime's lane-indexed representation; numerical,
memory, atomic, suspension, and decompiler checks passed in both modes.

Ordered LDSM/HMMA migration (2026-11-06) passed 47,376 shared lifted cases across
SM89/SM90 and both modes. Six cooperative Python/native fixture checks passed
under all three schedules in 262 seconds; native standalone contexts and
snapshot contracts passed 153 checks in 28 seconds. Word-group snapshots cover
one, two, and four words, retained generations, copied standalone contexts,
invalid participant/word indices, and released handles. Eight ordered K checks
passed in 17 seconds. GPU regressions passed 35 tests and 11,844 scenarios in
273 seconds.

The first Ghidra run passed all 47,376 shared cases and dataflow checks but failed
six cooperative fixture checks: immediately completing a capture could enter
the next capture before Ghidra exposed the fresh instruction frame. Explicitly
beginning the frame before execution fixed the grouping. The subsequent real
Ghidra gate and cooperative kernel/matrix run passed twelve checks in 181 seconds,
including both modes and all three schedules. All twenty languages compiled
below the size cap with zero dead temporaries.

After typed native snapshot-word reads and resume positions restricted to
suspending operations, 191 native scheduler/resumption checks passed in
19 seconds. Pinned alternating comparisons against the original checkout
returned identical outputs but retained 18–20% matrix overhead. Performance
remains unresolved; timing and per-class operation-count baselines are unchanged.
Logs are under `.cache/ordered-gate/matrix-*` and
`.cache/ordered-gate/native-pause-resumption-tests.log`.

Shared native runtime state now spans ordinary and resumable instructions
(2026-11-06). The ordinary path allocates no execution frame. A two-instruction
regression arrives in one instruction and waits at a later PC, compares native
and Python traces, and checks all three schedules. Explicit ticket release
preserves arrivals and existing pending waits while invalidating subsequent
handle use; Python and C++ tests also reject wrong-type handles.

The focused Python/native/export run passed 268 checks in five seconds with warm
caches. The isolated thirteen-form Ghidra language passed both modes in seven
seconds, including handle release. All sixteen ordered K execution contracts
passed in 36 seconds using the documented container, including release after a
wait and preservation of earlier writes. Logs are under
`.cache/ordered-gate/shared-runtime-*`; the first K command omitted the container
setting and failed because `kompile` was absent from PATH.

After removing the concrete-runtime optimization experiment, the retained
version passed 22 production native checks in 213 seconds. These include all
cooperative matrix fixtures for SM89/SM90 under three schedules, standalone
matrix contexts, ordinary runtime sharing, and generated-definition parity.
Source equality with the saved pre-experiment files was verified. Results are
in `.cache/ordered-gate/shared-runtime-production-native.log`.

Ordered block-barrier feasibility (2026-11-06) adds separate implicit/counted
arrival, waiting, deferral, release, and state/result operations backed by one
C++ coordinator. The Python/native scheduler cases run on SM89/SM90 under
round-robin, reverse, and seeded schedules. They check effects before suspension,
instruction counts, two-warp explicit counts, deferred waits after releasing
the ticket handle, and completion after a peer executes EXIT.
Coordinator tests cover reductions, ID reuse, invalid counts and arrivals,
ticket ownership, publication, and old tickets preserving newer deferred state.
The broader ordered software run passed 453 checks in 51 seconds; six
ordinary-arrival checks passed in three seconds after extending them to the
production block-ticket protocol.
The isolated Ghidra language now has seventeen forms; both modes passed in
seven seconds, including actual coordinator suspension and deferred waits.
Its decompressed sizes are 12,438 exact and 12,173 readable bytes. The added
implicit-arrival/wait, deferred-arrival, deferred-wait, and early-exit forms have
5, 4, 3, and 9 p-code operations respectively, including 4, 3, 2, and 4 user
operations. These are isolated gate measurements, not production language sizes.
Three formal barrier transcripts passed in seven seconds using
`SASS_K_CONTAINER=nvdisasm-re-k:7.1.337`. Production BAR migration and deferred
instruction gates remain pending; these checks verify the operation protocol.
Logs and Ghidra results are under `.cache/ordered-gate/block-coordination-*`.

Production BAR migration (2026-11-06) passed 33 selected SM89 coordinator,
standalone-read, reduction/state and indirect-branch checks in 311 seconds,
with cold native libraries. The same barrier/state fixtures passed both Ghidra
modes on SM89/SM90 under three schedules in 265 seconds. Fifty software
reconvergence checks passed in 63 seconds, including deferred BAR before
legacy BSSY, independent counted/deferred barriers, and fault diagnostics.
The broader ordered run passed 456 checks in 81 seconds. Three additional
checks verify native memory specialization and rejection of atomic suspension
before empty-wait elimination. Eleven formal production instruction contracts
passed in 23 seconds. All 232 control/thread hardware fixtures passed on the
local SM89 GPU in five seconds, including the added deferred-convergence fixture.
All twenty languages build below the supported limit with zero dead temporaries;
SM90 exact is largest at 16,311,101 bytes. Logs are in
`.cache/ordered-gate/bar-ordered-*`. These cold-run times do not replace existing
performance baselines; whole-kernel regressions remain under investigation.

Selective LOP3/ULOP3 sharing (2026-11-06) passed fourteen selected SM90 Python
groups in three seconds, twenty-eight native groups on SM89/SM90 in 24 seconds,
and twenty-eight Ghidra groups in both modes in 29 seconds. The selected SM89
hardware group passed in three seconds; four aliased-input formal contracts
passed in nine seconds. All twenty languages compile with zero dead temporaries;
SM90 exact is largest at 15,917,261 bytes. Full SM89/SM90 dataflow passes in all
four variants. Operation-count ratchets still require refactor-wide review,
and legacy definition-inspection tests still need migration from `.writes`.
A pre-sharing source snapshot reproduces the same 75 failures in the selected
legacy definition tests. The focused new definition/merger checks pass.
The four-LUT native timing comparison retains the original baselines.
Logs and inspected p-code/decompiler artifacts are under
`.cache/ordered-gate/lop3-*`.

Direct WARPSYNC migration (2026-11-06) passed 298 SM89/SM90 control checks
covering 1,344 shared cases in seven seconds. Production Ghidra checks passed
90 kernels in both modes on SM89/SM90 under three schedules in 41 seconds.
Fifteen production formal instruction contracts passed in 33 seconds; sixteen
selected WARPSYNC hardware fixtures passed on the local SM89 GPU in five seconds.
All twenty languages compile below the unchanged limit with zero dead
temporaries, with SM90 exact largest at 15,918,723 decompressed bytes.

After retained-wait and coordinator membership optimizations, 335 native
ordered/runtime/scheduler checks passed in 66 seconds (two Ghidra-marked checks
skipped in that selection). The Python/cooperative/merger selection passed
256 checks in 142 seconds with cold rebuilt kernels. Twenty additional checks
enumerate and type-check every normal WARPSYNC class on ten architectures in
both modes. Thirty-one coordinator checks and the native ticket-release
retention check passed in two seconds. Both isolated Ghidra gates passed in
eight seconds, including WARPSYNC position retention and a preceding write
executed once. Whole-kernel timing remains about 20% above the pre-WARPSYNC
control; no timing baseline was changed. Logs are `.cache/ordered-gate/warpsync-*`.

All 342 definition-inspection tests now pass against ordered captures and
statements in five seconds (2026-11-06). This replaces the unfinished `.writes`
inspection reported above, including PLOP3 alias cases on all architectures
and both modes. Pure display inspection rejects stateful bodies and reads
after writes; SHFL is inspected as separate ordered operations.
The log is `.cache/ordered-gate/ordered-definition-inspection.log`.

Direct BSSY/BSYNC and BREAK/BMOV migration (2026-11-06) passed 1,468 selected
Python control groups covering 6,444 shared cases across all architectures and
both modes in 25 seconds. Sixty checks enumerate and type-check the normal
WARPSYNC, convergence and environment classes across all twenty languages.
Thirty-two focused native checks preserve BREAK/BMOV inputs, state-read values,
events and disabled predicates. Twenty-eight production formal contracts passed
against the explicit environment in the K 7.1.337 container.
Focused production Ghidra checks passed 692 SM89 and 680 SM90 shared cases in
both modes. The broader SM89 run initially had 36 failing pure-p-code
assertions because the explicit deferred wait is a CALLOTHER. MD-driven
named-operation contracts now require that exact wait, reject duplicate or
unknown calls, and add one operation to existing bounded-effect profiles.
All 252 adapter checks pass in fourteen seconds. The complete control run
passes 1,666 Python checks (6,444 shared cases) in seventeen seconds and
1,466 Ghidra checks (6,580 shared cases) in seventy-five seconds across all
architectures and both modes. The full operation-count review remains open.
Logs are `.cache/ordered-gate/deferred-wait-*`.

Typed native capture bindings (2026-11-06) pass 465 combined native,
coordinator and cooperative checks, including 21 malformed-reply and
suspension regressions, in 227 seconds with rebuilt libraries.
The broader control/thread hardware selection passes 275 checks with
69 unsupported or inapplicable skips in 475 seconds. This command also
selects numerical-definition discovery and CUDA tests; its cost is not
the earlier warm 241-check fixture selection's five seconds.
Logs are `.cache/ordered-gate/capture-binding-combined-verification.log`
and `deferred-wait-contract-hardware.log`.

Partial deferred-barrier capture regressions (2026-11-06) passed eighteen
SHFL/MATCH/REDUX fixtures across SM89/SM90 and three schedules, plus a coordinator
query test, in 29 seconds. Definition and merger checks passed all 446 tests
in 39 seconds. The expanded production Ghidra convergence suite passed 200
kernels across both architectures, both modes and three schedules in 70 seconds.
The final native/coordinator/cooperative selection passed 431 checks in
139 seconds, including compilation of new kernel translation units. All 241
existing control/thread hardware fixtures passed on the local SM89 GPU in
five seconds. The partial-barrier fixtures preserve the software model and
do not claim new hardware verification of partially predicated barriers.
All twenty languages compile with zero dead temporaries; SM90 exact is largest
at 15,927,146 bytes. Runtime cache experiments were discarded, and performance
baselines remain unchanged. Logs are `.cache/ordered-gate/convergence-*` and
`partial-deferred-*`.

Canonical family interface cleanup (2026-11-06) passed 618 framework, merger,
definition and formal-export checks in 33 seconds, plus the family return-type
contract test. Every family now returns an ordered block without a migration flag;
symbolic operands always use explicit captures. Regenerated SM90 exact source was
byte-for-byte identical to the source of the compiled language.
The native fixtures passed all 2,400 random expression cases and 63 definition
checks; the final warm native/ordered selection passed 172 tests in 3.44 seconds.
Random-tree constants that exceeded their declared widths now receive
enough bits to hold the original numbers; all generated numerical inputs and
random-generator states were compared with the previous fixtures and preserved.
The assignment benchmark retains the original generated baseline function instead
of invoking the removed legacy converter: nine alternating runs of 100 million
assignments gave medians of 0.056509 seconds (baseline) and 0.056613 seconds
(ordered), ratio 1.00184. This does not close the larger kernel performance
regressions or establish a new timing baseline.
Fourteen ordered export checks and three K parameter-execution checks passed;
parameters are typed consistently, skipped branches need no missing parameter,
and parameter faults retain earlier writes. Logs are
`.cache/ordered-gate/canonical-*`.

Canonical formal migration (2026-11-06) passed 65 plain export/publication checks
across all architectures and 64 K execution/contract checks on SM89 in both FP
modes. LLVM and Haskell use the same ordered model. The worked example supplies
explicit completion responses for deferred-barrier waits; all eleven generated
reachability claims pass, and deliberately false claims still fail. The rebuilt
PDF contains all 228 rules from the four source modules, the response assumption,
and the actual prover output. A repeated defined floating comparison exposed
colliding expression IDs; reuse of its lowered expression now preserves unique
IDs and the independent DSETP results. Logs are
`.cache/ordered-gate/formal-canonical-*`.

The two partial deferred-BAR/BRA fixtures (2026-11-06) pass 12 Python/native
checks across SM89/SM90 and round-robin, reverse and seeded schedules. Production
Ghidra synchronization checks pass in both FP modes on both architectures.
Both synthetic 32-thread SM89 fixtures pass hardware regression and have recorded
fingerprints: DIV is untaken everywhere, CONV is taken everywhere. The original
emulator's split lane-group results disagree with these GPU observations; the
ordered body required no change. This does not establish behavior with lanes
parked at blocking BAR or WARPSYNC, or hardware verification on SM90. Logs are
`.cache/ordered-gate/deferred-branch-*`.

## Caches

Everything below lives in `.cache/` and is safe to delete.

| What | Keyed by |
|---|---|
| nvdisasm oracle results (`oracle.sqlite`) | nvdisasm hash, word, address |
| parsed md IR (`ir/`) | md files and parser sources |
| real-corpus words (`corpus/`) | the cubin set (paths, sizes, mtimes) |
| Ghidra decode/emulation results (`ghidra/`) | the language's `.sla`, its own `.ldefs` entry and specs, the `src/sass` sources the Ghidra program imports, the Ghidra install and venv paths; then the request |
| compiled kernels (`kernels/`), block emulator (`java/`), extracted library cubins (`cubins/`) | sources and flags |

## Prerequisites

| What | Default | Override |
|---|---|---|
| target nvdisasm (CUDA 13.4, V13.4.92) | the dev group's `nvidia-cuda-nvdisasm` in `.venv` or the uv cache | `NVDISASM` |
| Ghidra 12.2 and its pyghidra venv | `~/Ghidra/ghidra_12.2_PUBLIC`, `~/.config/ghidra/ghidra_12.2_PUBLIC/venv` | `GHIDRA_INSTALL_DIR`, `GHIDRA_PY` |
| `nvcc`, `ptxas` (CUDA 13.0.88: GPU-fixture kernels for the Ghidra and GPU tests; `ptxas` evidence) | the dev group's wheels in `.venv`, else `/usr/local/cuda/bin` | `NVCC`, `PTXAS` |
| `cuobjdump` and the CUDA libraries (real corpus) | `/usr/local/cuda/bin/cuobjdump`, `/usr/local/cuda/lib64` | `CUOBJDUMP` |
| generated languages | `processor/SASS/data/languages` | `SASS_SLEIGH_OUT` |
| `javac` (cooperative GPU fixtures) | on `PATH` | |
| a C++20 compiler (the runtime primitives every emulator calls, `sass.emulate`; compiled once into `.cache/emulate`) | `clang++`, else `g++`, on `PATH` | `CXX`, `SASS_CXXFLAGS` |

Detailed coverage enumeration is opt-in (`sass-build-languages --coverage`). Normal builds need
no runtime manifest: primitive calls are explicit in the generated p-code. `sass-refresh-coverage`
and CI enable coverage explicitly.

For a scratch build, run `uv run sass-build-languages SM89 --output /tmp/sass-languages` (it copies
the shared definitions), then set `SASS_SLEIGH_OUT=/tmp/sass-languages` for the Ghidra tests.
