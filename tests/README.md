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
| `test_assemble.py` | does source encode correctly and produce native cubins? | FORMAT parser, strict encoder, labels, directives, ELF writer | decoder round trips, nvdisasm, known words and metadata |
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

Need an SM89 device (outside the sandbox), but not Ghidra. Run these when shared cases,
GPU fixtures, references or the GPU harness change.

`test_cases_on_gpu.py` runs the hardware-valid cases from `emulator_cases(c)` directly:
encoded words, seeded registers, constants and independent reference outputs are shared
with the Python and Ghidra tests. Native cubin wrappers initialize state, execute the
original instruction encoding (with dependency-safe scheduling), and capture registers,
predicates and memory. Scalar cases are batched; uniform state is isolated between
scenarios, and warp contexts become actual lane inputs and active masks. A warp-context branch
(BRA.DIV/.CONV) records its landing PC like the branch adapter; its inactive lanes either all exit
or, with the context's `waiting` mask, all wait at a `BSSY`/`BSYNC` barrier. Memory adapters
bind logical addresses to real allocations and runtime shared/local windows. No CUDA
compiler is needed for these generated probes.

Sweeps use the same deterministic sample as Ghidra by default; `--exhaustive` runs every
case. Each invocation executes the selected tests, even when passing evidence exists.
The terminal summary reports passed, failed, sampled-out and emulator-only scenarios,
including exclusion reasons. Emulator errors, runtime events, impossible synthetic
state and reserved launch constants remain emulator-only. Unknown case state fails
classification; an adapter failure is a test failure. Worker processes time out after
30 seconds; device faults are bisected to individual cases in fresh CUDA contexts.

Existing CUDA and patched whole-kernel fixtures remain integration tests: they cover
address setup, constants, memory operations, predication, loops and cooperative blocks.
They continue to run both on hardware and in Ghidra.

| File | Question | Under test | Oracle |
|---|---|---|---|
| `test_cases_on_gpu.py` | do shared instruction cases agree with hardware? | `refs/`, through generated native probes | SM89 GPU; passing complete families are fingerprinted in `baseline/gpu_case_passes.json` |
| `test_refs_on_gpu.py` | are the references right on hardware? | `refs/`, through the GPU fixtures | SM89 GPU (CLI `uv run python -m tests.observe FAMILY`); records passing fixtures' fingerprints in `baseline/gpu_passes.json`, the source of `gpu` evidence (`tests/evidence.py`, per class and per form: the modifier combinations that ran) |
| `test_assemble.py::test_native_cubin_on_gpu` | do native cubins load and execute? | self-contained kernels assembled without compiled templates | SM89 Driver API: parameters, branches, constants, shared memory, multiple entries |

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
| `--exhaustive` | run complete GPU, Ghidra and lifted sweeps (otherwise deterministically sampled) |
| `--update-baseline` | accept current results into the ratchet baselines |
| `--full` | `test_pydecode_disassembly` and `test_ghidra_disassembly` on the whole real corpus instead of a sample |
| `--require-gpu` | fail instead of skipping without an SM89 device |

While iterating, run what a change touches (`--family conversions --arch SM89`). Before committing,
run the Python and Ghidra tests on every architecture and lowering; run the GPU tests too if
shared cases, fixtures, references or the GPU harness changed.

Rough wall times and peak memory (summed PSS) on a 32-core, 93 GiB machine with other work running (2026-10-16): Python
tests 25 s and 10 GiB on SM89, 3 minutes and 13 GiB on every architecture and lowering (25 GiB with `--exhaustive`); language
build 1.75 minutes and 27 GiB (about 17 compilers at once); Ghidra tests on all languages 8 minutes and
25 GiB cold, 2.5 minutes when cached (mostly building the cases); GPU tests 15 s. Ghidra results are
cached per program by the sources it imports (`ghidra_bridge.sources`), so an edit elsewhere in
`src/sass` keeps them.

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
| Ghidra decode/emulation results (`ghidra/`) | the language's `.sla`, specs and coverage manifest, the `src/sass` sources the Ghidra program imports, Ghidra; then the request |
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

Detailed coverage enumeration is opt-in (`sass-build-languages --coverage`). Normal builds
write only the lightweight runtime manifest needed by primitive emulation.
`sass-refresh-coverage` and CI enable coverage explicitly.

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

- `emulator_cases(c)`: shared single-instruction cases for `test_lifted_semantics`,
  `test_pcode_semantics` and `test_cases_on_gpu` (hardware expectations use exact mode). `c.add(cls, fields, registers, expected, ...)` encodes one instruction
  of md class `cls` with operand `fields`, seeds `registers`, and checks `expected`; classes the
  architecture lacks are skipped. Helpers in `harness/case.py`: `put` (place an operand value in
  a register, immediate or constant bank), `cx_source`, `c.kernel(...)` (a one-instruction kernel
  expected to fail), `c.sweep(n)` (indices of a large sweep over codes or values, sampled unless
  `--exhaustive`). Expectation keys are documented in `harness/checks.py`: plain keys are observed
  registers; `__native`, `__max_ops`, `__counter`, `__memory`, `__events`, `__fp8_nan`,
  `__opaque` and `error` are checks.
  Ordinary scalar cases automatically run on the GPU. `hardware={"adapter": ...}` binds
  memory, warp, branch or special-register scenarios; the family suites demonstrate
  these adapters. `hardware={"skip": "reason"}` explicitly marks model-only state.
  `__native`, `__max_ops` and runtime checks remain emulator assertions. Readable-mode
  approximations never become hardware expectations. The runner retains input/output
  register aliases and compares all expected registers and observed memory.
- `gpu_fixtures()`: `harness/gpu.Fixture`s for `test_refs_on_gpu` and `test_fixtures_emulated`.
  A fixture is a CUDA kernel (`kernels/*.cu`, compiled for sm_89, optionally with patched SASS
  words: `harness/scalar.py`, `harness/templates.py`) and its reference outputs `expected`. Each
  thread reads three inputs and writes one output. The GPU output is compared with `expected`
  using `same`; the kernel, run whole in the emulator from entry to EXIT, is compared with
  `expected` using `emulator_same` (default `same`), with `excused` admitting known emulator
  defects. Patched carry-kernel slots (`templates.slot_fixture`) must leave UR4/UR5 alone: the
  kernel loads its default memory descriptor there (`ULDC.64 UR4, c[0x0][0x118]` at 0x30) and its
  final `STG.E` uses it implicitly, so a value with bit 27 set in UR5
  (descriptor bit 59) traps with CUDA_ERROR_ILLEGAL_INSTRUCTION (715).

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

### Generating shared cases

`python -m tests.autocases SM89 --write --report /tmp/shared-case-gaps.json`
reads the exact-language coverage manifest, expands
its legal modifier combinations (including the factored free modifiers), and writes
`cases/generated/SM89.json`. Run through `uv run` in the normal development environment.
If manifests are missing, first run
`uv run sass-build-languages SM89 --fp exact --generate-only --coverage`.
`--manifest-dir DIR` selects another manifest directory. Omit `--write` for a dry run.
Rerunning replaces the plan deterministically; prior generated cases are excluded
from the comparison. `--check` fails if regeneration would change the saved plan.
Plans are committed for all ten supported architectures; regenerate each affected plan.

Plans store class/form choices, named input profiles and native assertions, not
captured outputs. The normal `emulator_cases` loader expands them into shared cases for Python, Ghidra, and the GPU, calculating
expectations from the independent references at runtime. Plans cover BREV, POPC,
SGXT, PRMT, UPRMT, ISETP, UISETP, IMNMX, SEL, USEL, IDP, SHF, USHF, IABS,
UMOV, BMSK, VIMNMX, VISET, VIADDMNMX, VIMNMX3, LOP3, ULOP3, IADD3, UIADD3, LEA, ULEA, IMAD, UIMAD, UBREV, UPOPC, USGXT and MOV register-pair forms through
`harness/autocases.py`. The planner routes IDP and packed-integer cases to `int_simd`, BMSK to `bmsk`, arithmetic/carry adapters to `carry`, and the other
operations to `int_alu`. Comparison cases exercise both predicate outputs, .EX's
low-word result, signed boundaries, predicate inversion and input/output aliases.
Dot-product cases cover lane signedness and negated accumulators; funnel shifts
exercise counts around both 32- and 64-bit wrap/clamp boundaries. CX setup uses the
same descriptor/header helpers as the manual cases on each architecture. Strict
encoding and a decode round trip reject forms that the chosen operand setup cannot preserve.
Add an adapter only when an independent reference and operand setup are available.

`harness/autocase_profiles.py` declares edge values, seeded random corpora and
representative supplements. Ordinary cases for adapter-owned operations belong there,
rather than in parallel manual loops. Retained manual regressions do not suppress
generation of the same form. Each retained case for these operations must have a
`@retained(reason)` justification, checked by a test. Current reasons are FLO's
partially modelled predicate outputs (explicit PT-only subset), PRMT/UPRMT zero
registers and p-code costs, ISETP's constant PT input, SHF p-code cost, IDP CX
descriptor/header regressions,
and opaque IMNMX outputs on newer architectures. Operations without adapters remain
manual until an independent reference and operand setup support migration.
BMSK profiles preserve the complete 0..33 position/width domain and seeded edge
pairs. Its manual cases retain descriptor bounds, unusual headers, special
registers and guard behavior. IABS profiles include the signed minimum and the
former random inputs; UMOV retains explicit p-code cost tests. MOV/UMOV register-pair
profiles verify both words; partially modelled pixel-mask forms stay manual and
unresolved in the planner.
Packed min/max and comparison profiles preserve the former seeded lane patterns.
They exercise every supported lane width, signedness, comparison, predicate inversion
and RELU form, with destination aliases. Three-input profiles also exercise
modular lane addition, B-source negation, three-way min/max and aliases with each source. VIMNMX forms with unresolved predicate
outputs retain explicit PT-only manual coverage.
LOP3/ULOP3 profiles cover all 256 truth tables with all eight Boolean input
combinations packed into each byte. Edge and seeded profiles check zero/nonzero
predicate results, POR/PAND, predicate inversion, register aliases and the former
manual selector/random corpora. Constant PT/UPT inputs and the XOR p-code cost
contract remain explicit manual regressions.
Arithmetic profiles preserve seeded carry vectors, LEA shifts, wide IMAD addends
and dense negation boundaries. References retain the carry from ordinary negation
and distinguish .X complement/carry inputs. The IMAD immediate adapter explicitly
recognizes its printing alias while retaining strict checks for all other class/form
changes; printing-only alternate plan rows remain explicit adapter gaps. Constant
UPT inputs, overlapping predicate outputs, p-code costs and hand-calculated width
regressions stay manual.
Hand-calculated PRMT examples also check the reference directly in `test_refs.py`.

The JSON report counts manual, generated, blocked, and unresolved forms. Unsupported
operations are grouped by class to keep millions of gaps manageable; their exact
forms remain in the manifest. Opaque and partially modelled combinations are counted
as unresolved rather than covered. These counts measure instruction forms, not all
input values or proof of correct behavior. After changing a plan or adapter, run the
shared Python, Ghidra and GPU tests and `uv run sass-refresh-coverage`. Generating a
plan gives no GPU evidence; only an actual passing hardware run does.

The remaining-operation adapters in `harness/autocase_remaining.py` use memory and
warp references for values, and supplied synchronization handlers for event contracts.
An explicit `branches: true` in a structured p-code limit permits generic-memory
window routing; its exact named-effect guard still rejects value/opaque primitives.
MUFU error profiles are diagnostic contracts only: the planner leaves numerical
coverage blocked until an independent hardware-specific approximation oracle exists.
Synchronization profiles do not claim to validate a multithread scheduler.

### Shared-case hardware evidence

`baseline/gpu_case_passes.json` records a passing family's selected inputs/expectations,
exact instruction words, sampler mode, probe-source digest, device and executed cubin
hashes. Changing any of these invalidates its evidence. The digest covers only the sources
that build, run and compare a probe (the import closure of `harness/device_cases.py`,
`device_setup.py` and `device_runner.py`, plus `out/SM89`), not semantics, references
or case lists, whose effect on a family is already in its signature. A partial or
failing family run drops that family's shared-case evidence; unchanged other families
remain recorded. `tests/evidence.py` credits the target instruction directly as
`cases/FAMILY`, independent of wrapper instructions and whole-kernel data-path slicing.
Evidence refresh needs neither a GPU nor Ghidra and does not execute recorded passes.

### Raw single-instruction probes

Use `Cases.probe(cls, fields, registers, observe=[...], ...)` and
`tests.harness.device_cases.observe(cases)` to discover outputs without a reference.
It uses the same generated wrappers and setup adapters, returning raw registers and
observed memory without comparisons or evidence updates. `python -m tests.probe
INPUT.json --out OUTPUT.json` accepts a JSON list of the same arguments. Draft probes
cannot enter a regression suite until converted to `c.add` with an independent
reference. Whole-kernel discovery through `tests.observe` remains available.
The investigation skill's [probe guide](../.agents/skills/sass-opcode-semantics/references/gpu-probes.md)
contains runnable examples and memory/warp setup notes.

The extended adapters in `harness/autocase_extended.py` cover FADD/FMUL/FFMA,
DADD/DMUL/DFMA, FSEL/FMNMX/FMNMX3, FSET/FSETP/UFSETP/DSETP, FCHK/FRND,
PLOP3/UPLOP3 and P2R/R2P/UP2UR. Floating profiles include signed zeros,
subnormal, finite, infinity and NaN inputs. Arithmetic skips undefined NaN payload
comparisons and the documented Ghidra RN underflow band, matching the existing
reference policy. Readable formulas have separate expectations. Source modifiers are supported for FSEL/UFSEL, FRND/UFRND, FCHK, DSETP and DADD/DMUL/DFMA.
Other floating source-sign modifiers, FMZ and XORSIGN remain explicit gaps
with retained manual numerical corpora. Existing manual numerical
sweeps remain because they cover denser seeded inputs, exponent boundaries,
exceptional results and costs absent from the new form profile. Predicate profiles
exercise all eight LUT inputs and masked transfers. Repeated extended-adapter gap
reasons are counted by class to keep reports compact.

The 2026-10-18 follow-up adds UFADD/UFMUL/UFFMA/UFSEL/UFRND and supports
absolute/negated sources on FSEL/UFSEL, FRND/UFRND and FCHK. The named
`fchk_boundaries` profile preserves the old manual exponent/seed corpus, while
fixtures retain the identical full 1,792-pair corpus. Ordinary manual FCHK cases
are therefore removed. Source modifiers on other floating operations, FMZ and
XORSIGN still have explicit gaps and retained numerical regressions.

Generated system profiles use an integer `__max_ops` for the existing strict
nonempty, branch-free, CALLOTHER-free limit. A structured `__max_ops` value,
`{"count": N, "userops": [...]}`, additionally requires the exact named userops
and rejects every other runtime call. LDC/LDCU permit only `sass_memory_order`,
with one runtime event; NOP requires zero p-code operations and zero events.
Structured limits require inspection metadata, including the userop names.
The existing `__native` assertion stays strict. QSPC values run in Ghidra's
explicit provenance context and remain marked as primitive checks rather than
native Python value validation.
