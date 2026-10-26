---
name: sass-opcode-coverage
description: Drive one SASS opcode (e.g. FADD) in nvdisasm-re toward native semantics and verification evidence for every legal modifier combination, on every architecture. Measures the opcode's semantics and evidence gaps, classifies each, closes them through references, families, autocase adapters and GPU runs, and reports before/after coverage. With no opcode given, ranks opcodes and picks a promising one. Use for "improve coverage/evidence of OPCODE", "support all modifier combinations", or "pick an opcode to cover". To establish unknown behavior (ptxas traces, GPU discovery probes), use sass-opcode-semantics instead.
---

# SASS opcode coverage

Goal: every legal modifier combination of one opcode is **native** (not primitive,
opaque or partial) on every architecture, and **verified**: GPU evidence on SM89 (and SM90
where an SM90 run passed), reference evidence elsewhere. Anything short of that ends with a specific, recorded
reason per remaining gap.

Work in the user's `nvdisasm-re` checkout. `AGENTS.md` "Writing new semantics" is the
authoritative implementation, test, build and hardware order, and its "Rules" apply
(`nice` for builds, test suites, plan regeneration and coverage refreshes; language
builds and GPU runs outside the sandbox). Follow it; this skill adds only what is
specific to coverage. When a form's behavior is unknown rather than merely untested,
use the `sass-opcode-semantics` skill to establish it (ptxas, GPU probes) before
implementing it.

## How coverage is counted

Read this before choosing work, because the counting rules decide what earns credit.

- A class's combinations are the cross product of its modifier labels, minus illegal
  ones. The manifest (`processor/SASS/data/languages/sass_smXX_coverage.json`, generated
  and gitignored) stores the `decides` modifiers' table with a status per row,
  multiplied by `free` combinations of the rest.
- Evidence counts **exact modifier tuples**. A combination is verified only if a
  passing case ran that full form (`tests/evidence.py`, `sass.sleigh.coverage.combinations`).
  Running each modifier value once credits only those tuples, not their cross product.
- Evidence kinds: `reference` (emulator cases checked against `tests/refs/`), `ptxas`
  (annotated `tests/ptx/` probes) and `gpu` (SM89 and SM90, per architecture).
- Reference evidence comes from the default 0% sample (`sass-refresh-coverage` keeps the
  committed artifacts at 0%), which keeps at least one scenario per available form; a
  higher `--sample` adds inputs, not forms. See `tests/README.md` "Sampling".
- GPU evidence (recorded per architecture) comes from `tests/baseline/gpu_observations/` (shared
  cases: what each probe returned, re-judged against the current expectations) and from passing
  whole-kernel fixtures' data paths (`tests/baseline/gpu_passes.json`). A run replaces the
  observations of the scenarios it ran and keeps the rest; a word counts once every current GPU
  scenario for it passes, so new cases need only a run of their family (`--family F`), and a
  changed reference needs none unless its scenarios now disagree (`test_gpu_observations.py`).
- The shared-case plans (`tests/cases/generated/SMxx.json`) hold one row per legal form
  that an adapter can generate. A blocked form simply has no row; the planner's report,
  committed beside each plan as `tests/cases/generated/SMxx.gaps.json`, gives the reason.

## 1. Measure

```sh
S=.agents/skills/sass-opcode-coverage/scripts/gaps.py
uv run python $S                      # rank SM89 opcodes (--arch, --top, --all)
uv run python $S FADD                 # every arch, then SM89 per-class gaps
```

Per opcode, it prints combinations by status and evidence, then for each class the
non-native table rows (with the family's `reason`) and every modifier value never
verified. It reads the committed `coverage.json` and, for the per-class part, the
generated manifests. If they are missing (a fresh checkout or worktree), generate them
with `uv run sass-build-languages SM89 --fp exact --generate-only --coverage` (no Ghidra
needed), or pass `--manifest-dir` pointing at another checkout's
`processor/SASS/data/languages`. Note the starting commit: it is the baseline for the
step 5 comparison, so don't save `gaps.py` output to diff by hand.

## 2. Choose the opcode (when none is given)

Rank, then prefer, in order:
1. **Native but unverified.** Modelled near 100% with low verified % (HSET2, HSETP2,
   HMUL2 on 2026-10-22). Only references and adapters change, which is the cheapest
   and largest gain.
2. **One selector value blocks many combinations.** For example, HADD2/HFMA2 `.FTZ`/`.FMZ`
   are `unmodelled` in the exact lowering (`no_flush` in `families/floating.py`); HFMA2
   was 8% modelled on SM89 on 2026-10-22. Confirm the blocking reason with `gaps.py OP`.
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
- Unknown: probe the GPU first (sass-opcode-semantics,
  `references/gpu-probes.md`), then promote the observation to a reference.
- Genuinely runtime-only: a named primitive (`src/sass/semantics/primitive.py`) with explicit
  arguments, everything around it native.

**Evidence gap** (native, no verifying form). Find why the planner drops it in the
committed report, or regenerate it after changing adapters (about 3 minutes; the plan is
unchanged without `--write`):

```sh
R=tests/cases/generated/SM89.gaps.json
# nice uv run python -m tests.autocases SM89 --report $R
python3 -c "import json,sys;[print(g) for g in json.load(open(sys.argv[1]))['gaps'] if g['cls'].startswith(sys.argv[2])]" $R hmul2__
```

Typical causes:
- an adapter's `blocked_reason`/`autocases.Unsupported(...)` in `tests/harness/autocase*.py`
  (`tests/README.md` "Generated shared cases" lists each adapter's remaining gaps, e.g.
  FMNMX3 source-sign modifiers);
- a reference in `tests/refs/` that lacks the modifier;
- a profile with no input that distinguishes the modifier
  (`tests/harness/autocase_profiles.py`);
- operand setup that cannot preserve the form.

Fix the reference first, then lift the block. Add profile values that separate the
plausible rules: for sign modifiers, signed zeros, NaN and ±Inf through abs/neg; for
FTZ/FMZ, 0×Inf, 0×NaN and subnormal operands; for SAT, values just outside [0,1].
Remove manual cases the generator now covers, or justify the ones you keep with
`@retained(reason)` (`tests/harness/manual.py`).

**Unreachable**: the harness can't represent the form (model-only state, illegal on
hardware). Leave an explicit adapter gap reason or `hardware={"skip": reason}`. Never
fake evidence.

## 4. Close the loop

Follow AGENTS.md "Writing new semantics" steps 1-6, cheapest test first. Coverage work
adds these steps:

- After step 1 (references): `nice uv run pytest tests/test_refs.py`. References stay
  independent of `src/sass`.
- Before step 3's Python tests, when adapters or profiles changed:
  `nice uv run python -m tests.autocases SM89 --write --report tests/cases/generated/SM89.gaps.json`.
- At step 5 (GPU), a word gets evidence once every GPU scenario for it passes. A reference that
  disagrees with the GPU is wrong; never loosen `same`/`excused` to make it pass.
- Then regenerate every plan and report the change affects (minutes each, one core per
  architecture; run them in the background, under `nice`):
  `for a in SM75 SM80 SM86 SM89 SM90 SM100 SM101 SM103 SM107 SM120; do nice uv run python -m tests.autocases $a --write --report tests/cases/generated/$a.gaps.json & done; wait`
  (each needs that architecture's manifest), then run the full-architecture Python and
  Ghidra tests.
- Finally `nice uv run sass-refresh-coverage` (about 19 minutes, in the background; see
  AGENTS.md "Coverage"), then rerun `gaps.py OP` for the remaining gaps.

CI fails on stale plans (`tests.autocases --check`) and stale coverage, and reruns the
per-architecture Python tests. It is a backstop, not a replacement for these local runs:
it refreshes nothing and has no Ghidra or GPU.

Growth check: GPU and Ghidra time scale with forms × profile vectors. Use the
default sampled run; reach for `--exhaustive` only for sweeps.

## 5. Record and report

Update the documentation that the change affects:
- `tests/README.md` "Generated shared cases": the adapter paragraphs that list
  remaining gaps.
- `docs/semantics.md`: the semantic scope.
- `docs/hardware-facts.md`: anything the GPU established, dated.

Compare with the starting commit using the same renderer as the PR coverage comment:
`python3 .github/scripts/coverage_comment.py local REV`. It compares the working
tree's `coverage.json` and `coverage.html` with REV's, names the classes and modifier
forms whose semantics or evidence changed, and lists what needs attention (weakened
semantics, lost evidence, semantics no evidence has run) with the next check for each.
Every 🔴 item it lists needs an explanation or a fix.

Report:
- the opcode and why it was chosen;
- that comparison, trimmed to the target opcode plus any regression;
- what changed (references, families, adapters, profiles);
- each remaining gap, labelled semantic, evidence or unreachable, with its reason;
- test runs and their scope, labelling any failures as semantics or testing/environment;
- the commands to reproduce.

Claim "all combinations" only when `gaps.py` shows 100% modelled and 100% verified.
