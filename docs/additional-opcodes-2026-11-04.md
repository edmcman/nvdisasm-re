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
| SM80 | 192 | 32 | 0 | 160 | 32/0 | 0/0 | 0 |
| SM86 | 480 | 64 | 0 | 416 | 64/0 | 0/0 | 0 |
| SM89 | 480 | 64 | 0 | 416 | 64/0 | 64/0 | 0 |
| SM90 | 480 | 64 | 0 | 416 | 64/0 | 0/0 | 0 |
| SM100 | 384 | 64 | 0 | 320 | 64/0 | 0/0 | 0 |
| SM101 | 384 | 64 | 0 | 320 | 64/0 | 0/0 | 0 |
| SM103 | 384 | 64 | 0 | 320 | 64/0 | 0/0 | 0 |
| SM107 | 384 | 64 | 0 | 320 | 64/0 | 0/0 | 0 |
| SM120 | 384 | 64 | 0 | 320 | 64/0 | 0/0 | 0 |

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

The second trace set, `compiler-probes-13.4.json` and `compiler-sources-13.4/`, uses ptxas 13.4.92 with PTX 9.4. All thirteen probes compile on nine modeled architectures; SM75 compiles six. The SM101 model explicitly maps to compiler target `sm_110`, following the PTX target rename. Package and executable hashes are retained in `compiler-13.4-identity.json`. With that executable selected through `PTXAS`, reproduce using `nice uv run python -m research.compiler_additional_opcodes --ptx-version 9.4 --renamed-targets --out research/additional-opcodes/compiler-probes-13.4.json --sources research/additional-opcodes/compiler-sources-13.4`. Target mappings and compiler emission provide no hardware verification credit.

## Replay and PR checks

The retained YOLO replay compares every captured memory region: 249 launches pass, 65 are blocked, 0 differ, and 0 have no memory region to compare. Passing launches compare 470 complete memory regions. Target instructions executed by passing launches: DEPBAR in 48 launches, HMMA in 48 launches, LDGDEPBAR in 48 launches, LDGSTS in 48 launches, LDSM in 48 launches. Executed counts in each launch record count distinct visited instruction locations, rather than dynamic invocations. See `research/additional-opcodes/yolo-replay.json`.

PR #119's failures came from a stale SM107 case plan, launch tests still expecting Ghidra to reject launch scope, invalid size selectors admitted by span constructors, and excess P-code for discarded integer predicates. The plan is regenerated; launch tests compare against the reference on SM89/SM90; span constructors enforce their narrowed domains; predicate-sharing costs account for surviving expressions. All 58 PR checks pass on pushed commit `dfc316d`, with nine skipped. That result does not establish CI status for subsequent commits.

## Current matrix validation

The packed `1688` extension adds F16/F32, BF16/F32 and F16/F16. SM89 hardware regression passes 9,964 shared cases across all sixty-four supported dense selector forms, plus twenty-four whole-warp fixtures. Raw discovery observations are retained separately and earn no verification credit. All-architecture exact/readable Ghidra validation passes numerical and dataflow checks for matrices and conversions: 237,550 shared cases and 2,719 passing tests, with nineteen reviewed operation-count baseline changes. The updated twenty baseline tests pass. Focused source checks pass 196 tests, structural checks pass 571 tests, saved plans pass ten tests, and native matrix checks pass 66 tests. Real parameter expansion passes for 1,072 discovered definition signatures. Every F16 accumulator encoding agrees with the independent conversion reference. Shared stages and conversions keep all twenty languages below 16 MiB (SM107 exact: 16,775,109 bytes), at the cost of 3.5–6.5% more operations in the sampled HMMA baseline. Full-form HMMA planning is exhaustive; input profiles remain sampled regression domains. F16 output rounds the aligned sum directly to half after each tensor stage; a distinguishing SM89 boundary rejects intermediate F32 rounding. SM75 compiler emission establishes the two F16 `1688` encodings, but its numerical behavior remains unresolved. Other architectures' numerical staging remains inferred; sparse/indexed forms and other target gaps remain unfinished.
