# PTX → SASS lowering generator (LibAFL + SymQEMU)

Generates PTX kernels that the stripped CUDA 13.0.88 `ptxas` accepts, compiles them for
all nine repository architectures and catalogues the distinct SASS lowering forms, with
compiler IR witnesses for new ones. Compiler coverage guides enumeration only; there is
no crash or vulnerability objective.

## Run

```sh
bash research/ptxas/concolic/build.sh /tmp/ptx-concolic     # pinned tools, needs network
python3 research/ptxas/concolic/run.py --output /tmp/ptx-concolic/run-01 --duration 120
```

`run.py` builds and runs `libafl/` (`ptx-sass-gen`). Options:
`--n 8` mutation workers, `--m 1` concolic workers, `--duration` seconds,
`--seed FILE` (repeatable; default `generic_sm75.ptx` plus `../*.ptx`),
`--architectures SM75,...`, `--dictionary FILE` (repeatable; default `ptx.dict`), `--resume`.
Build prerequisites are listed in `build.sh`; `afl-qemu-trace`, SymQEMU and LibAFL
(rev `70259b66`, `libafl/Cargo.lock`) are pinned.

The default `ptx.dict` includes all 139 opcode roots from the PTX ISA 9.4 reserved
instruction table and instruction headings (including `fabric`, absent from the table),
plus documented qualified instruction names. It retains the original type, register,
modifier and complete-statement suggestions. Source:
[NVIDIA PTX ISA](https://docs.nvidia.com/cuda/parallel-thread-execution/#instruction-statements).
These are mutation tokens; newer and target-specific names may be rejected by the
CUDA 13.0.88 compiler or the seed's PTX version. It does not enumerate every modifier
combination or supply a valid operand scaffold for every opcode.

## Seeds and the instruction region

Only the bytes between the `BEGIN_INSTRUCTION` and `END_INSTRUCTION` comments (line `//` or
inline `/* */`) are mutated or symbolic; the rest of the file is fixed. `libafl/src/input.rs`
(`PtxInput`, used by every byte mutator) and `corpus.instruction_region` (concolic) implement
the same rule, checked by `test_region_parsers_agree`. `generic_sm75.ptx` declares registers
of every width, defines a few inputs from special registers (`%tid.x`, `%tid.y`, `%ctaid.x`,
`%clock64`, `%globaltimer`: opaque, so never folded into the instruction) and stores `%r2`
so a result is not deleted as dead code. Its SASS is 4-5 instructions on every target.
Uniform special registers (`%ctaid.x`, `%clock64`) give uniform-operand forms.

## Architecture

- **Mutation workers** (Rust, `libafl/src/main.rs`): LibAFL forkserver over
  `afl-qemu-trace ptxas`, deferred to `0x4428e0` (`FORKSERVER_ENTRY`; skips ptxas's
  input-independent startup, 6.5x throughput). Per entry: trim on first scheduling
  (LibAFL's `StdTMinMutationalStage`), calibration, then power-scheduled havoc + dictionary
  tokens with AFL++'s stack depth; scheduling is AFL++'s (favored minimal entries per edge,
  `explore` schedule). Each worker rotates through the targets every 60 s with a saved state per
  target. ptxas writes its `-o` file only on success: an observer submits every input that
  left one and marks its facts in feature slots past qemu's edges (`features.rs`: PTX
  mnemonic, opcode, modifiers, operand shape, and SASS opcodes; compiling files only), so a
  new opcode counts like a new edge.
- **LibAFL workarounds** (rev `70259b66`): the minimizer records a trimmed entry as its own
  parent (a `ClosureStage` clears it), retries skipped mutations without counting them
  (`Counted` wrapper), and needs every entry's edge indexes (`non_metadata_removing` scheduler).
  LibAFL has no AFL deterministic stage, and its RedQueen cannot rebuild a region-only input,
  so neither is used.
- **Concolic workers**: SymQEMU over native ptxas with only the region symbolic, 30 s per run.
  One job per decoded (kernel, target); dispatch rotates through the targets. Every output is
  broadcast to the mutation workers, which evaluate it in that target's context: it joins their
  corpus only with new coverage (edges or PTX/SASS facts), and a compiling one is submitted to
  the catalogue under its concolic origin. In a 75 s check, 153 of 411 distinct outputs entered
  corpora.
- **Coordinator** (`pipeline.py`): candidates arrive on a Unix socket; dedup, SQLite
  catalogue, compilation cache, leases. A candidate is compiled for the newest target first;
  a rejection naming no target is recorded for the others without compiling them. Accepted
  cases are broadcast to the mutation workers over LibAFL LLMP. Two analysis workers compile
  and decode with the repository md decoder (`forms.py`); one replay worker captures PTX IR,
  COP DAG and ORI through the GDB loggers for cases with a new form or sequence.
- **Identity**: candidates are keyed by `corpus.name_key`, which ignores whitespace (ptxas
  also skips `\x1a`), comments and consistent renaming of names the file binds (declarations,
  labels, register families), but keeps opcodes, modifiers, literals, strings, `.target` and
  any bound name also used as a mnemonic. SASS forms (`forms.signature`) abstract register
  numbering, labels and recognized data immediates (including LOP3's operand); LOP3 truth
  tables, shift counts and unknown literals stay literal.
- **SM101** is compiled as `sm_110` (CUDA 13 rename) and decoded with the SM101 md; the
  ELF reports `SM110`. Checked against nvdisasm 13.4 on the generic seed.

## Outputs

`catalogue.sqlite` (candidates, observations, compilations, forms, sequences, replays,
concolic leases), `catalogue.jsonl` (exported validated forms), `status.json`,
`sources/<key>.ptx` (first spelling), `cases/<cache key>/` (cubin, `sass.txt`,
`normalized.json`, `ir/`), `concolic/<cache key>/`, `mutation/<worker>/{<target>.state,queue/<target>/}`, `logs/`.
Stopping (SIGINT/SIGTERM or deadline) checkpoints workers and kills child process groups;
`--resume` retries interrupted leases once. The PTX IR logger and the COP/ORI loggers are specific to
CUDA 13.0.88 (checked by hash).

`status.json` also records AFL-style statistics under `afl`: live corpus size,
pending entries (never scheduled), favored entries, pending favored entries and
corpus size by architecture. Counts sum the worker/target corpora; copies shared
between workers are counted separately. `afl.contexts` retains each worker/target's
latest snapshot across target rotations and resume, including queue cycles, recent
executions per second, covered QEMU edge slots, edge-map density and covered PTX/SASS
feature slots. Coverage is reported per context because workers can cover the same
slots. These are map occupancy counts, not exact counts of compiler branches or forms.
Workers report at startup, about once per second between fuzz stages, and before
checkpointing at a target change or shutdown. The console summary includes corpus
counts and campaign execution rate every five seconds. Execution counts include
seed/import evaluation, calibration and trimming as well as havoc.

## Findings

- **What made mutation work.** Whole-file byte havoc almost never yields valid PTX (17,068
  executions, 1 compiling file), and coverage feedback fills the queue with parse errors. Plain
  AFL++ did better through `trim` and deterministic bit flips but found no new instructions.
  Restricting mutation to the instruction region, a minimal scaffold, AFL++ scheduling, the
  deferred forkserver and PTX/SASS feature slots together give, in 3 minutes on 8 workers
  (all nine targets): ~110,000 executions, 115 distinct compiling kernels, 294 new forms.
- **Fuzzing vs concolic** (same run, 8 + 8 workers): concolic ran 148 jobs, 22,071 outputs,
  1,103 valid (5%), but only 2 distinct new kernels (`or`/`xor` with 0) and no new forms.
  SymQEMU flips one branch per output; in the lexer most flips take an error path, and the
  valid ones are mostly cosmetic. Concolic is worth at most one worker.
- **The dictionary bounds the vocabulary.** In the measured run, new instructions were
  the whole statements in
  `ptx.dict` (`add`, `sub`, `mul.lo`, `and`, `or`, `xor`, `shl`, `mov`, `setp`, `selp`),
  recombined with other operands, immediates, aliasing and types. A larger list of whole
  statements is the direct way to reach more opcodes. The default dictionary now also
  contains the full documented opcode vocabulary, allowing token swaps across hash buckets.
- **ptxas lexes with flex and parses with bison** (`fatal flex scanner internal error`). Opcodes
  and target names are identifiers looked up with `strcmp` in a hash table, so only
  bucket-mates are compared: `add` vs `cctl`, `fns`, `_mma`, `vmax2`; `st` vs nothing.
  Types and modifiers (`.u32`, `.global`) are matched by the flex DFA. CmpLog (removed)
  therefore offered few opcode swaps; it raised compiling mutants to ~4.5% before region
  restriction, almost all renames.
- **Scaffold side effects.** ptxas optimizes the whole kernel, so inputs derived from
  parameters, unused `.local`/`.shared` arrays and extra stores change their own lowering
  with every region edit; most "new" forms from such a scaffold were scaffold artifacts.
  Uninitialized inputs all lower to one register (`IADD3 R3, R0, R0`).
- `dict/harvest.py` builds `ptx-strcmp.dict` (815 tokens) from the operands of ptxas `strcmp`
  calls (LD_PRELOAD `strcmp_log.c`). It showed no benefit in an A/B under whole-file
  mutation (both arms 0-2 degenerate mutants) and is not a default.

Baseline (blocking AFL++ + SymQEMU custom mutator, SM100 only, `/tmp/ptx-concolic/hour-01`):
559 executions in 390 s, 33 queue entries.

## Tests

```sh
uv run --with pytest python -m pytest research/ptxas/concolic
cargo test --release --manifest-path research/ptxas/concolic/libafl/Cargo.toml
```

Rust tests cover marker parsing, scaffold invariance under 1,000 havoc mutations, and PTX/SASS facts.

Covers candidate keys (whitespace, `\x1a`, strings, token boundaries, binding renames, mnemonic
safety), Rust/Python region agreement, and form normalization (register renaming, aliasing, RZ, predicates, data
immediates, LOP3 tables, unknown literals) on SM89 words built with `tests/semantic_cases.encode`.

## Historical

`historical_run.py` / `historical_parallel.py` are the AFL++ custom-mutator and blocking
generators, kept for reference with `afl-symqemu-adapter.patch` and the `symqemu-x86_64`
wrapper. `build.sh` no longer builds the adapter. `symqemu-movcond.patch` (null expression and
false-arm mask in symbolic conditional moves) and `symcc-ptx-input.patch` (optional
symbolic byte interval, historical `{add, sub}` restriction) remain applied; the
concolic worker clears their restricting environment variables. `first-run.json` records
the first AFL++ hybrid run.
