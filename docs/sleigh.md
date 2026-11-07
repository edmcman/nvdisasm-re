# SLEIGH generation (`src/sass/sleigh/`, `processor/SASS/`)

- Generation is deterministic: `pattern()` orders each pattern's conjuncts canonically (callers collect clauses from sets, so the order varied with the hash seed).
- `uv run sass-gen-sleigh SM89 ... [--fp=readable] [--coverage]` writes `processor/SASS/data/languages/sass_smXX.slaspec` (`sass_smXX_readable.slaspec` for the readable lowering), `sass_smXX.pspec` (shared by both lowerings), and `sass.ldefs` (ids `SASS:LE:64:smXX`, `SASS:LE:64:smXX_readable`); these are gitignored. `sass_common.sinc`, the processor-spec template `sass.pspec` and `sass.cspec` are hand-written and committed. Detailed coverage enumeration (`_coverage.json`, the only manifest: primitives carry their arguments, so emulation needs none) requires `--coverage`. `sass-refresh-coverage` enables detailed coverage automatically. `uv run sass-build-languages [SMxx...] [--fp F] [--generate-only] [--coverage] [--jobs N]` (`src/sass/sleigh/build.py`) generates and compiles with `support/sleigh` (~5 s/arch; heap capped, see AGENTS.md) and checks the `.sla` size limit below. Ghidra also recompiles a stale `.sla` on load. On a fresh machine, the first `support/sleigh` launches save the JDK path to `$XDG_CONFIG_HOME` (or `~/.config`)`/ghidra/<version>/java_home.save` through `AppConfig.saveJavaHome`'s `if (!dir.exists() && !dir.mkdirs()) throw`, so concurrent first launches race: losers fail with `Failed to create directory` and `Unable to prompt user for JDK path, no TTY detected` (most CI `ghidra-tests` jobs, 2026-10-22). `compile_all` therefore launches `support/sleigh` once, bare, before starting the compilers.
- Constant-bank labels (2026-10-28): `src/sass/sleigh/processor_spec.py` extends the template using `launch.LAUNCH_LAYOUT`, compiler-established descriptor offsets (`docs/testing.md`), and the assembler's established ABI profiles. Bank-0 addresses have names for block dimensions, the local stack and generic windows, known default descriptors, and `kernel_arguments` (0x160 on SM75–SM89, 0x210 on SM90, 0x380 on SM100/SM101/SM103/SM120). Descriptor labels cover SM80/SM86/SM89 at 0x118, SM90 at 0x208, and SM100/SM101/SM103/SM120 at 0x358; the GPU harness's narrower descriptor map must not limit labels on other targets. SM107 has no established layout and receives none. A read-only, uninitialized 64-KiB `cbank` block ensures the importer applies processor labels even when its general processor-label option is disabled; no launch values or argument types are inferred. Updating the processor does not populate existing program databases: `/vine/resnet18.cubin` still lacked both the bank and symbols even though its loaded SM80 language knew the labels. Reimporting applies the defaults; an existing database can also be upgraded by applying the loaded language's missing default memory blocks and symbols in a transaction. The generated per-architecture processor spec is part of release archives and the Ghidra cache key.
- `src/sass/ghidra_bridge/ghidra_decode.py` (run with Ghidra's pyghidra venv) loads a language straight from the `.ldefs` via the package-private `SleighLanguageProvider(ResourceFile)` constructor (reflection) -- no install. `uv run sass-compare-ghidra ARCH [real|synthetic|mutated] [N] [--fp exact|readable]` (`src/sass/check/ghidra_disasm.py`) diffs it against pydecode with `FLOAT_HEX` (SLEIGH can't print floats); guard shown as `@P0:` (SLEIGH drops the space before a mnemonic) and operand whitespace ignored.
- Kernel arguments (2026-10-28): `processor/SASS/src/main/java/sass/SassKernelArgumentsAnalyzer.java` is enabled by default during automatic analysis. It parses `EIATTR_PARAM_CBANK` and `EIATTR_KPARAM_INFO` in each `.nv.info.<kernel>` block, validates ordinals and byte ranges, and defines `arg_N` with custom `cbank` storage on the corresponding function. These are function-local parameters, so kernels with different layouts can share the same bank addresses. Sizes come from metadata; source names and pointer/scalar types are not guessed. Explicit user signatures are preserved. A single global `kernel_arguments` label cannot provide function parameters. `src/sass/sleigh/processor.py` compiles the analyzer against the selected Ghidra installation; normal language builds populate `processor/SASS/lib/SASS.jar`, and the complete processor release archive ships it. Integration tests exercise Ghidra's analyzer discovery and automatic dispatch, two different layouts, both decompiled bodies, and preservation of user signatures.
- Argument region before analysis (2026-10-28): `SassCubinLoader` is automatically selected for `EM_CUDA` ELF files and delegates ELF layout, options, and architecture selection to Ghidra's ELF loader. After default memory blocks are created, it defines `kernel_arguments` as a 4096-element `byte` array, using the address and size from the processor spec. This gives interior bank addresses a global extent before the kernel-argument analyzer runs: on SM80, `cbank:0x170` displays as `kernel_arguments[16]`, equivalent to byte offset `+0x10`. Processor-spec symbol `size` alone does not define data in Ghidra. An `undefined1` array is insufficient because Ghidra's ordinary Reference analyzer may split it into separate data items; a `byte` array preserves the extent. Function-local metadata types still take precedence inside each kernel, including different parameter sizes at the same bank address. The 4-KiB default matches the assembler's supported parameter-buffer limit; it does not imply that a particular kernel uses all of it. The loader and analyzer are shipped together in `SASS.jar`.
- Thread/block coordinates (2026-10-28): `sass.cspec` marks the six `SR_TID.X/Y/Z` and `SR_CTAID.X/Y/Z` registers as global state. Ghidra otherwise treats them as irregular function inputs and generates `in_SR_TID_X` names. `sass.pspec` renames their display names to `threadIdx_x/y/z` and `blockIdx_x/y/z`, retaining the original names as aliases. Storage and S2R semantics remain identical; scalar register aliases are tested by seeding the original names and executing S2R. Decompiler checks cover every axis, both floating-point languages, and analysis with or without kernel-parameter recovery. The values occupy register space; the compiler-spec global marking does not allocate CUDA global memory. Dots in standalone variable names are sanitized to underscores by Ghidra, so the specs use valid C identifiers explicitly.
- SLEIGH facts that shaped the generator: fields joined with `&` must be in one token, so every pattern is `(lo ; hi)` and patterns combine with `&`; subtable coverage is not folded into the parent's pattern (a subtable with no matching constructor makes the instruction fail, no fallback), so class discrimination is all in root constraints; overlaps are lenient (first in file order) unless nested; an operand may not be defined in several OR branches; a field can't be constrained and displayed in one pattern (separate aliases); `!=`/`<` on wide fields splits into huge state sets (16-bit `!=0` hung the compiler at 25 GB). Membership constraints now use aligned positive bit blocks. Pseudo-op scheduling validity uses a first-in-order blocker constructor.
- Ghidra silently truncates decompressed `.sla` data at 16 MiB (`SlaFormat.MAX_FILE_SIZE` / `LinkedByteBuffer.ingestStream`), causing random "Format violation" errors. Ghidra master (12.3) raises the limit to 2 GB (GP-7358, `dea9863592`, 2026-10-08, the C++ decompiler reader never had it). Ghidra 12.2 (2026-10-22, built from `8ca9077`) was branched before it and keeps `MAX_FILE_SIZE = 16777216` (checked 2026-10-23). `LIMIT` in `src/sass/sleigh/build.py` follows the oldest Ghidra the module supports (12.1.4), not the compiler, since a larger `.sla` breaks there; lift it, and revisit the size workarounds (`Instruction.select_lookup`, `Instruction.select_in` in `src/sass/semantics/family.py`), once every supported release has the fix (12.3 at the earliest). SM107 exact is the closest (16,156,776 bytes; SM90 exact 15,848,575; 2026-10-22, after loaded-operand masks cut SM90 exact from 16,745,311). 2026-10-24: SM107 exact 16,173,549 and SM90 exact 16,180,332 (main: 16,153,572 and 15,918,984): making every HFMA2.MMA class and HFMA2/HFMA2.MMA `.RELU` native costs about 7-10 KB per newly native class, as an existing HFMA2 class does (SM89 exact: +165 KB for 17 MMA classes, +127 KB for 18 RELU classes). Earlier large-OR failures were size failures. Shared display fields and per-class semantic span subtables keep all twenty languages (ten architectures, exact and readable) under the cap (largest: SM107 exact, above). Class span tables enumerate only output (and native/primitive) spans; read-only expression-sized inputs of opaque classes get per-operand subtables zero-extended to their widest view (SLEIGH requires one export size per subtable; >256 bytes uses `DYN_<operand>_<n>` registers). Enumerating all spans jointly made TEX tables 432 rows.
- Ghidra 12.2's emulator API (GP-5976, 2026-10-23): `PcodeExecutor.execute(List, Map, library)` became `execute(List, library)`, which calls the library's default `getSymbols`, and `PcodeUseropDefinition` gained `canInterrupt`/`isOutSigned`/`isInSigned`. JPype 1.5.2 proxies cannot run interface default methods (`NoSuchMethodException ... invokeSpecial`), so `ghidra_emulate` runs `PcodeProgram.fromInstruction` (in 12.1.4 and 12.2) with the Python userop library composed into the thread's Java one, as kernels already did.
- Ghidra 12.2 leaks every userop library an executor runs (2026-10-23): `PcodeExecutor.execute` calls `getSymbols`, which caches a `SymbolsCache` per library in the static `WeakHashMap` `PcodeUseropLibrary.SYMBOL_CACHE`, but `SymbolsCache` holds its library strongly, so no entry is ever cleared. A thread's library holds the thread, so every `PcodeEmulator` that stepped stays reachable with its state and `Disassembler` (unchanged on master). The SM89/SM90 fixture kernels, one emulator per lane, grew each session to 4-7.5 GB of live heap (12.1.4: about 130 MB), so on 4-CPU, 16 GB runners the CI jobs went from 16 minutes cold to timing out at 60; locally on 4 CPUs the run takes 8.5 minutes and 2.6 GB with the fix. `ghidra_emulate` clears `SYMBOL_CACHE` after every request.
- Shared half expression bodies (2026-11-02): BF16 fused arithmetic and packed F16 FTZ arithmetic
  retain their defined calls during SLEIGH lowering. Each argument has an operand-form subtable,
  constrained by its instruction class and enclosing decode-time choices; one expression subtable
  computes the result. SAT/RELU remain at the caller. Defined labels alone did not share emitted
  bodies: ordinary expansion repeated them across operand classes and modifier forms. Explicit
  sharing reduced SM90 exact from 17,982,581 to 16,705,477 decompressed bytes. SM89 exact is
  14,987,932 and SM107 exact 16,568,533. All twenty languages compile below the unchanged cap
  without dead temporaries. Tables are emitted after their dependencies because SLEIGH forbids
  forward references. `lzcount` replaces repeated bit-position searches in the shared integer
  rounding machinery; zero is handled by the caller's `sig | 1`.
- Pure-definition calls (2026-11-04): HMMA products and accumulation blocks,
  floating ATOMG updates, dense HMMA fragments, scalar half/BF16 FMA and selected
  F64-to-half narrowing definitions share bodies through internal argument/result registers.
  Callers write every argument before an explicit build and copy the result
  immediately. Distinct forwarding operands allow repeated calls in one
  instruction; SLEIGH rejects repeated builds of the same operand. Nested calls
  use disjoint registers. Arithmetic remains a pure IR definition.
  Class-filtered families no longer disable independent input-span tables in
  unrelated opaque classes of the same opcode. Small predicate results share the
  main body; predicate inputs used only by discarded results have decode-time
  constant substitutes, including inside modifier choices. Complete-discard
  pruning and larger liveness savings retain separate bodies. Savings are measured
  after narrowing the surviving expressions: omitting IADD3 carries removes the
  wide sum as well as its comparisons. IADD3/UIADD3 retain their two-operation
  default forms, and LOP3 retains its three-operation XOR form.
  Sharing is selective: argument/result copies and operand constructors can
  outweigh a small numerical body, and explicit builds expand into P-code rather
  than providing runtime function calls. Scalar half FMA sharing saves about
  21 KiB in SM107 exact after restoring integer pruning and rejecting invalid
  span selectors. Dense TF32 support required additional fragment/FMA/narrowing
  sharing and decode-time source-negation masks; SM107 exact is 16,753,273 bytes
  with these definitions (2026-11-04). Sizes are checked against the limit on
  every build. Shared F64-to-F16/BF16 narrowing loses constant specialization:
  SM107 exact `hmul2__RR` grows from 150 to 194 operations and `hmul2_fixed__RU`
  from 271 to 313. All twenty languages' FP16 numerical and dataflow checks pass;
  the extra operations buy about 73 KiB of required language-size savings.
- Ghidra 12.2 locks each `.sla` while loading it (`sass_<variant>.sla.lock` beside it, GP-4045), and a `sass.ldefs` entry whose `.slaspec` is missing ("Missing sleigh file(.slaspec)", only in `application.log` when headless) stops the provider reading the rest of the file: the languages after it are silently absent and `getLanguage` returns null (2026-10-23; a committed `sass_sm89.sla.lock` listed sm89 in every CI job and lost sm90, sorted after it). `gen.write_ldefs` lists only variants with a `.sla` or `.slaspec`.
- Size compromises (the 16 MiB `.sla` limit above). Each trades run-time p-code, readability or
  generator simplicity for size; revisit them when the limit is lifted.
  Removal is tracked in [#120](https://github.com/edmcman/nvdisasm-re/issues/120)
  (2026-11-05). Once every supported release includes GP-7358, remove size-only
  pure-definition sharing (`SHARED`/`SCRATCH` and shared-call lowering in `lower.py`)
  and restore constant specialization. Keep the reusable pure definitions and arithmetic
  improvements; retain sharing only for demonstrated benefits independent of the old limit.
  Review operation counts, numerical/dataflow checks, decompiler output and performance
  across all ten architectures and both floating-point modes. The measurements in
  [semantics.md](semantics.md) record added p-code, not measured execution-time regressions.
  - `Instruction.select_lookup` (and `select_lookup_const`) instead of `select_fork`: a selector's
    label picks a value at decode time rather than giving each label its own body. Used for swizzles,
    HMNMX2's per-format NaN test and order (forking made SM90 18.75 MB), FSETP's comparison, and the
    conversions' rounding modes, formats, signedness and NTZ (forking made SM86 exceed the limit).
  - `Instruction.select_in`: FMUL/FFMA .FTZ and .FMZ share one body, so FTZ forms run 9-17 more ops
    (`ffma__RRR_RRR` 48 -> 65; a plain fork made SM90 16,848,482 bytes, 2026-10-20; `docs/open.md`).
  - The row merger's `cost` (`lower.py`: generated nodes + 8 x operations on the longest path) picks
    per-output choices or whole bodies split on a key. A choice subtable can't reuse values computed
    elsewhere, so the cheaper size can cost run-time ops, and a whole-body split repeats what the
    bodies share (FADD2/FHADD2 bodies per rounding mode each repeat their lanes' NaN select).
  - Ordered decode alternatives share an identical statement prefix when none of its temporaries
    are needed by the remaining alternatives (`ordered.common_prefix`, 2026-11-06).
    This keeps runtime branches and their state accesses intact and needs no scratch transfers.
    An isolated SM90 exact comparison reduced decompressed size from 15,934,896 to 15,735,834 bytes
    (199,062 saved). All 7,762 sampled legal words retained their p-code operation counts;
    7,759 retained identical p-code after temporary renaming, and three LDGSTS words moved a pure
    predicate negation after the memory-order operation, matching the ordered body.
    Generation CPU time was 55.468 versus 55.533 seconds on the same inputs.
    After the target-address correction below, all twenty languages compile with zero dead
    temporaries; the largest is SM90 exact at 15,735,814 bytes, 1,041,402 below the limit.
  - `shared_subtrees` hoists every shared subtree into a value table (the smallest SM89 `.sla`), except
    ones holding a loaded operand, which would load again.
  - Loaded operands (c[], cx[]): a mask modifier (|x|, -x: `v & K`, `v ^ K`, `v | K`) chooses its
    constant at decode time and applies it to the one load; other modifiers fork the rows so each
    keeps the load in one body (`Lower.modified`). Until 2026-10-22 every modifier forked, which
    copied whole bodies per modifier bit (HMNMX2's lane body 24 times) and cost SM90 exact ~0.9 MB.
  - The GPU NaN result is a separate `nanresult` node outside the IEEE operation, so a decode-time
    choice of rounding or scale leaves one NaN select (a pick inside the op made SM90 ~95 KB larger);
    a narrowing to an unsigned-NaN destination drops it (`fpformat.narrow`), and the readable
    lowering strips it (`readable_outputs`). The SM107 pair ops' F16 narrowing (`NARROW_NAN`)
    nominally keeps the NaN's sign, which is always + there: an unsigned F16 NaN cost 866 KB.
  - Shared display fields and per-class semantic span subtables; class span tables enumerate only
    output (and native/primitive) spans, and read-only expression-sized inputs of opaque classes are
    zero-extended to their widest view (enumerating all spans jointly made TEX tables 432 rows).
- Ghidra caches instruction prototypes by a 32-bit CRC of the constructor tree and trusts the hash entirely (`SleighLanguage.parse` `instructProtoMap`, `SleighInstructionPrototype.equals`). SASS languages have so many distinct trees that one process decoding ~64k varied words hits about one collision per architecture: the word reuses another class's prototype (e.g. LEA shown as HMUL2), though it decodes correctly alone. Emulation is exposed too: on 2026-10-19 the full Ghidra suite ran HSETP2 as LOP3 (SM75 readable) and DSETP as FHFMA2 (SM107) for four cases each. Real Ghidra sessions are exposed as well (worth reporting upstream). The bridge (`ghidra_decode.load_language`) replaces the map with `ExactPrototypes`, which builds every prototype, so all decodes and emulation are exact; the former fresh-process `collision` retry is gone.
- Ordered target values (2026-11-06): a branch-target subtable exports a RAM reference for direct
  branches. Passing that reference as an ordinary runtime argument reads memory at the target.
  Ordered value lowering now takes its address with `&:8`; direct control transfers keep the
  original target operand. Forward/backward BSSY and WARPSYNC acknowledgement tests check the
  numerical arguments, alongside suspension and cooperative synchronization tests
  (10 Ghidra tests passed in 90.77 seconds).
- Uniform zero depends on the architecture and the field width. SM75-SM90 define URZ=63. SM100+ define URZ=255 and treat UR63 as an ordinary register, but six-bit fields still encode URZ as 63. `Gen.register_zero` derives the zero from the md, and URZ gets its own register slot outside the 64-register bank.
- Register dataflow p-code: md register reads/writes via opaque pcodeops, instruction guards, and branch/call/return. Modifier spans are evaluated by `src/sass/md/expression.py`, exactly (including 0, 3, and matrix spans above 8), without a widest-span fallback. Immediate operands with misleading latency resources (IPA `attr`) are excluded by operand type. Register-bank overflow uses sinks to preserve display decoding; its dataflow is undefined. Wide zero constants use `zext`; above SLEIGH's 256-byte temporary cap, initialized internal ZERO registers are used.
- `SASS_SLEIGH_OUT=/tmp/...` (`sass.paths.LANGUAGES`) redirects generation and Ghidra tests; copy the shared `.sinc`, `.pspec`, and `.cspec` there first. `tests/test_pcode_dataflow.py` (`tests/harness/dataflow.py`) compares structured Ghidra p-code against decoded md operand roles/spans. `sass-compare-ghidra` compares standalone 64-bit immediates in MOV.64/UMOV.64/MOV64IUR modulo 2^64 because SLEIGH prints computed values as signed int64.

## Ordered-body feasibility (2026-11-05)

The isolated ordered language passes suspension, conditional memory access,
atomic memory dataflow, typed multiple results, collective capture, and numerical
definition checks in exact and readable modes. Captured numerical operands are
instruction-local temporaries. Shared numerical tables therefore receive them
through the existing scratch argument/result registers, rather than referring
to a caller's local temporaries.

Before direct memory and atomic migration, all twenty production languages
compiled with zero dead temporaries. Their decompressed sizes were:

| Architecture | Exact bytes | Readable bytes |
|---|---:|---:|
| SM75 | 11,019,789 | 9,456,124 |
| SM80 | 12,563,865 | 10,935,789 |
| SM86 | 13,408,252 | 11,663,232 |
| SM89 | 14,466,722 | 12,720,611 |
| SM90 | 16,637,594 | 15,002,098 |
| SM100 | 15,368,648 | 12,703,728 |
| SM101 | 13,042,957 | 12,084,199 |
| SM103 | 15,379,562 | 12,714,641 |
| SM107 | 16,735,261 | 13,511,732 |
| SM120 | 14,130,692 | 12,918,271 |

Direct memory and atomic statements initially increased SM107 exact to
17,689,970 bytes. Sharing compatible pure calculation positions, eliminating
temporary forwarding copies, and replacing mapped register/value-display
constructor enumerations with attached fields reduced it to 16,580,503 bytes.
The attached fields preserve non-linear raw mappings, missing-value domains,
and zero-register behavior; zero-register constructors have disjoint domains.
SM89 exact/readable compiled to 15,210,555/13,464,774 bytes and SM107 readable
to 13,356,816 bytes. These builds use the ordered instruction-form merger for
the migrated families. Retain the numerical definition sharing workaround while the
16,777,216-byte limit applies.

Ordered numerical migration exposes more partially specified modifier rows.
`determining_keys` compares incompatible row sets with integer bit masks,
preserving wildcard rows and the existing minimum-key search. On the same
3,474 SM90 `hset2__RC` rows, selecting the guard keys took 0.116 s instead of
9.043 s and returned the same keys (2026-11-05). The exhaustive wildcard tests
also pass. This measures the key search, not full language generation.

The first ordered integer, floating-point and conversion migration produced a
48,642,936-byte SM90 exact language, exceeding the limit. Register-containing
numerical choices forced excessive form enumeration, temporary references were
mistaken for statement structure, and the merger emitted individual bodies for
selector values with compatible structures.

An isolated half-FMA `.RELU` experiment (2026-11-06) captures the arithmetic
inputs and results before the pure conditional rectification. Both predicate
paths require that arithmetic. Discarded destinations still capture nothing.
SM90 exact decreases from 15,735,814 to 15,713,401 decompressed bytes, with zero
dead temporaries. The candidate passes 334 existing reference cases across
architectures and modes, plus 32 focused predicate, alias and discarded-output
checks. On identical SM90 MMA words, p-code arithmetic counts halve; the
constant-memory form retains one visible load shared by both lanes. A decompiler
probe that stores the result shows one arithmetic calculation per lane followed
by conditional rectification. Artifacts are in
`.cache/ordered-gate/relu-sharing-{sm90-build,pcode,decompiler}.log`.

The change is retained with the constant-table LOP3 predicate-capture fix
(2026-11-06). That fix removes irrelevant predicate reads before they become
ordered statements. HSETP2's independent dataflow model now also recognizes
constant comparisons absorbed by AND/OR, while preserving the second output's
dependency under `.H_AND`. The combined source compiled all twenty languages
with zero dead temporaries and passed dataflow checks on 133,100 words. Its
largest language is SM90 exact at 15,820,358 bytes, 956,858 below the limit.
Correctly omitting LOP3 predicate captures creates additional statement-body
alternatives: SM90 body constructors increase from 805 to 888; total constructors
increase from 22,708 to 22,713 after half-FMA sharing. The combined build is
84,544 bytes larger than the preceding production build. The runtime reference
still matches the shared operation registry.

Further count review (2026-11-06) found that splitting an exact BF16 packed
result into separately merged lane expressions emitted the packed calculation
twice. The half-result helper now captures one packed result before conditional
rectification. FMNMX3 keeps its pure numerical definition through form merging,
with caller-supplied captured arguments and ordinary arithmetic p-code.
On identical words, the BF16 half-FMA sample falls from 1,373 to 686 total
operations (679 before the refactor); FMNMX3 falls from 506 to 96 (81 before).
The remaining FMNMX3 increase includes argument and result copies, not repeated
FTZ/comparison calculations. SM107 exact saves 276,344 bytes. All twenty
languages compile without dead temporaries; the largest is SM90 exact at
15,786,247 bytes, 990,969 below the limit. Python and Ghidra each pass all
31,423 inputs from the affected generated profiles; Ghidra dataflow passes on
133,100 words. Focused dependency/merger checks pass 349 tests, including BF16
RELU predicates, aliases, memory operands and discarded destinations. Native
SM89/SM90 half-precision checks pass 274 items in 51.7 seconds. Internal
argument/result registers disappear from the inspected decompiler output.
Artifacts and installation provenance are under
`.cache/ordered-gate/pure-sharing-*`. Operation-count baselines remain unchanged
while other arithmetic outliers are investigated.

Multi-output calculation sharing (2026-11-06) exposed another cause of
duplication: ordered input capture reused reads, but did not bind a calculation
shared by distinct output expressions. Normalization also expanded every pure
`Move`, including reused calculations. Capture now binds common calculations
over captured values, and normalization retains reused calculation assignments.
Traversal follows expression order; it does not search conditional arms for
calculations to move outside their selected path. The discarded unordered
traversal produced unstable body structures and an oversized SM90 language.
SM75 and SM90 specifications now reproduce byte-for-byte across separate builds.
The discarded comparison-definition sharing trial also introduced irrelevant
predicate reads for constant comparisons; its smaller languages were not
retained. Dataflow expectations and numerical comparisons were not weakened.

The retained change halves the sampled SM75 HSETP2 arithmetic and passes 461
focused tests, 364 native scheduling/resumption tests, 14 formal-export tests,
and 3,536 production Ghidra items covering 337,394 selected cases and all twenty
languages' dataflow checks. The existing IADD3/UIADD3 dead-output count guards
also pass: cost estimation expands pure assignments without changing emitted
statements. Across the same 26,920 p-code samples, total operations fall from
912,272 to 891,059. All twenty languages compile with zero dead temporaries.
SM75 exact shrinks by 741,268 bytes; SM90 exact grows by 190,906 bytes because
body alternatives increase from 888 to 1,111. Its 15,977,153-byte language remains
800,063 bytes below the limit. This is a tradeoff between computation sharing
and instruction-form sharing, rather than a uniform size reduction. Some PRMT,
SHFL and architecture-specific floating-point samples still exceed the original
arithmetic counts. Their review remains open; baselines are unchanged.
Artifacts and source-matching installation provenance are under
`.cache/ordered-gate/shared-calculations-*`.

SHFL had the same problem across statement boundaries (2026-11-06): its target
and validity expressions were repeated in the selected-lane test, lane read,
fallback result and predicate output. They now have explicit calculation
assignments before warp capture. On the SM89/SM90 register-form samples,
arithmetic falls from 35 operations to 15, matching the original baseline,
and copies fall from 21 to 11. The existing eight explicit state userops remain.
The change passes 84 reference/native items, including aliased destinations
under all three schedules, and 48 SM89/SM90 Ghidra items including cooperative
kernels. Across all twenty languages, 222 Ghidra items cover 2,272 selected
cases and dataflow. All languages compile without dead temporaries and shrink
slightly; SM90 exact is now 15,974,304 bytes, 802,912 below the limit.
The same 26,920 p-code samples fall from 891,059 to 889,267 operations,
with only the four SHFL classes changing per language. The remaining arithmetic
outliers are PRMT and architecture-specific FMNMX/half-FMA forms. Count baselines
remain unchanged. Artifacts and source-matching installation provenance are
under `.cache/ordered-gate/shfl-sharing-*`.

An isolated live-prefix sharing trial (2026-11-06) transfers captured values
explicitly between SLEIGH subtable scopes. SM90 exact shrinks from 15,974,304 to
15,635,093 decompressed bytes, saving 339,211, and compiles with zero dead
temporaries. The existing 131 merger tests pass. On 7,730 identical legal words,
the arithmetic total and load, store and userop counts are unchanged; 513 samples gain a
total of 1,695 copies. Dataflow rejects 88 candidate samples because exporting
whole captured values loses the original byte-width or decode-dependent input
demand. The control has no dataflow failures on those words. The trial remains
isolated and is not installed; preserving those demands is required before
further consideration. Artifacts are under `.cache/ordered-gate/live-prefix-*`.

An isolated pure-expression merger trial (2026-11-06) restores cost-based
decode-time alternatives for numerical expressions while retaining ordered
statement structure. Its definition-preserving variant compiles SM90 exact to
16,178,245 decompressed bytes with zero dead temporaries and passes 131 merger
tests. On the same 7,730 legal words, dataflow passes and total p-code falls
from 235,788 to 231,211. However, 172 words across 31 classes gain 1,036
arithmetic operations, and 264 words across 50 classes gain 1,684 total
operations. The largest increases are HADD2.F32: arithmetic rises from 35 to
63 and total operations from 56 to 95. Integer forms also regress: scalar
cost decisions duplicate IMAD calculations shared by different outputs and
move SGXT constant calculations from decode time to execution. Aggregate
savings do not justify those regressions. The candidate remains isolated and
uninstalled; operation-count baselines are unchanged. Evidence is under
`.cache/ordered-gate/expression-cost-guard-*`.

The retained expression-merging fix (2026-11-06) measures pure calculations
across all statement outputs together, so a cheaper individual output cannot
duplicate a calculation shared with another output. Floating-point definitions
retain their existing sharing. Bounded decode-time folding distributes a pure
calculation over at most eight selector alternatives; every resulting arm must
be foldable. This restores SGXT's constant shift calculations without moving
state access or calculations out of runtime branches.

All twenty languages compile with zero dead temporaries. The largest, SM90
exact, is 16,143,172 decompressed bytes, 634,044 below the supported limit.
Across 26,920 identical legal class samples, total p-code falls from 889,267 to
856,169 operations. Arithmetic increases remain in twelve samples: F2IP.RCR
gains one operation in SM86/SM90 exact and readable, and four SM107 packed
conversion classes gain eight or eleven operations in each mode. The SM107
conversion changes pass 4,640 existing independent-reference cases across both
modes. These checks establish emulator behavior, not hardware verification on
SM107. All 133,100 broad dataflow words pass. The separate 7,730-word SM90
exact sample falls from 235,788 to 223,085 total operations and from 148,947
to 139,772 arithmetic operations; its sole arithmetic increase is one
`INT_ZEXT` in an F2IP word. Small individual increases are accepted alongside
the overall reduction; count baselines still require review against the
original pre-migration languages.

The same review found that fixed BF16 immediate format rules such as
`convertFloatType(1==1,E8M7Imm,E8M7Imm)` were incorrectly treated as runtime
format selectors. Recognizing identical format alternatives keeps those
operands in their encoded representation. Forty existing BF16 F2I cases,
including enabled and disabled predication, agree between Python and Ghidra
without an operand environment. No generated language now invokes
`sass_operand_value`. This is backend equivalence evidence, not new hardware
coverage.

The complete build took 254 seconds. Isolated SM90 generation took 87.25
seconds versus 58.78 for the preceding source. `nice uvx py-spy` attributes most
of the additional generation work to testing nested selector folding
alternatives; the inclusive folding samples cover 16.49 seconds versus 0.40
previously. This is an offline generation cost and does not add native
execution work. Builds, source-matching installation provenance, counts,
reference checks and profiles are under `.cache/ordered-gate/selector-fold-*`
and `.cache/ordered-gate/fixed-format-runtime-*`.

The retained languages also pass 7,476 integer, predicate, conversion and
packed-conversion test items across all twenty variants. One hundred manual
conversion items initially rejected the explicit deferred-barrier wait while
their numerical results agreed. Their expectations now use the same
machine-description-derived contract as generated cases: ordinary classes
require native p-code, and `DECOUPLED` classes permit exactly the named deferred
wait. The comparator still rejects missing, repeated or additional runtime
operations. All 2,150 conversion items then pass, covering 68,830 cases.
SM89 conversion hardware checks pass 3,939 scenarios; 325 remain emulator-only.
Logs are under `.cache/ordered-gate/selector-fold-{production,conversion-contract}-*`.

Against the original pre-migration class-count baselines, rather than the
preceding ordered candidate, total p-code is 781,446 → 856,169 (+9.56%) and
arithmetic is 528,420 → 542,023 (+2.57%). Individual increases can be larger:
the SM86 exact HFMA2.MMA constant-bank sample has 127 → 248 arithmetic
operations. Ordered lowering retains the numerical size workaround through
generic scratch argument registers, which loses caller constant specialization.
These static counts include all numerical control paths and are not execution
timings. The final review accepts explicit state captures and the retained
numerical size tradeoff. All twenty count baselines now match these reviewed
samples, and the corresponding all-language dataflow and count checks pass.

The backend now normalizes pure assignments and branches before merging, keeps
binding identities distinct from expression alternatives, and groups compatible
selector values under membership guards. Numerical choices with the same
unconditional register inputs retain one explicit capture; memory, environment,
different-register, and fallible special-register choices keep separate bodies.
State accesses in runtime branches retain their positions. Pure calculations
share within a control-flow block, register-expression caches are invalidated
before each fresh read, and nested selectors and shared value tables retain the
ordered lowerer.

The first complete rebuild after these normalization changes compiled all
twenty languages with zero dead temporaries
(2026-11-05). The numerical definition sharing workaround is unchanged.

| Architecture | Exact bytes | Readable bytes |
|---|---:|---:|
| SM75 | 9,873,034 | 8,103,390 |
| SM80 | 10,154,640 | 9,025,237 |
| SM86 | 10,667,104 | 9,490,572 |
| SM89 | 11,803,567 | 10,619,956 |
| SM90 | 13,496,566 | 12,402,475 |
| SM100 | 12,021,193 | 10,190,924 |
| SM101 | 10,516,038 | 9,630,594 |
| SM103 | 12,029,938 | 10,199,879 |
| SM107 | 13,040,486 | 10,873,370 |
| SM120 | 11,143,582 | 10,450,315 |

The subsequent byte-dataflow fixes narrowed retained register captures to the
bytes their consumers need, including decode-dependent widths. Expanding every
capture into an instruction-specific register expression prevented numerical
choice tables from sharing their bodies across forms. SM90 exact reached
16,993,384 bytes. Equal single-key choice arms now share membership guards,
including tables with a fallback arm. A temporary reference costs no arithmetic
operation in the merger's estimate. Choices whose arms all exceed 32 estimated
operations retain canonical captured arguments; choices with a cheap arm keep
direct inputs. This preserves plain FADD's fast path. Capture projections happen
at their original statement positions, and already retained values are staged
without repeating their register reads.

All twenty languages compiled again with zero dead temporaries (2026-11-05).
The largest, SM90 exact, leaves 636,933 bytes below the supported limit. The
existing numerical-definition sharing workaround is unchanged. Experiments
with default compression during width analysis and cost-based whole-body
selection increased compiled size and were removed.

| Architecture | Exact bytes | Readable bytes |
|---|---:|---:|
| SM75 | 12,196,382 | 10,254,523 |
| SM80 | 12,536,852 | 11,388,693 |
| SM86 | 13,186,756 | 11,932,396 |
| SM89 | 14,327,119 | 13,072,098 |
| SM90 | 16,140,283 | 14,939,006 |
| SM100 | 14,361,255 | 11,976,459 |
| SM101 | 12,418,796 | 11,358,113 |
| SM103 | 14,369,949 | 11,985,164 |
| SM107 | 15,398,241 | 12,602,606 |
| SM120 | 13,090,773 | 12,284,563 |

Explicit asynchronous commit slots and retained snapshots for SHFL, VOTE/VOTEU,
32/64-bit MATCH, and REDUX compiled in all twenty languages with zero dead
temporaries (2026-11-05). The largest, SM90 exact, leaves 584,602 bytes below the
unchanged cap. Compared with the build immediately before the new VOTE/MATCH/REDUX
bodies, its growth is 419 bytes. Register and predicate inputs remain visible
before their capture calls; stateful operations are not moved outside branches.

| Architecture | Exact bytes | Readable bytes |
|---|---:|---:|
| SM75 | 12,189,795 | 10,247,835 |
| SM80 | 12,569,739 | 11,421,753 |
| SM86 | 13,219,581 | 11,965,317 |
| SM89 | 14,359,856 | 13,104,843 |
| SM90 | 16,192,614 | 14,991,505 |
| SM100 | 14,429,136 | 12,044,636 |
| SM101 | 12,486,660 | 11,426,277 |
| SM103 | 14,437,827 | 12,053,287 |
| SM107 | 15,466,149 | 12,670,682 |
| SM120 | 13,158,723 | 12,352,774 |

The distinct DEPBAR group-count and slot-mask operations compiled in all twenty
languages (2026-11-05), with zero dead temporaries. SM90 exact remains the
largest at 16,192,864 bytes, 250 bytes above the preceding build and 584,352
bytes below the supported limit. Its readable language is 14,991,754 bytes.
Mask-only DEPBAR emits one user operation; `.LE` emits two, in declared order.
The shared-case eight-op bound remains unchanged.

Ordered LDSM/HMMA word-group captures compiled in all twenty languages with
zero dead temporaries (2026-11-06). SM90 exact remains the largest at
16,181,129 bytes, 11,735 bytes smaller than the preceding DEPBAR build and
596,087 bytes below the supported limit. Its readable language is
14,980,008 bytes. Numerical-definition sharing is unchanged. Matrix operands
are captured before destination writes; LDSM's shared-memory reads remain
ordinary p-code loads.

An instrumented SM90 exact generation (2026-11-06) examined grouping
multi-modifier structural alternatives by identical remaining alternatives.
It found no eligible duplicate groups in this language, so that proposed
merger change was not implemented. The measurement is recorded in
`.cache/ordered-gate/structure-sharing-before.json`; it does not establish
per-class compiled-size attribution or savings from sharing common statements.

Explicit encoded dependency waits compile in all twenty languages with zero
dead temporaries (2026-11-06). The largest, SM90 exact, is 16,250,887 bytes,
69,758 bytes above the preceding matrix build and 526,329 bytes below the
supported limit. Its readable language is 15,049,717 bytes. The wait table
selects an empty body for mask zero and emits `sass_dependency_wait(mask)`
before the predication guard otherwise. This keeps ordinary zero-mask
instructions' statement bodies unchanged. Numerical-definition sharing is
unchanged. Predicated-off wait kernels pass both Ghidra modes on SM89/SM90
under round-robin, reverse, and seeded schedules.

An isolated SM90 exact size diagnostic omitted individual classes and pruned
unreachable tables while retaining the baseline declarations (2026-11-06).
Normalizing the diagnostic source filename reproduced the production baseline
byte for byte. Among the eight classes ranked highest by unique source-table
bytes, omitting `hmma_x8_` removed 213,971 compiled bytes and
`atomg_int__RaNonRZ` removed 194,384. The six sampled LOP3/ULOP3 forms removed
155,812–174,101 bytes each. These are marginal omission costs, not additive
class footprints or proposed support removals. Repeated Boolean calculations
were therefore a measured target for selective sharing; sharing had not yet
been applied at that measurement. The script and results are in
`.cache/ordered-gate/class-size-attribution.py` and
`.cache/ordered-gate/class-size-attribution/results.json`.

Direct ordered BAR and B2R bodies, plus explicit deferred waits after predication,
compile in all twenty languages with zero dead temporaries (2026-11-06).
The largest remains SM90 exact at 16,311,101 bytes, 60,214 bytes above the
dependency-prefix build and 466,115 bytes below the supported limit. SM90
readable is 15,110,002 bytes; SM89 exact/readable are 14,475,932/13,220,951.
One shared deferred-wait subtable serves all ordered DECOUPLED instructions.
Both Ghidra modes on SM89/SM90 pass the production reduction/state fixtures
under round-robin, reverse, and seeded schedules. Numerical-definition sharing
is unchanged. Build and execution logs are under `.cache/ordered-gate/bar-ordered-*`.

Selective LOP3/ULOP3 sharing (2026-11-06) lowers SM90 exact from
16,311,101 to 15,917,261 decompressed bytes: 393,840 bytes saved and 859,955
bytes below the supported limit. SM90 readable is 14,633,531 bytes; SM89
exact/readable are 14,120,561/12,779,292. All twenty languages compile with
zero dead temporaries. The numerical-definition size workaround remains.

Truth tables requiring more than three bitwise operations use shared pure
definitions with caller-supplied captured arguments. The ordered merger
separates bodies with different captured argument requirements before merging
their expressions. This keeps simple LUTs inline: XOR still has two XORs and
one result copy. Arithmetic, loads, stores and branches remain ordinary p-code.
Inspection of 1,792 forms (all 256 LUTs in seven SM90 classes, with predicate
output discarded) found 476 unchanged operation counts; shared LUTs gain
between two and ten copies. The decompiler removes the temporary transfers
and shows the majority expression without synthetic argument registers.

Two alternatives were measured and discarded. Direct-register argument
subtables compiled to 17,083,496 bytes, exceeding the limit. Forwarding
registers into caller-supplied arguments reduced some copies but grew
the language to 16,652,169 bytes. Both lost table sharing between operand
forms. The retained implementation preserves that sharing. Artifacts and
comparisons are under `.cache/ordered-gate/lop3-*`.

Direct normal WARPSYNC bodies (2026-11-06) compile in all twenty languages
with zero dead temporaries. SM90 exact remains largest at 15,918,723
decompressed bytes: 1,462 bytes above the preceding LOP3 sharing build and
858,493 bytes below the unchanged limit. SM90 readable is 14,635,044 bytes;
SM89 exact/readable are 14,122,393/12,781,121. Arrival, wait and release are
separate user operations; mask reads and predicate control flow remain ordinary
p-code. Both isolated FP-mode gates retain the p-code position across a real
coordinator wait and execute the preceding register increment once under all
three schedules. Their WARPSYNC body contains three CALLOTHER operations.
Build and gate logs are `.cache/ordered-gate/warpsync-language-build.log` and
`warpsync-retained-ghidra.log`.

Direct BSSY/BSYNC migration (2026-11-06) compiled all twenty languages with
zero dead temporaries. SM90 exact remained largest at 15,921,735 bytes, adding
3,012 bytes to the WARPSYNC build. The isolated convergence gate contains four
CALLOTHER operations and retains the execution position across a real coordinator
wait without repeating its preceding register write. Production cooperative
checks passed 164 kernels across SM89/SM90, both modes and three schedules.
Build and verification logs are `.cache/ordered-gate/convergence-*`.

Direct BREAK/BMOV environment bodies subsequently compiled all twenty languages
with zero dead temporaries. SM90 exact is 15,925,164 bytes, adding 3,429 bytes
and leaving 852,052 bytes below the unchanged limit. Its readable language is
14,641,447 bytes; SM89 exact/readable are 14,130,388/12,789,093. Environment state
reads and writes are typed operations; constant-bank source loads remain
ordinary p-code. Numerical-definition sharing remains unchanged. The build log
is `.cache/ordered-gate/convergence-environment-languages.log`.

Explicit barrier eligibility before DECOUPLED collective capture (2026-11-06)
adds 1,982 bytes to SM90 exact: 15,927,146 bytes with 850,070 bytes of headroom.
SM90 readable is 14,643,431 bytes. All twenty languages compile with zero dead
temporaries. The query is a typed user operation that reads coordinator state;
it neither executes peer instructions nor consumes their waits. Production
Ghidra checks pass the expanded convergence and partial-deferred collective
fixtures in exact/readable modes on SM89/SM90 under all three schedules.
Logs are `.cache/ordered-gate/partial-deferred-languages.log` and
`partial-deferred-ghidra.log`.
