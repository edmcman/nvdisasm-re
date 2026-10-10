# PTX → SASS lowering generator (LibAFL + SymQEMU)

Generates PTX kernels that the stripped CUDA 13.0.88 `ptxas` accepts, compiles them for
all nine repository architectures and catalogues the distinct SASS lowering forms, with
compiler IR witnesses for new ones. Compiler coverage guides enumeration only; there is
no crash or vulnerability objective.

## Run

```sh
bash research/ptxas/concolic/build.sh     # pinned tools into research/ptxas/concolic/tools, needs network
python3 research/ptxas/concolic/run.py --output research/ptxas/concolic/tools/run-01 --duration 120
```

`run.py` builds and runs `libafl/` (`ptx-sass-gen`). Options:
`--n 8` mutation workers, `--m 1` concolic workers, `--duration` seconds,
`--seed FILE` (repeatable, given to every target; default `generic_sm75.ptx` with its `.target` set to each architecture's target),
`--architectures SM75,...`, `--dictionary FILE` (repeatable; default `dict/ptx-lexer.dict`), `--resume`.
RedQueen comparison-guided mutations are enabled by default; `--no-redqueen` disables
the native CmpLog/colorization stages without changing the campaign's compiler or architecture identity.
Build prerequisites are listed in `build.sh`; `afl-qemu-trace`, SymQEMU and LibAFL
(rev `70259b66`, `libafl/Cargo.lock`) are pinned.

The default `dict/ptx-lexer.dict` is generated from the pinned compiler: its 485 reserved
lexer spellings plus registered opcode names (see "Compiler-derived dictionary"). In repeated
trials it performed the same as `ptx.dict`; it is the default because it is reproducible from the
compiler rather than maintained by hand.

The previous default `ptx.dict` includes all 139 opcode roots from the PTX ISA 9.4 reserved
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
  (LibAFL's `StdTMinMutationalStage`), calibration, RedQueen on first scheduling, then
  power-scheduled havoc + dictionary tokens with AFL++'s stack depth; scheduling is AFL++'s (favored minimal entries per edge,
  `explore` schedule). Each worker rotates through the targets every 60 s with a saved state per
  target. ptxas writes its `-o` file only on success: an observer submits every input that
  left one and marks its facts in feature slots past qemu's edges (`features.rs`: PTX
  mnemonic, opcode, modifiers, operand shape, and SASS opcodes; compiling files only), so a
  new opcode counts like a new edge.
- **LibAFL workarounds** (rev `70259b66`): the minimizer records a trimmed entry as its own
  parent (a `ClosureStage` clears it), retries skipped mutations without counting them
  (`Counted` wrapper), and needs every entry's edge indexes (`non_metadata_removing` scheduler).
  LibAFL has no AFL deterministic stage, so that stage is not used.
- **RedQueen** (`libafl/src/redqueen.rs`): colorization changes only instruction-region
  bytes while preserving coverage. A separate QEMU CMPLOG forkserver traces the original
  and colorized complete kernels; LibAFL's `AflppRedQueen` generates region replacements
  with arithmetic and transformation matching. Local adapters preserve the fixed PTX
  scaffold, and generated candidates use the ordinary coverage feedback and compilation
  observer. Harvested comparison tokens also feed havoc. Shared memory includes QEMU's
  trailing comparison-site arrays, which stay intact between paired traces. Empty regions
  are skipped. Each worker's activity includes cumulative per-target `redqueen` counters:
  `attempts`, `comparison_sites`, `candidates`, and `admitted` corpus entries; they survive
  checkpoints. The current enabled setting is stored in catalogue metadata.
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

`catalogue.jsonl` is maintained during the campaign: it is rebuilt from SQLite on
startup/resume, updated with each five-second status report when new forms appear,
and updated at shutdown. Each line contains one unique form per architecture, its
normalized signature, compiler hash and paths to a witness PTX source and cubin.
Atomic replacement lets readers inspect a complete snapshot while generation runs.

`status.json` also records AFL-style statistics under `afl`: live corpus size,
pending entries (never scheduled), favored entries, pending favored entries and
corpus size by architecture. Counts sum the worker/target corpora; copies shared
between workers are counted separately. `afl.contexts` retains each worker/target's
latest snapshot across target rotations and resume, including queue cycles, recent
executions per second, covered QEMU edge slots, edge-map density and covered PTX/SASS
feature slots. Coverage is reported per context because workers can cover the same
slots. These are map occupancy counts, not exact counts of compiler branches or forms.
Workers report at startup, about once per second between fuzz stages, and before
checkpointing at a target change or shutdown. Every five seconds the console summary
includes mutation and candidate rates, accepted compilations, total and per-architecture
form counts, per-architecture opcode counts, sequence counts, and replay/concolic states.
Execution counts include
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
uv run --with pytest python -m pytest research/ptxas/concolic/test_pipeline.py
cargo test --release --manifest-path research/ptxas/concolic/libafl/Cargo.toml
# Include the pinned QEMU comparison fixture and C layout check (requires built tools and cc):
cargo test --release --manifest-path research/ptxas/concolic/libafl/Cargo.toml -- --include-ignored
```

Rust tests cover marker parsing, scaffold invariance under 1,000 havoc mutations, PTX/SASS
facts, numeric/string RedQueen replacements, harvested tokens, and checkpoint metadata.
The optional checks validate the pinned QEMU map layout and solve a comparison through
actual QEMU colorization and tracing while preserving the input scaffold.

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

## Token substitution

Before native colorization, each new corpus entry gets one-token substitutions within parser
token classes (`libafl/src/tokens.rs`). This is enabled by default; `--no-token-substitution`
disables it. The worker reads the lexer automaton from the pinned compiler, as
`lexer_vocab.py` does (see "Compiler-derived dictionary"). It lexes the instruction region
with longest match and backup. A reserved token is replaced by every other spelling whose
lexer action returns the same parser token, for example `.rn` by `.rz` or `.u32` by `.s32`.
An identifier is replaced only at a statement head (after `;`, `{`, `}`, a label or a
`@p`/`@!p` guard, and not a label itself), by every registered opcode name in
`opcode_registry.json` (252 full names such as `mul.lo`). Other identifiers, numbers,
punctuation, comments and the fixed scaffold are never changed. Replacements may change
length. On load, every class spelling must lex alone to an action of its own class.
The Rust lexer matches `lexer_vocab.lex` on all the seed files (an ignored test that needs
the pinned compiler); the Python lexer is checked against ptxas under GDB.

Candidates run through ordinary compilation and coverage feedback. Activity records
`token_substitution` counters (`attempts`, `slots`, `candidates`, corpus `admitted`).
Catalogue observations use `token-substitution:<worker>:<target>` origins.
This replaces the synthetic opcode RedQueen stage, which presented virtual string
comparisons with same-length registry roots. Its evidence remains in
`synthetic-opcodes-first-run.json`, and its counters remain deserializable in old checkpoints.

`--token-probe SEED OUT` compiles every candidate of a seed for the seed's own `.target`
with real ptxas. It uses no fuzzing or QEMU, and OUT must be a new directory.
`token-probe-first-run.json` contains:

| Seed region | Class | Candidates | Accepted |
| --- | --- | ---: | --- |
| `add.u32 %r2, %r0, %r1;` (SM75) | opcode | 251 | addc div max min mul.hi mul.lo mul24.hi mul24.lo rem shr sub subc |
| | type 275 | 37 | .s32 .f16x2 .f32 |
| `mad.wide.u32 %rd2, %r0, %r1, %rd1;` (SM90) | opcode | 251 | none |
| | type 275 | 37 | .s32 |

The earlier same-length stage accepted six roots on the same `add` seed. Operand
conventions bound one-token substitution: no other opcode accepts the wide `mad` operands
unchanged.

Campaign comparison, under the same conditions as the dictionary comparison below (SM75,
5 minutes, three concurrent arms with 8 workers each, no concolic workers, per-target seed),
in `token-substitution-first-run.json`:

| Arm | Mutations | Accepted | Forms | Sequences | Region opcodes |
| --- | ---: | ---: | ---: | ---: | --- |
| `ptx.dict` | 140,198 | 85 | 9 | 6 | mov |
| `ptx-lexer.dict` | 140,948 | 75 | 9 | 6 | mov |
| `ptx-lexer.dict` + token substitution | 145,947 | 105 | 31 | 28 | abs bfind brev clz cnot mov neg not popc |

The stage made 25,518 candidates over 99 attempts. 1,216 entered the corpus, and 51
catalogued candidates originate from it. It also found `.f16x2`. Execution rate was unchanged.
The removed same-length stage reached 21 forms and five opcodes in the corresponding arm.

Dictionary control with token substitution on in every arm (same conditions,
`dictionary-control-first-run.json`):

| Dictionary | Mutations | Accepted | Forms | Sequences | Additional region vocabulary |
| --- | ---: | ---: | ---: | ---: | --- |
| none (empty file) | 143,448 | 96 | 39 | 32 | `alloca`, `.pred` |
| `ptx.dict` | 141,259 | 117 | 39 | 36 | `alloca`, `bar.arrive`, `bar.cta.arrive`, `barrier.arrive`, `barrier.cta.arrive`, `.f16x2` |
| `ptx-lexer.dict` | 130,206 | 151 | 47 | 45 | `alloca`, `.b64 .u64 .e5m2x2 .f16x2` |

All three reached `abs bfind brev clz cnot mov neg not popc`. Five such rounds
(`dictionary-repeats.json`; the table is round 1):

| Dictionary | Forms per round | Mean ± sd | Sequences mean ± sd | Distinct forms, all rounds |
| --- | --- | --- | --- | ---: |
| none | 39 30 36 40 34 | 35.8 ± 4.0 | 33.6 ± 3.6 | 55 |
| `ptx.dict` | 39 35 37 32 49 | 38.4 ± 6.5 | 34.2 ± 3.7 | 60 |
| `ptx-lexer.dict` | 47 41 43 28 37 | 39.2 ± 7.2 | 35.4 ± 5.5 | 61 |

Paired by round, a dictionary adds +2.6 (`ptx.dict`) or +3.4 (`ptx-lexer.dict`) forms over
none, with paired standard deviations of 8–9. `ptx-lexer.dict` minus `ptx.dict` is
+0.8 ± 8.6. The sign changes between rounds. Execution rates match. With token substitution,
the dictionary has no measurable effect in five-minute SM75 runs, and the two dictionaries
are indistinguishable. Any real difference is smaller than about four forms.

```sh
cargo run --release --offline --manifest-path research/ptxas/concolic/libafl/Cargo.toml \
  --target-dir research/ptxas/concolic/tools/libafl-target -- \
  --token-probe research/ptxas/concolic/synthetic_add.ptx /tmp/ptx-token-probe
```

Refresh the registry with the pinned compiler (GDB needs ptrace access):

```sh
PTX_OPCODE_REGISTRY=/tmp/opcode_registry.json gdb -q -batch \
  -ex 'source research/ptxas/concolic/opcode_cmp_registry.py' \
  --args /usr/local/cuda-13.0/bin/ptxas -arch=sm_75 -o /dev/null \
  research/ptxas/concolic/synthetic_add.ptx
```

The extractor verifies the compiler SHA256 before reading its table. Review the output
before replacing `opcode_registry.json` and rebuilding the Rust worker.

### Generic map query trace

`map_trace.py` probes lookup `0x426d60`, selecting tables with string hash
`0x427630` and strcmp equality `0x4277b0`. It records queries, return addresses,
and the union of observed resident keys. `map-trace-first-run.json` contains four
successful compiler runs: integer add, float add, predicates/carry/cache/branch
modifiers, and wide mad. The broad sample used labels `L_probe`/`L_done`,
`setp.eq.u32`, `@%p0 add.cc.u32`, `addc.u32`, `shl.b32`, `ld.global.ca.u32`,
`st.global.wb.u32`, `bra.uni`, and `%tid.x`.

The instruction lookup callers are `0x46c6a8` and `0x46c7bc`; the secondary table
is queried at `0x46c7da` (empty in these runs). They receive roots such as `add`,
`setp`, `ld`, and qualified names such as `mad.wide`. No separate queries for
`u32`, `f32`, `rn`, `eq`, `cc`, `global`, `ca`, `wb`, or `uni` were observed.
A hook on this lookup therefore covers registered qualified instruction names,
but these observations do not support using it for arbitrary type/modifier tokens.

Other string tables handle CLI options, architecture names, ROT13 internal settings,
texture-reference categories, internal metadata names, register declaration prefixes
(`%r<`), function names and ELF sections/symbols. Most of these queries are unrelated
to the mutable instruction region. Integer-key, pointer-key, and structured-key map
callbacks also occur and are counted separately; the tracer does not decode them.

```sh
PTX_MAP_TRACE=/tmp/map.json gdb -q -batch \
  -ex 'source research/ptxas/concolic/map_trace.py' \
  --args /usr/local/cuda-13.0/bin/ptxas -arch=sm_75 -o /dev/null \
  research/ptxas/concolic/synthetic_add.ptx
```

### Qualified names and suffix parsing

`prefix_trace.py` records lexer actions/semantic values and parser reductions;
`prefix-trace-first-run.json` contains successful float, wide-mad and broad sample runs.
Addresses are native addresses in the pinned CUDA 13.0.88 binary.

The lexer is `0x720f00`: initial-state pointers are at `0x203c020`, character
transitions at `0x720f4d`/`0x720f7d`/`0x720fde`, accepting action selection at
`0x720f81`, and action dispatch at `0x720fc7` through `0x203a5a8`.
This is a generated table-driven lexer, not a series of string hash lookups.
Suffix recognition produces numeric enums or type objects before the parser sees them.

| Text | Lexer action | Action address | Returned token |
| --- | ---: | --- | ---: |
| `mad.wide` | 528 | `0x723e6f` | 258 (identifier) |
| `.u32` | 258 | `0x724981` | 275 (type) |
| `.f32` | 279 | `0x724651` | 275 (type) |
| `.rn` | 160 | `0x721249` | 288 (rounding; value 1) |
| `.eq` | 74 | `0x722b86` | 320 (comparison; value 1) |
| `.cc` | 112 | `0x7236df` | 359 (value 1) |
| `.global` | 66 | `0x722be3` | 303 |
| `.ca` | 148 | `0x72135d` | 290 (cache; value 1) |
| `.wb` | 153 | `0x7212ea` | 290 (cache; value 6) |
| `.uni` | 193 | `0x7231c7` | 325 (value 1) |

`mad.wide.u32` is tokenized as the identifier `mad.wide`, then the type `.u32`.
`add.rn.f32` is identifier `add`, rounding `.rn`, then type `.f32`.
Thus some dotted components belong to the lookup name while others have dedicated
lexer rules. The exact boundary depends on the lexer rule, not a universal dot split.

Parser entry is `0x4ce6b0`, lexer return site `0x4ced17`, token translation table
`0x1d15fa0`, and reduction-action dispatch `0x4ce7b0` via `0x1d10a68`.
Observed instruction suffix reductions include:

- Rounding: rule 426 at `0x4d0f07`, writes high nibble of parsing context +`0x25d`.
- Cache mode: rule 430 at `0x4d0ce5`, writes bits 3–6 at context +`0x25e`.
- Carry: rule 406 at `0x4d10fc`, writes bit 4 at context +`0x25b`.
- Type: rule 373 at `0x4d5b9f`, calls `0x4a9e30` to process the type object.

The parsing context in those cases is loaded from the parser's caller context +`0x448`.
For suffix feedback, the character automaton or token boundary is the relevant hook;
a generic map hook alone misses these tokens. Token-group alternatives could be derived
from the lexer automaton and grouped by returned token without a hand-written PTX list,
but automaton extraction and that mutation hook are not yet implemented.

```sh
PTX_PREFIX_TRACE=/tmp/prefix.jsonl gdb -q -batch \
  -ex 'source research/ptxas/concolic/prefix_trace.py' \
  --args /usr/local/cuda-13.0/bin/ptxas -arch=sm_100 -o /dev/null \
  research/ptxas/add_f32.ptx
```

### Parser recognition feedback

`--parser-recognition` adds opt-in coverage at successful registered-opcode lookups
and dedicated lexer keyword/type actions. It replaces the broad transition experiment.
There is no active character-transition hook: the obsolete `--lexer-transitions` option
is removed, and checkpoints made with it enabled are rejected because the slot meanings
changed. Old campaigns with transition feedback disabled retain their original layout.

Opcode recognition is recorded at `0x46c6b4`, after either opcode-table lookup succeeded.
EAX contains the stable opcode ID; misses bypass the site. Thus a known opcode with
invalid operands can earn feedback before compilation succeeds. Reserved-token recognition
records the selected lexer action ID at dedicated action handlers. Generic identifier
rules 528/529, numeric/string rules and scanner fallback machinery are excluded.
The audited reserved-token partition includes modifiers, types, built-in register names
and some declaration keywords; this is recognition, not final instruction validation.

`recognition_hooks.py` extracts the 467 reserved-token handler addresses from the pinned
compiler's action table, then adds the successful opcode-lookup site. It uses no PTX
spelling inventory. `parser-recognition.ijon` is the generated configuration, embedded
in the Rust worker. Refresh it with:

```sh
python3 research/ptxas/concolic/recognition_hooks.py /usr/local/cuda-13.0/bin/ptxas \
  research/ptxas/concolic/parser-recognition.ijon
```

Ordinary edges retain their 1 MiB map. IJON follows with 64 KiB set coverage plus its
unused 4 KiB max/min reservation; accepted PTX/SASS features follow that reservation.
Activity reports `covered_parser_recognitions`. The flag is persisted and cannot change
on resume. `qemu-ijon-quiet.patch` gates upstream runtime logging behind `AFL_DEBUG`.

The real-QEMU probe asserts that unknown `banana`/`papaya` opcodes, renamed operands,
and an unknown `.blorp` suffix add no recognition coverage relative to an empty region.
`sub.u32 %r2;` is rejected for operand mismatch but earns the same recognition as valid
`sub.u32 %r2, %r0, %r1;`. Unknown `.rq` and `.rx` spellings have the same recognition map,
whereas `.rn` and `.rz` differ. All repeated maps are byte-identical.

```sh
cargo run --release --offline --manifest-path research/ptxas/concolic/libafl/Cargo.toml \
  --target-dir research/ptxas/concolic/tools/libafl-target -- \
  --recognition-probe research/ptxas/concolic/synthetic_add.ptx /tmp/ptx-recognition-probe
```

Evidence is in `parser-recognition-first-run.json`. A 10-second SM75 pipeline run
with native RedQueen and the (since replaced) synthetic opcode comparisons disabled completed successfully,
then resumed for five seconds with recognition coverage intact. Changing the flag on
resume and loading an obsolete transition-feedback checkpoint both fail explicitly.
All ten Rust tests, including the real-QEMU tests, pass. This verifies endpoint recognition
feedback, not intermediate prefix progress or a campaign discovery-rate improvement.
Runtime map enumeration or symbolic modeling of lexer table loads is the next step for
vocabulary-independent intermediate progress. The old transition experiment remains
recorded in `lexer-transitions-first-run.json` as historical evidence, not an active mode.

### Compiler-derived dictionary

`lexer_vocab.py` reads the pinned compiler's flex `-F` lexer table: start-state pointers at
`0x203c020` (index 1 is the normal start condition), transition entries
`{u32 check, i32 next}` at `state + 8*c`, next state `state + 8*next`, accept action at
`state - 4`. Only reserved actions 60..526 are vocabulary. The walk follows only states from
which a reserved accept is reachable. Generic identifier actions 528/529 therefore never
contribute, although their states lie on keyword prefixes. That subgraph is acyclic:
467 actions and 485 strings.
Each action handler ends with `mov $token,%eax; jmp 0x7211d6`. This groups the strings into
96 parser-token classes, for example rounding 288, type 275 and cache 290.
`lexer_vocab.json` records those classes. `dict/ptx-lexer.dict` adds the registry names
and roots from `opcode_registry.json`, because opcode names are identifiers to the lexer.

```sh
python3 research/ptxas/concolic/lexer_vocab.py /usr/local/cuda-13.0/bin/ptxas \
  research/ptxas/concolic/lexer_vocab.json research/ptxas/concolic/dict/ptx-lexer.dict
```

The extractor writes nothing unless all of these hold:
- the qualified-names action table above reproduces;
- the reserved subgraph has no cycle;
- every handler matches the pattern;
- `.rq`, `.rx`, `.blorp`, `.r`, `banana`, `papaya` and `%r1` are absent;
- the string count is 400–600.

`lexer_vocab.lex` replays the automaton with longest match and backup. Comment actions
544/545 read through `*/` or a newline themselves. `lexer_vocab_check.py FILE...` compares
it with the real lexer under GDB. ptxas lexes 919 actions of built-in PTX before each file,
and each file's actions must then match as one contiguous run. All ten `../*.ptx` seeds
and `instruction.ptx` match exactly.

`measure_vocab.py RUN...` reports, per architecture of each catalogue, the accepted
instruction regions' registered statement-head opcodes and reserved tokens by class, along
with forms, sequences and first-observation origins. It counts no identifiers other than
registry heads.

Comparison, 5 minutes on SM75: three arms run concurrently, each with 8 mutation workers,
no concolic workers and the default per-target seed. The SM75 region is `mov.b32 %r2, %r0;`.
Native RedQueen is on. The counts are for accepted instruction regions
(`vocab-dictionary-first-run.json`). Forms include about six scaffold forms.

| Arm | Mutations | Accepted | Forms | Sequences | Region opcodes | Reserved tokens |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| `ptx.dict` | 145,738 | 66 | 9 | 6 | mov | `.b32 .f32 .s32 .u32` |
| `ptx-lexer.dict` | 150,126 | 58 | 9 | 6 | mov | `.b32 .f32 .s32 .u32` |
| `ptx-lexer.dict` + synthetic opcodes | 136,204 | 148 | 21 | 21 | abs clz mov neg not | `.b32 .f32 .s32 .u32` |

The compiler-derived dictionary performs the same as `ptx.dict`. Neither reaches a new
opcode. Both reach only the same-length `mov` types, which `TokenReplace` can place.
The synthetic stage contributed 42 accepted inputs, four new same-length opcodes and
more than twice the forms. Random dictionary placement and stacked havoc limit discovery,
not vocabulary. Therefore, by the decision rule, the class substitution stage is justified.
This is one short trial per arm on one region.
