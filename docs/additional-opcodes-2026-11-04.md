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
| SM80 | 192 | 12 | 0 | 180 | 12/0 | 0/0 | 0 |
| SM86 | 480 | 24 | 0 | 456 | 24/0 | 0/0 | 0 |
| SM89 | 480 | 24 | 0 | 456 | 24/0 | 24/0 | 0 |
| SM90 | 480 | 24 | 0 | 456 | 24/0 | 0/0 | 0 |
| SM100 | 384 | 24 | 0 | 360 | 24/0 | 0/0 | 0 |
| SM101 | 384 | 24 | 0 | 360 | 24/0 | 0/0 | 0 |
| SM103 | 384 | 24 | 0 | 360 | 24/0 | 0/0 | 0 |
| SM107 | 384 | 24 | 0 | 360 | 24/0 | 0/0 | 0 |
| SM120 | 384 | 24 | 0 | 360 | 24/0 | 0/0 | 0 |

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

## Replay and PR checks

The retained YOLO replay compares every captured memory region: 249 launches pass, 65 are blocked, 0 differ, and 0 have no memory region to compare. Passing launches compare 470 complete memory regions. Target instructions executed by passing launches: DEPBAR in 48 launches, HMMA in 48 launches, LDGDEPBAR in 48 launches, LDGSTS in 48 launches, LDSM in 48 launches. Executed counts in each launch record count distinct visited instruction locations, rather than dynamic invocations. See `research/additional-opcodes/yolo-replay.json`.

PR #119's failing logs identified a stale SM107 case plan and launch tests that still expected Ghidra to reject launch scope. The regenerated plan passes `--check`; the updated launch test compares against the reference and passes locally on SM89/SM90. After push `12ee374`, CI exposed invalid size selectors admitted by span constructors and excess P-code for discarded integer predicates. Span constraints now encode their narrowed raw domains, and predicate-sharing costs account for narrowing surviving expressions. Scalar half FMA sharing keeps all twenty languages below the unchanged 16 MiB limit. All-architecture decoder/dataflow and int_alu/fp16 numerical checks passed 2,794 tests with 209,716 selected shared cases; the twenty reviewed operation-count baselines pass separately. Carry, threads and memory checks on SM89/SM107 in both modes passed 614 tests with 23,406 shared cases. That refresh left the committed coverage/evidence files unchanged. Following `d715824`, all 58 current PR checks pass and nine are skipped. SM90's earlier generation job failed artifact upload with ECONNRESET after its generation checks passed.

The dense TF32 and operand-control extension passes 4,612 SM89 shared GPU cases and nine whole-warp GPU fixtures. All-architecture exact/readable matrix Ghidra validation passes 94 tests with 78,216 shared cases; FP16 numerical/dataflow validation passes 916 tests with 155,492 shared cases. Corrected arithmetic passes 72 Python/native tests, including all eight source-negation/predicate-inversion combinations in the native engine. Integrated source checks pass 54 tests, and all twenty reviewed P-code baselines pass. All twenty languages compile below the unchanged 16 MiB limit; SM107 exact is 16,753,273 bytes. Full-form HMMA planning is exhaustive; input profiles remain sampled regression domains. BF16, F16 destinations, sparse/indexed HMMA and other unresolved target forms still prevent completion.
