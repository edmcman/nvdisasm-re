# Additional opcodes in #117, 2026-11-04

The request remains incomplete. The corrected machine descriptions define 267,359 non-alias selector forms for these twelve opcodes across ten architectures. The inventory records each class's legal combination table, operand spans, semantics and evidence separately. Register values and scheduling fields are additional domains; these counts do not establish exhaustive input or scheduling coverage.

Hardware evidence here is SM89. Other architectures have code generation and independent reference cases for implemented forms. Compiler evidence applies only to successfully compiled probes on their recorded targets; rejected probes remain gaps. Neither compiler traces nor raw discovery captures earn functional verification credit.

Supported counts include native semantics and runtime primitives. Reference/GPU counts below require a complete recorded selector form with positive verification evidence. Unplanned counts measure every supported full form beyond the planner's interaction coverage. Sparse and indexed HMMA, packed LDSM, descriptor variants and branch-depth forms remain visible in the inventory even when unsupported.

Partial counts require operand restrictions and remain included in Unresolved. Reference and GPU columns show complete/partial form counts separately. Dense HMMA cases exercise accumulator predicate values and inversion as well as source negation.

## ATOMG

| Architecture | Legal | Supported | Partial | Unresolved | Reference | GPU | Unplanned |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| SM75 | 27,936 | 25,344 | 0 | 2,592 | 1,643/0 | 0/0 | 23,701 |
| SM80 | 16,812 | 11,520 | 0 | 5,292 | 1,934/0 | 0/0 | 9,586 |
| SM86 | 16,812 | 11,520 | 0 | 5,292 | 1,934/0 | 0/0 | 9,586 |
| SM89 | 16,812 | 11,520 | 0 | 5,292 | 1,934/0 | 1,092/0 | 9,586 |
| SM90 | 20,640 | 8,928 | 0 | 11,712 | 1,813/0 | 0/0 | 7,115 |
| SM100 | 20,640 | 8,928 | 0 | 11,712 | 1,813/0 | 0/0 | 7,115 |
| SM101 | 20,640 | 8,928 | 0 | 11,712 | 1,813/0 | 0/0 | 7,115 |
| SM103 | 20,640 | 8,928 | 0 | 11,712 | 1,813/0 | 0/0 | 7,115 |
| SM107 | 20,640 | 8,928 | 0 | 11,712 | 1,813/0 | 0/0 | 7,115 |
| SM120 | 20,640 | 8,928 | 0 | 11,712 | 1,813/0 | 0/0 | 7,115 |

## B2R

| Architecture | Legal | Supported | Partial | Unresolved | Reference | GPU | Unplanned |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| SM75 | 3 | 1 | 0 | 2 | 1/0 | 0/0 | 0 |
| SM80 | 3 | 1 | 0 | 2 | 1/0 | 0/0 | 0 |
| SM86 | 3 | 1 | 0 | 2 | 1/0 | 0/0 | 0 |
| SM89 | 3 | 1 | 0 | 2 | 1/0 | 1/0 | 0 |
| SM90 | 3 | 1 | 0 | 2 | 1/0 | 0/0 | 0 |
| SM100 | 3 | 1 | 0 | 2 | 1/0 | 0/0 | 0 |
| SM101 | 3 | 1 | 0 | 2 | 1/0 | 0/0 | 0 |
| SM103 | 3 | 1 | 0 | 2 | 1/0 | 0/0 | 0 |
| SM107 | 3 | 1 | 0 | 2 | 1/0 | 0/0 | 0 |
| SM120 | 3 | 1 | 0 | 2 | 1/0 | 0/0 | 0 |

## BRX

| Architecture | Legal | Supported | Partial | Unresolved | Reference | GPU | Unplanned |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| SM75 | 6 | 2 | 0 | 4 | 2/0 | 0/0 | 0 |
| SM80 | 6 | 2 | 0 | 4 | 2/0 | 0/0 | 0 |
| SM86 | 6 | 2 | 0 | 4 | 2/0 | 0/0 | 0 |
| SM89 | 6 | 2 | 0 | 4 | 2/0 | 0/0 | 0 |
| SM90 | 6 | 2 | 0 | 4 | 2/0 | 0/0 | 0 |
| SM100 | 6 | 2 | 0 | 4 | 2/0 | 0/0 | 0 |
| SM101 | 6 | 2 | 0 | 4 | 2/0 | 0/0 | 0 |
| SM103 | 6 | 2 | 0 | 4 | 2/0 | 0/0 | 0 |
| SM107 | 6 | 2 | 0 | 4 | 2/0 | 0/0 | 0 |
| SM120 | 6 | 2 | 0 | 4 | 2/0 | 0/0 | 0 |

## CCTL

| Architecture | Legal | Supported | Partial | Unresolved | Reference | GPU | Unplanned |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| SM75 | 96 | 0 | 0 | 96 | 0/0 | 0/0 | 0 |
| SM80 | 144 | 0 | 0 | 144 | 0/0 | 0/0 | 0 |
| SM86 | 144 | 0 | 0 | 144 | 0/0 | 0/0 | 0 |
| SM89 | 160 | 0 | 0 | 160 | 0/0 | 0/0 | 0 |
| SM90 | 144 | 0 | 0 | 144 | 0/0 | 0/0 | 0 |
| SM100 | 172 | 0 | 0 | 172 | 0/0 | 0/0 | 0 |
| SM101 | 172 | 0 | 0 | 172 | 0/0 | 0/0 | 0 |
| SM103 | 172 | 0 | 0 | 172 | 0/0 | 0/0 | 0 |
| SM107 | 172 | 0 | 0 | 172 | 0/0 | 0/0 | 0 |
| SM120 | 172 | 0 | 0 | 172 | 0/0 | 0/0 | 0 |

## DEPBAR

| Architecture | Legal | Supported | Partial | Unresolved | Reference | GPU | Unplanned |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| SM75 | 2 | 2 | 0 | 0 | 2/0 | 0/0 | 0 |
| SM80 | 2 | 2 | 0 | 0 | 2/0 | 0/0 | 0 |
| SM86 | 2 | 2 | 0 | 0 | 2/0 | 0/0 | 0 |
| SM89 | 2 | 2 | 0 | 0 | 2/0 | 0/0 | 0 |
| SM90 | 3 | 3 | 0 | 0 | 3/0 | 0/0 | 0 |
| SM100 | 3 | 3 | 0 | 0 | 3/0 | 0/0 | 0 |
| SM101 | 3 | 3 | 0 | 0 | 3/0 | 0/0 | 0 |
| SM103 | 3 | 3 | 0 | 0 | 3/0 | 0/0 | 0 |
| SM107 | 3 | 3 | 0 | 0 | 3/0 | 0/0 | 0 |
| SM120 | 3 | 3 | 0 | 0 | 3/0 | 0/0 | 0 |

## ERRBAR

| Architecture | Legal | Supported | Partial | Unresolved | Reference | GPU | Unplanned |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| SM75 | 1 | 0 | 0 | 1 | 0/0 | 0/0 | 0 |
| SM80 | 1 | 0 | 0 | 1 | 0/0 | 0/0 | 0 |
| SM86 | 1 | 0 | 0 | 1 | 0/0 | 0/0 | 0 |
| SM89 | 1 | 0 | 0 | 1 | 0/0 | 0/0 | 0 |
| SM90 | 1 | 0 | 0 | 1 | 0/0 | 0/0 | 0 |
| SM100 | 1 | 0 | 0 | 1 | 0/0 | 0/0 | 0 |
| SM101 | 1 | 0 | 0 | 1 | 0/0 | 0/0 | 0 |
| SM103 | 1 | 0 | 0 | 1 | 0/0 | 0/0 | 0 |
| SM107 | 1 | 0 | 0 | 1 | 0/0 | 0/0 | 0 |
| SM120 | 1 | 0 | 0 | 1 | 0/0 | 0/0 | 0 |

## HMMA

| Architecture | Legal | Supported | Partial | Unresolved | Reference | GPU | Unplanned |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| SM75 | 384 | 0 | 0 | 384 | 0/0 | 0/0 | 0 |
| SM80 | 192 | 20 | 0 | 172 | 20/0 | 0/0 | 0 |
| SM86 | 480 | 40 | 0 | 440 | 40/0 | 0/0 | 0 |
| SM89 | 480 | 40 | 0 | 440 | 40/0 | 40/0 | 0 |
| SM90 | 480 | 40 | 0 | 440 | 40/0 | 0/0 | 0 |
| SM100 | 384 | 40 | 0 | 344 | 40/0 | 0/0 | 0 |
| SM101 | 384 | 40 | 0 | 344 | 40/0 | 0/0 | 0 |
| SM103 | 384 | 40 | 0 | 344 | 40/0 | 0/0 | 0 |
| SM107 | 384 | 40 | 0 | 344 | 40/0 | 0/0 | 0 |
| SM120 | 384 | 40 | 0 | 344 | 40/0 | 0/0 | 0 |

## LDGDEPBAR

| Architecture | Legal | Supported | Partial | Unresolved | Reference | GPU | Unplanned |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| SM75 | 0 | 0 | 0 | 0 | 0/0 | 0/0 | 0 |
| SM80 | 1 | 1 | 0 | 0 | 1/0 | 0/0 | 0 |
| SM86 | 1 | 1 | 0 | 0 | 1/0 | 0/0 | 0 |
| SM89 | 1 | 1 | 0 | 0 | 1/0 | 0/0 | 0 |
| SM90 | 1 | 1 | 0 | 0 | 1/0 | 0/0 | 0 |
| SM100 | 1 | 1 | 0 | 0 | 1/0 | 0/0 | 0 |
| SM101 | 1 | 1 | 0 | 0 | 1/0 | 0/0 | 0 |
| SM103 | 1 | 1 | 0 | 0 | 1/0 | 0/0 | 0 |
| SM107 | 1 | 1 | 0 | 0 | 1/0 | 0/0 | 0 |
| SM120 | 1 | 1 | 0 | 0 | 1/0 | 0/0 | 0 |

## LDGSTS

| Architecture | Legal | Supported | Partial | Unresolved | Reference | GPU | Unplanned |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| SM75 | 0 | 0 | 0 | 0 | 0/0 | 0/0 | 0 |
| SM80 | 8,064 | 8,064 | 0 | 0 | 409/0 | 0/0 | 7,655 |
| SM86 | 8,064 | 8,064 | 0 | 0 | 409/0 | 0/0 | 7,655 |
| SM89 | 8,064 | 8,064 | 0 | 0 | 409/0 | 409/0 | 7,655 |
| SM90 | 5,760 | 5,760 | 0 | 0 | 405/0 | 0/0 | 5,355 |
| SM100 | 5,760 | 5,760 | 0 | 0 | 405/0 | 0/0 | 5,355 |
| SM101 | 5,760 | 5,760 | 0 | 0 | 405/0 | 0/0 | 5,355 |
| SM103 | 5,760 | 5,760 | 0 | 0 | 405/0 | 0/0 | 5,355 |
| SM107 | 5,760 | 5,760 | 0 | 0 | 405/0 | 0/0 | 5,355 |
| SM120 | 5,760 | 5,760 | 0 | 0 | 405/0 | 0/0 | 5,355 |

## LDSM

| Architecture | Legal | Supported | Partial | Unresolved | Reference | GPU | Unplanned |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| SM75 | 72 | 24 | 0 | 48 | 12/0 | 0/0 | 12 |
| SM80 | 36 | 12 | 0 | 24 | 12/0 | 0/0 | 0 |
| SM86 | 36 | 12 | 0 | 24 | 12/0 | 0/0 | 0 |
| SM89 | 36 | 12 | 0 | 24 | 12/0 | 12/0 | 0 |
| SM90 | 36 | 12 | 0 | 24 | 12/0 | 0/0 | 0 |
| SM100 | 60 | 12 | 0 | 48 | 12/0 | 0/0 | 0 |
| SM101 | 60 | 12 | 0 | 48 | 12/0 | 0/0 | 0 |
| SM103 | 60 | 12 | 0 | 48 | 12/0 | 0/0 | 0 |
| SM107 | 60 | 12 | 0 | 48 | 12/0 | 0/0 | 0 |
| SM120 | 60 | 12 | 0 | 48 | 12/0 | 0/0 | 0 |

## MEMBAR

| Architecture | Legal | Supported | Partial | Unresolved | Reference | GPU | Unplanned |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| SM75 | 30 | 12 | 0 | 18 | 12/0 | 0/0 | 0 |
| SM80 | 20 | 12 | 0 | 8 | 12/0 | 0/0 | 0 |
| SM86 | 20 | 12 | 0 | 8 | 12/0 | 0/0 | 0 |
| SM89 | 20 | 12 | 0 | 8 | 12/0 | 0/0 | 0 |
| SM90 | 25 | 12 | 0 | 13 | 12/0 | 0/0 | 0 |
| SM100 | 25 | 12 | 0 | 13 | 12/0 | 0/0 | 0 |
| SM101 | 25 | 12 | 0 | 13 | 12/0 | 0/0 | 0 |
| SM103 | 25 | 12 | 0 | 13 | 12/0 | 0/0 | 0 |
| SM107 | 25 | 12 | 0 | 13 | 12/0 | 0/0 | 0 |
| SM120 | 25 | 12 | 0 | 13 | 12/0 | 0/0 | 0 |

## YIELD

| Architecture | Legal | Supported | Partial | Unresolved | Reference | GPU | Unplanned |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| SM75 | 2 | 0 | 0 | 2 | 0/0 | 0/0 | 0 |
| SM80 | 2 | 0 | 0 | 2 | 0/0 | 0/0 | 0 |
| SM86 | 2 | 0 | 0 | 2 | 0/0 | 0/0 | 0 |
| SM89 | 2 | 0 | 0 | 2 | 0/0 | 0/0 | 0 |
| SM90 | 2 | 0 | 0 | 2 | 0/0 | 0/0 | 0 |
| SM100 | 2 | 0 | 0 | 2 | 0/0 | 0/0 | 0 |
| SM101 | 2 | 0 | 0 | 2 | 0/0 | 0/0 | 0 |
| SM103 | 2 | 0 | 0 | 2 | 0/0 | 0/0 | 0 |
| SM107 | 2 | 0 | 0 | 2 | 0/0 | 0/0 | 0 |
| SM120 | 2 | 0 | 0 | 2 | 0/0 | 0/0 | 0 |

## Reproduction

Run from the repository root; builds and GPU validation require device/JDK access outside the sandbox.

```bash
nice uv run sass-build-languages --coverage
nice uv run python -m tests.autocases SM107 --check
nice uv run pytest tests/test_lifted_semantics.py tests/test_family_lowering.py --family atomic --family asynchronous --family matrix --family control --arch all --fp all
nice uv run pytest tests/test_pcode_semantics.py tests/test_fixtures_emulated.py --family atomic --family asynchronous --family matrix --family control -m ghidra --arch all --fp all
nice uv run pytest tests/test_cases_on_gpu.py tests/test_refs_on_gpu.py --family atomic --family asynchronous --family matrix --family control -m gpu --require-gpu
nice uv run sass-refresh-coverage
nice uv run python -m research.additional_opcodes --out research/additional-opcodes/inventory-current.json --report docs/additional-opcodes-2026-11-04.md
```

Use `--expand` on the inventory command to materialize every legal form rather than its compact combination table. Per-architecture planner gaps are retained as `research/additional-opcodes/plan-gaps-SMxx.json`.

Numerical and cooperative findings, including unfinished behavior, are documented in [hardware-facts.md](hardware-facts.md), [emulation.md](emulation.md) and [open.md](open.md). Compiler identities and hashes are pinned in the inventory. PTX probes are retained under `tests/ptx/`.

Architecture-specific compiler traces and transformed source files are retained in `research/additional-opcodes/compiler-probes.json` and `compiler-sources/`. Reproduce with `nice uv run python -m research.compiler_additional_opcodes --out research/additional-opcodes/compiler-probes.json --sources research/additional-opcodes/compiler-sources`. This distinguishes emitted encodings from compiler/version rejections; numerical behavior remains separately evidenced.

The second trace set, `compiler-probes-13.4.json` and `compiler-sources-13.4/`, uses ptxas 13.4.92 with PTX 9.4. All ten probes compile on nine modeled architectures; SM75 compiles four. The SM101 model explicitly maps to compiler target `sm_110`, following the PTX target rename. Package and executable hashes are retained in `compiler-13.4-identity.json`. With that executable selected through `PTXAS`, reproduce using `nice uv run python -m research.compiler_additional_opcodes --ptx-version 9.4 --renamed-targets --out research/additional-opcodes/compiler-probes-13.4.json --sources research/additional-opcodes/compiler-sources-13.4`. Target mappings and compiler emission provide no hardware verification credit.

## Replay and PR checks

The retained YOLO replay compares every captured memory region: 249 launches pass, 65 are blocked, 0 differ, and 0 have no memory region to compare. Passing launches compare 470 complete memory regions. Target instructions executed by passing launches: DEPBAR in 48 launches, HMMA in 48 launches, LDGDEPBAR in 48 launches, LDGSTS in 48 launches, LDSM in 48 launches. Executed counts in each launch record count distinct visited instruction locations, rather than dynamic invocations. See `research/additional-opcodes/yolo-replay.json`.

PR #119's failing logs identified a stale SM107 case plan and launch tests that still expected Ghidra to reject launch scope. The regenerated plan passes `--check`; the updated launch test compares against the reference and passes locally on SM89/SM90. After push `12ee374`, CI exposed invalid size selectors admitted by span constructors and excess P-code for discarded integer predicates. Span constraints now encode their narrowed raw domains, and predicate-sharing costs account for narrowing surviving expressions. Scalar half FMA sharing keeps all twenty languages below the unchanged 16 MiB limit. All-architecture decoder/dataflow and int_alu/fp16 numerical checks passed 2,794 tests with 209,716 selected shared cases; the twenty reviewed operation-count baselines pass separately. Carry, threads and memory checks on SM89/SM107 in both modes passed 614 tests with 23,406 shared cases. That refresh left the committed coverage/evidence files unchanged. Following `d715824`, all 58 current PR checks pass and nine are skipped. SM90's earlier generation job failed artifact upload with ECONNRESET after its generation checks passed.

The dense TF32 and operand-control extension passes 4,612 SM89 shared GPU cases and nine whole-warp GPU fixtures. All-architecture exact/readable matrix Ghidra validation passes 94 tests with 78,216 shared cases; FP16 numerical/dataflow validation passes 916 tests with 155,492 shared cases. Corrected arithmetic passes 72 Python/native tests, including all eight source-negation/predicate-inversion combinations in the native engine. Integrated source checks pass 54 tests, and all twenty reviewed P-code baselines pass. All twenty languages compile below the unchanged 16 MiB limit; SM107 exact is 16,753,273 bytes. Full-form HMMA planning is exhaustive; input profiles remain sampled regression domains. F16 destinations, sparse/indexed HMMA and other unresolved target forms still prevent completion.

The BF16 extension passes 5,892 SM89 shared GPU cases and twelve whole-warp GPU fixtures. Final all-architecture exact/readable Ghidra matrix validation passes 106 tests with 99,880 shared cases in 408 seconds. The expanded source-overlap checks pass 96 Python/native tests; cooperative fixtures separately pass all six SM89/SM90 schedule combinations. Integrated source checks pass 575 tests, including real numeric-definition parameter expansion. All twenty reviewed P-code baselines pass. The shared packed-format parameter increases F16's P-code, while keeping every language below the unchanged 16 MiB limit (SM107 exact: 16,776,587 bytes). SM89 has 32 supported dense selector forms with exhaustive planning; F16 destinations, sparse/indexed forms and other target gaps still prevent completion.

The packed F16-output extension passes 7,108 SM89 shared GPU cases across all forty supported dense selector forms and fifteen whole-warp fixtures. Ghidra exact/readable validation on all ten architectures checks 120,456 shared cases; all 140 numerical, fixture and dataflow tests pass. Eighteen HMMA-only operation-count changes were reviewed, and the twenty updated baseline tests pass. Integrated source validation passes 741 tests, including independent references, real definition parameter expansion and cooperative schedules. Final native arithmetic passes 42 tests. All twenty languages compile below the unchanged 16 MiB limit (SM107 exact: 16,776,580 bytes). The F16 model rounds the aligned sum directly to half after each tensor stage; a distinguishing GPU boundary rejects an intermediate F32 rounding. Other architectures' F16 staging remains inferred, and legacy/other shapes, sparse/indexed forms and the other opcode gaps remain unfinished.
