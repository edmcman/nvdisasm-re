---
name: sass-opcode-coverage
description: Drive one SASS opcode (e.g. FADD) in nvdisasm-re toward native semantics and verification evidence for every legal modifier combination, on every architecture. Measures the opcode's semantics and evidence gaps, classifies each, closes them through references, families, autocase adapters and GPU runs, and reports before/after coverage. With no opcode given, ranks opcodes and picks a promising one. Use for "improve coverage/evidence of OPCODE", "support all modifier combinations", or "pick an opcode to cover".
---

# SASS opcode coverage

Goal: every legal modifier combination of one opcode is **native** (not primitive,
opaque or partial) on every architecture, and **verified**: GPU evidence on SM89,
reference evidence elsewhere. Anything short of that ends with a specific, recorded
reason per remaining gap.

Work in the user's `nvdisasm-re` checkout. `AGENTS.md` "Writing new semantics" is the
authoritative implementation, test, build and hardware order; follow it and don't
repeat it here. When a form's behavior is unknown rather than merely untested, use
the `sass-opcode-semantics` skill to establish it (ptxas, GPU probes) before
implementing it.

## How coverage is counted

Read this before choosing work, because the counting rules decide what earns credit.

- A class's combinations are the cross product of its modifier labels, minus illegal
  ones. The manifest (`processor/SASS/data/languages/sass_smXX_coverage.json`, generated)
  stores the `decides` modifiers' table with a status per row, multiplied by `free`
  combinations of the rest.
- Evidence counts **exact modifier tuples**. A combination is verified only if a
  passing case ran that full form (`tests/evidence.py`, `sass.sleigh.coverage.combinations`).
  Running each modifier value once credits only those tuples, not their cross product.
- Reference evidence comes from the *sampled* emulator cases (`exhaustive=False`). A form
  reachable only under `--exhaustive` earns nothing.
- SM89 GPU evidence comes from `tests/baseline/gpu_case_passes.json`, recorded per
  family. A partial or failing family run drops that family's evidence, so the
  whole family must pass.
- The shared-case plans (`tests/cases/generated/SMxx.json`) hold one row per legal form
  that an adapter can generate. A blocked form simply has no row, and the planner's
  `--report` gives the reason.

## 1. Measure

```sh
S=.agents/skills/sass-opcode-coverage/scripts/gaps.py
uv run python $S                      # rank SM89 opcodes (--arch, --top, --all)
uv run python $S FADD                 # every arch, then SM89 per-class gaps
```

Per opcode, it prints combinations by status and evidence, then for each class the
non-native table rows (with the family's `reason`) and every modifier value never
verified. It reads the committed `coverage.json` and the generated manifests. In a
fresh worktree, pass `--manifest-dir` pointing at the main checkout's
`processor/SASS/data/languages`, or generate them with
`uv run sass-build-languages SM89 --fp exact --generate-only --coverage`. Note the
starting commit: it is the baseline for the step 5 comparison, so don't save
`gaps.py` output to diff by hand.

## 2. Choose the opcode (when none is given)

Rank, then prefer, in order:
1. **Native but unverified.** Modelled near 100% with low verified % (FADD, FMNMX,
   FSETP, DFMA, HSET2 on 2026-10-19). Only references and adapters change, which is
   the cheapest and largest gain.
2. **One selector value blocks many combinations.** For example, FMUL/FFMA `fmz=FMZ` is
   `unmodelled('FMZ')` in `families/floating.py` and makes a third of each class primitive.
3. Small opcodes with a few opaque rows whose behavior is documented.

Avoid, unless asked: fully opaque opcodes, memory (LD/LDG/ST), MMA and approximation
units (MUFU), and anything without an independent reference path. Those are
investigations, not coverage work. State the pick and why, then proceed.

## 3. Classify every gap

For each class, sort each gap into exactly one of these:

**Semantic gap** (a non-native row). Find where the family declines:
`grep -n "unmodelled(\|primitive(" src/sass/semantics/families/*.py`, plus the
manifest `reason`.
- Behavior established (md, PTX ISA, `docs/hardware-facts.md`): write the reference,
  then implement it natively.
- Unknown: probe the GPU first (sass-opcode-semantics, `references/gpu-probes.md`), then
  promote the observation to a reference.
- Genuinely runtime-only: keep it `primitive()` with a specific reason.

**Evidence gap** (native, no verifying form). Find why the planner drops it:

```sh
uv run python -m tests.autocases SM89 --report R.json   # ~3 min; plan unchanged without --write
python3 -c "import json,sys;[print(g) for g in json.load(open('R.json'))['gaps'] if g['cls'].startswith(sys.argv[1])]" fadd__
```

Typical causes:
- an adapter's `blocked_reason`/`Unsupported(...)` in `tests/harness/autocase_*.py`
  (for FP: source-sign modifiers, XORSIGN, FMZ);
- a reference in `tests/refs/` that lacks the modifier;
- a profile with no input that distinguishes the modifier;
- operand setup that cannot preserve the form.

Fix the reference first, then lift the block. Add profile values that separate the
plausible rules: for sign modifiers, signed zeros, NaN and ±Inf through abs/neg; for
FMZ, 0×Inf, 0×NaN and subnormal operands; for SAT, values just outside [0,1].
Remove manual cases the generator now covers, or justify the ones you keep with
`@retained(reason)`.

**Unreachable**: the harness can't represent the form (model-only state, illegal on
hardware). Leave an explicit adapter gap reason or `hardware={"skip": reason}`. Never
fake evidence.

## 4. Close the loop

Iterate cheapest-first, as AGENTS.md orders:

1. `uv run pytest tests/test_refs.py`. The references stay independent of `src/sass`.
2. `uv run python -m tests.autocases SM89 --write`, then
   `uv run pytest -k "lifted or family_lowering" --family F` and later `--arch all --fp all`.
3. Languages and Ghidra, outside the sandbox: `uv run sass-build-languages SM89 --fp exact`,
   then `uv run pytest -m ghidra --family F --arch SM89`. Review op-count baseline
   changes before `--update-baseline`.
4. GPU, outside the sandbox: `uv run pytest -m gpu --require-gpu --family F`. This must
   pass for the whole family to record evidence. A reference that disagrees with the
   GPU is wrong; never loosen `same`/`excused` to make it pass.
5. Regenerate every plan the change affects:
   `for a in SM75 SM80 SM86 SM89 SM90 SM100 SM101 SM103 SM107 SM120; do uv run python -m tests.autocases $a --write & done; wait`
   (minutes each, one core per architecture, so run them in parallel and in the background). Then run the full-architecture Python and
   Ghidra tests.
6. `uv run sass-refresh-coverage` (~7 min, background), then rerun `gaps.py OP` for
   the remaining gaps.

CI reruns the per-architecture Python tests and fails on stale coverage. It is a
backstop and does not replace these local runs: it doesn't refresh anything, and it
has no Ghidra or GPU.

Growth check: GPU and Ghidra time scale with forms × profile vectors. Use the
default sampled run; reach for `--exhaustive` only for sweeps.

## 5. Record and report

Update the documentation that the change affects:
- `tests/README.md`: the shared-case paragraphs that list adapter gaps. For FP this
  includes the "source modifiers, FMZ and XORSIGN remain explicit gaps" lines.
- `docs/semantics.md`: the semantic scope.
- `docs/hardware-facts.md`: anything the GPU established, dated.

Compare with the starting commit using the same renderer as the PR coverage comment:
`python3 .github/scripts/coverage_comment.py local REV`. It prints per-architecture
and per-opcode status and evidence changes against the working tree's
`coverage.json`, and calls out regressions such as a family's dropped GPU evidence.
Every regression it flags needs an explanation or a fix.

Report:
- the opcode and why it was chosen;
- that comparison, trimmed to the target opcode plus any regression;
- what changed (references, families, adapters, profiles);
- each remaining gap, labelled semantic, evidence or unreachable, with its reason;
- test runs and their scope, labelling any failures as semantics or testing/environment;
- the commands to reproduce.

Claim "all combinations" only when `gaps.py` shows 100% modelled and 100% verified.
