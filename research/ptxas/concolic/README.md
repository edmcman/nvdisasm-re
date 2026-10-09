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
`--architectures SM75,...`, `--dictionary FILE` (repeatable; default `ptx.dict`),
`--cmplog`, `--resume`. Build prerequisites are listed in `build.sh`; `afl-qemu-trace`,
SymQEMU and LibAFL (rev `70259b66`, `libafl/Cargo.lock`) are pinned.

## Architecture

- **Mutation workers** (Rust, `libafl/src/main.rs`): LibAFL forkserver over
  `afl-qemu-trace ptxas`, edge-coverage feedback, havoc + dictionary mutations. Each
  rotates through the architecture targets every 60 s with a separate saved
  state per target. ptxas writes its `-o` file only on success; an observer submits every
  input that left one behind, including inputs run inside other stages.
- **Concolic workers**: SymQEMU over native ptxas, whole file symbolic, 30 s per run.
  Solver outputs are submitted unvalidated.
- **Coordinator** (`pipeline.py`): candidates arrive on a Unix socket; dedup, SQLite
  catalogue, compilation cache, leases. Accepted cases are broadcast to the mutation workers over
  LibAFL LLMP. Two analysis workers compile for every target and decode with the
  repository md decoder (`forms.py`); one replay worker captures PTX IR, COP DAG and ORI
  through the GDB loggers for cases with a new form or sequence.
- **Identity**: candidates are keyed by `corpus.name_key`, which ignores whitespace,
  comments and consistent renaming of names the file binds (declarations, labels,
  register families), but keeps opcodes, modifiers, strings, `.target` and any bound name
  also used as a mnemonic. SASS forms (`forms.signature`) abstract register numbering,
  labels and recognized data immediates; LOP3 tables, shift counts and unknown literals stay literal.
- **SM101** is compiled as `sm_110` (CUDA 13 rename) and decoded with the SM101 md; the
  ELF reports `SM110`. Checked against nvdisasm 13.4 on the generic seed.

## Outputs

`catalogue.sqlite` (candidates, observations, compilations, forms, sequences, replays,
concolic leases), `catalogue.jsonl` (exported validated forms), `status.json`,
`sources/<key>.ptx` (first spelling), `cases/<cache key>/` (cubin, `sass.txt`,
`normalized.json`, `ir/`), `concolic/<key>/`, `mutation/<worker>/<target>.state`, `logs/`.
Stopping (SIGINT/SIGTERM or deadline) checkpoints workers and kills child process groups;
`--resume` retries interrupted leases once. The PTX IR logger and the COP/ORI loggers are specific to
CUDA 13.0.88 (checked by hash).

## Findings

- **Byte mutation rarely yields valid PTX.** Without CmpLog, 17,068 mutations over 2
  minutes produced 1 compiling file. Mutating only a valid seed, about 1 in 200–400 compiles
  regardless of havoc stack depth. Coverage feedback also fills the queue with
  rejected inputs, since parse errors produce new coverage.
- **ptxas lexes with flex and parses with bison** (`fatal flex scanner internal error`). Opcodes
  and target names are identifiers looked up with `strcmp` in a hash table, so only
  bucket-mates are compared: `add` vs `cctl`, `fns`, `_mma`, `vmax2`; `st` vs nothing.
  Types and modifiers (`.u32`, `.global`) are matched by the flex DFA, never compared.
- **CmpLog** (`--cmplog`; qemuafl logs the first two pointer arguments of every call) raised
  compiling mutants from ~0 to ~4.5% on the generic seed. They were
  almost all renamed identifiers and whitespace: no new instructions or forms. It halves mutation throughput, so it is off by default. RedQueen runs on an entry's
  first scheduling; LibAFL's example waits for the second, which short campaigns never reach.
- **Concolic** produced all new forms so far (e.g. SM100 `UIADD3`/`UIADD3.X` uniform carry,
  `FADD`/`IMAD`/`MOV` constant-bank variants). Each run submits hundreds of candidates at
  once, ×9 targets, which outpaces two analysis workers; the backlog persists for `--resume`.
- Seeds with `.target sm_100` are incompatible in sm_75–sm_90 contexts, so workers there
  have only the generic seed as a valid parent.
- `dict/harvest.py` builds `ptx-strcmp.dict` (815 tokens: opcodes, special registers,
  targets, options) from the operands of ptxas `strcmp` calls (LD_PRELOAD `strcmp_log.c`)
  over all seeds and targets. It is not a default: in an A/B (generic seed only, 4 workers,
  no concolic, no CmpLog, 3 min, 2 runs per arm, ~9,800 executions each) both arms found
  0–2 compiling non-seed mutants, all degenerate (kernel deleted, or `.version` and a
  mangled `.target` only). Havoc places tokens at arbitrary byte offsets, so an opcode
  rarely lands in an opcode position with a compatible operand list.

Baseline (blocking AFL++ + SymQEMU custom mutator, SM100 only,
`/tmp/ptx-concolic/hour-01`): 559 executions in 390 s, 33 queue entries. Two-minute LibAFL
validation (8 + 1 workers, all targets, before CmpLog): 17,068 mutations (141/s), 146 forms,
28 IR replays captured; resume preserved the catalogue.

## Tests

```sh
uv run --with pytest python -m pytest research/ptxas/concolic
```

Covers candidate keys (whitespace, strings, token boundaries, binding renames, mnemonic
safety) and form normalization (register renaming, aliasing, RZ, predicates, data
immediates, LOP3 tables, unknown literals) on SM89 words built with `tests/semantic_cases.encode`.

## Historical

`historical_run.py` / `historical_parallel.py` are the AFL++ custom-mutator and blocking
generators, kept for reference with `afl-symqemu-adapter.patch` and the `symqemu-x86_64`
wrapper. `build.sh` no longer builds the adapter. `symqemu-movcond.patch` (null expression and
false-arm mask in symbolic conditional moves) and `symcc-ptx-input.patch` (optional
symbolic byte interval, historical `{add, sub}` restriction) remain applied; the
concolic worker clears their restricting environment variables. `first-run.json` records
the first AFL++ hybrid run.
