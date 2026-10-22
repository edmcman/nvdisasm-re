# Tests

Each test checks one artifact against one oracle, so a failure points at one component. Tests are
grouped by what they need to run; markers say only that: `ghidra` (compiled SLEIGH languages and
Ghidra's pyghidra venv) and `gpu` (an SM89 or SM90 device).

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
- **Before committing:** the Python and Ghidra tests on every architecture and lowering, plus the
  GPU tests if shared cases, fixtures, references or the GPU harness changed.

GPU tests detect device ordinal 0 before collecting cases; this host's H100 NVL selects
SM90. An explicit `--arch SM90` (or a list/`all` containing SM90) must include that
device; only its architecture runs on hardware. CPU-only tests still default to SM89.
Both shared cases and compiled whole-kernel fixtures use the selected architecture.
Whole-kernel Ghidra tests support `--arch SM89,SM90 --fp all` without a GPU.
The MUFU sample fixtures compare the same 1,462 recorded inputs per operation on
both devices; their GPU pass records remain architecture-specific.

CI (`.github/workflows/ci.yml`) runs the Python tests on every architecture and lowering for every
push and pull request, and fails when a generated plan (`tests.autocases --check`) or the committed
coverage and evidence are stale.

### Cost

Rough wall times and peak memory (summed PSS) on a 32-core, 93 GiB machine with other work running
(2026-10-16):

| Run | Time | Memory |
|---|---|---|
| Python tests, SM89 | 25 s | 10 GiB |
| Python tests, every architecture and lowering | 3 minutes | 13 GiB (25 GiB with `--exhaustive`) |
| language build (about 17 compilers at once) | 1.75 minutes | 27 GiB |
| Ghidra tests, all languages | 8 minutes cold, 2.5 minutes cached (mostly building the cases); 2 hours cold for `--arch all --fp all` (2.2 million selected cases, load 55-85 from two other suites, 2026-10-22) | 25 GiB |
| GPU tests | 15 s | |

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
| `test_build.py` | does coverage collection leave the generated languages alone? | `sass.sleigh.gen` with and without coverage, runtime manifests | the same build without coverage |
| `test_family_lowering.py` | does every family lower on every class it covers? | `sass.semantics` families under the SLEIGH enumeration | no crash |
| `test_lifted_semantics.py` | do the semantics compute the right values? | `sass.semantics.lifter` + `evaluate` | `refs/`, through the emulator cases |
| `test_mufu.py` | does the MUFU runtime match hardware? | `sass.semantics.fp.mufu`, the runtime's MUFU primitive | `refs/mufu.py`, recorded SM89 outputs |
| `test_autocases.py`, `test_*_autocases.py`, `test_half_comparison_profiles.py` | are generated cases reproducible and reference-backed? | the [planner and adapters](#generated-shared-cases) | saved plans, independent references |
| `test_sampling.py`, `test_evidence.py`, `test_device_cases.py` | do sampling, evidence and GPU-probe construction behave? (no GPU needed) | `harness/sampling.py`, `tests/evidence.py`, `harness/device_*.py` | hand cases, mocked runners |
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
| `test_cases_on_gpu.py` | do shared instruction cases agree with hardware? | `refs/`, through generated native probes | SM89/SM90 GPU; passing complete families are fingerprinted in `baseline/gpu_case_passes.json` |
| `test_refs_on_gpu.py` | are the references right on hardware? | `refs/`, through the GPU fixtures | SM89/SM90 GPU (CLI `uv run python -m tests.observe FAMILY`); records passing fixtures' fingerprints in `baseline/gpu_passes.json`, which with `gpu_case_passes.json` is the source of `gpu` evidence (`tests/evidence.py`, per class and per form: the modifier combinations that ran) |
| `test_assemble.py::test_native_cubin_on_gpu` | do native cubins load and execute? | self-contained kernels assembled without compiled templates | SM89/SM90 Driver API: parameters, branches, constants, shared memory, multiple entries |

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

## Writing tests

Each `cases/<family>.py` (listed in `cases/__init__.py` `FAMILIES`) provides `emulator_cases(c)`
and, optionally, `gpu_fixtures()`.

### Shared cases: `emulator_cases(c)`

Single-instruction cases for `test_lifted_semantics`, `test_pcode_semantics` and
`test_cases_on_gpu` (hardware expectations use exact mode).

`c.add(cls, fields, registers, expected, ...)` describes one instruction of md class `cls` with
operand `fields` (encoded by `c.encode_words()`, which the loader `cases.emulator_cases` runs on
the selected cases), seeds `registers`, and checks `expected`; classes the architecture lacks are skipped.
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

### GPU fixtures: `gpu_fixtures()`

`harness/gpu.Fixture`s for `test_refs_on_gpu` and `test_fixtures_emulated`. A fixture is a CUDA
kernel (`kernels/*.cu`, compiled for the selected architecture, sm_89 or sm_90, optionally with patched SASS words:
`harness/scalar.py`, `harness/templates.py`) and its reference outputs `expected`. Each thread reads
three inputs and writes one output. The GPU output is compared with `expected` using `same`; the
kernel, run whole in the emulator from entry to EXIT, is compared with `expected` using
`emulator_same` (default `same`), with `excused` admitting known emulator defects.

Patched carry-kernel slots (`templates.slot_fixture`) must leave UR4/UR5 alone: the kernel loads
its default memory descriptor there (`ULDC.64 UR4, c[0x0][0x118]` at 0x30) and its final `STG.E`
uses it implicitly, so a value with bit 27 set in UR5 (descriptor bit 59) traps with
CUDA_ERROR_ILLEGAL_INSTRUCTION (715).

Prefer shared cases for single-instruction behavior: the same case then runs in the emulator,
Ghidra and on the GPU, and creates evidence.

#### Multi-thread fixtures

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

GPU vs reference is exact, except F16 NaN payload/sign in F16 packs and packed F16 arithmetic, and
the bits outside the converted width of the wide conversion fixtures. Emulator vs reference
tolerates floating-point NaN payload/sign, and excuses Ghidra 12.1.4's minimum-subnormal underflow
(fp16/32/64) only on its exact signature. FP8 emulator cases compare NaN class only (`__fp8_nan`).

## Generated shared cases

### Running the planner

```sh
uv run python -m tests.autocases SM89 --write --report tests/cases/generated/SM89.gaps.json
```

reads the exact-language coverage manifest, expands its legal modifier combinations (including the
factored free modifiers), and writes `cases/generated/SM89.json`. If manifests are missing, first
run `uv run sass-build-languages SM89 --fp exact --generate-only --coverage`. The committed
`cases/generated/SMxx.gaps.json` are `--report` outputs.

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

The JSON report counts manual, generated, blocked and unresolved forms. Unsupported operations are
grouped by class to keep millions of gaps manageable; their exact forms remain in the manifest.
Repeated extended-adapter gap reasons are likewise counted by class. Opaque and partially modelled
combinations are counted as unresolved rather than covered. These counts measure instruction forms,
not all input values or proof of correct behavior.

### Integer adapters (`harness/autocases.py`)

Cover BREV, POPC, FLO, SGXT, PRMT, UPRMT, ISETP, UISETP, IMNMX, SEL, USEL, IDP, SHF, USHF, IABS, UMOV,
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
  Partially modelled pixel-mask forms stay manual and unresolved in the planner.
- **PRMT/UPRMT** zero registers and p-code costs stay retained; hand-calculated PRMT examples also
  check the reference directly in `test_refs.py`.
- **FLO**'s partially modelled predicate outputs keep an explicit PT-only retained subset.
- **IMNMX** outputs that are opaque on newer architectures stay retained.
- **Packed min/max and comparison** profiles preserve the former seeded lane patterns. They
  exercise every supported lane width, signedness, comparison, predicate inversion and RELU form,
  with destination aliases. Three-input profiles also exercise modular lane addition, B-source
  negation, three-way min/max and aliases with each source. VIMNMX forms with unresolved predicate
  outputs retain explicit PT-only manual coverage.
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

Cover LD/LDL/LDS/ST/STG/STL/STS, ELECT/MATCH/REDUX/SHFL/VOTE/VOTEU, BAR/BMOV/BREAK/BSSY/BSYNC/
WARPSYNC and MUFU. These use memory and warp references for values, and supplied synchronization
handlers for event contracts. Synchronization profiles do not claim to validate a multithread
scheduler. Full-width MUFU SIN/COS (`mufu__*` classes) use the SM89 fixed-point reference
(`refs/mufu.py`, profile `mufu_sincos`); the other MUFU operations get error contracts only, which
are diagnostic, and the planner leaves their numerical coverage blocked until an independent
hardware-specific approximation oracle exists.

`__max_ops` limits in generated system profiles:

- An integer keeps the strict nonempty, branch-free, CALLOTHER-free limit.
- A structured value, `{"count": N, "userops": [...]}`, additionally requires the exact named
  userops and rejects every other runtime call. Structured limits require inspection metadata,
  including the userop names. LDC/LDCU permit only `sass_memory_order`, with one runtime event; NOP
  requires zero p-code operations and zero events.
- An explicit `branches: true` in a structured limit permits generic-memory window routing; its
  exact named-effect guard still rejects value/opaque primitives.

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
  fields (`sampling.declared`, one encoding per combination), and `encode_words` checks each
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

`baseline/gpu_case_passes.json` partitions records by architecture and records a passing family's selected inputs/expectations, exact
instruction words, sampling percentage and algorithm version, probe-source digest, device and
executed cubin hashes. Changing any of these invalidates its evidence. The digest covers only the
sources that build, run and compare a probe (the import closure of `harness/device_cases.py`,
`device_setup.py` and `device_runner.py`, plus the target architecture’s `src/sass/md/data/SMxx`), not semantics (but softfloat), case
lists or references (but the `refs/bits.py`/`fpformat.py` helpers the harness imports), whose
effect on a family is already in its signature.

A partial or failing family run drops that family's shared-case evidence on the tested architecture; unchanged other families
remain recorded. `tests/evidence.py` credits the target instruction directly as `cases/FAMILY`,
independent of wrapper instructions and whole-kernel data-path slicing. Evidence refresh needs
neither a GPU nor Ghidra and does not execute recorded passes. The signature is order-free (the sum
of per-scenario sha256 digests), so the families' cases are classified and checked shard by shard
(`harness/device_evidence.py` `summaries`, over `harness/sharded.py`); reference evidence streams
the same shards on every core.

### Raw single-instruction probes

Use `Cases.probe(cls, fields, registers, observe=[...], ...)` and
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

## Caches

Everything below lives in `.cache/` and is safe to delete.

| What | Keyed by |
|---|---|
| nvdisasm oracle results (`oracle.sqlite`) | nvdisasm hash, word, address |
| parsed md IR (`ir/`) | md files and parser sources |
| real-corpus words (`corpus/`) | the cubin set (paths, sizes, mtimes) |
| Ghidra decode/emulation results (`ghidra/`) | the language's `.sla`, specs and runtime manifest, the `src/sass` sources the Ghidra program imports, the Ghidra install and venv paths; then the request |
| compiled kernels (`kernels/`), block emulator (`java/`), extracted library cubins (`cubins/`) | sources and flags |

## Prerequisites

| What | Default | Override |
|---|---|---|
| target nvdisasm (CUDA 13.4, V13.4.92) | the dev group's `nvidia-cuda-nvdisasm` in `.venv` or the uv cache | `NVDISASM` |
| Ghidra 12.1.4 and its pyghidra venv | `~/Ghidra/ghidra_12.1.4_PUBLIC`, `~/.config/ghidra/ghidra_12.1.4_PUBLIC/venv` | `GHIDRA_INSTALL_DIR`, `GHIDRA_PY` |
| `nvcc`, `ptxas` (CUDA 13.0.88: GPU-fixture kernels for the Ghidra and GPU tests; `ptxas` evidence) | the dev group's wheels in `.venv`, else `/usr/local/cuda/bin` | `NVCC`, `PTXAS` |
| `cuobjdump` and the CUDA libraries (real corpus) | `/usr/local/cuda/bin/cuobjdump`, `/usr/local/cuda/lib64` | `CUOBJDUMP` |
| generated languages | `processor/SASS/data/languages` | `SASS_SLEIGH_OUT` |
| `javac` (cooperative GPU fixtures) | on `PATH` | |

Detailed coverage enumeration is opt-in (`sass-build-languages --coverage`). Normal builds write
only the lightweight runtime manifest needed by primitive emulation. `sass-refresh-coverage` and CI
enable coverage explicitly.

For a scratch build, run `uv run sass-build-languages SM89 --output /tmp/sass-languages` (it copies
the shared definitions), then set `SASS_SLEIGH_OUT=/tmp/sass-languages` for the Ghidra tests.
