# Machine descriptions (`out/`)

`nvdisasm` doesn't hard-code instruction knowledge. It embeds an encrypted, compressed
**machine description** per GPU architecture and parses it at startup. The extracted
descriptions for CUDA 13.4 are committed in `out/`, along with a per-instruction table of
operand semantics derived from them.

## What's embedded

For each of SM75, SM80, SM86, SM89, SM90, SM100, SM101, SM103, SM107 and SM120, `.data` holds
three blobs:

| Blob | Contents |
|---|---|
| `md` | ~1,200–1,600 instruction `CLASS` definitions: `FORMAT` (syntax and operand names), `CONDITIONS` (legality rules), `PROPERTIES`, `PREDICATES` (operand bit sizes), `OPCODES`, `ENCODING` (bit layout) |
| `latencies` | Register resources, operand "connectors" (`Ra`, `Rb`, `Rc`, `Rd`, `Pu`, `URa`, …), operation sets (pipes), and `TABLE_TRUE` / `TABLE_OUTPUT` / `TABLE_ANTI` dependency latencies |
| `patterns` | A tiny pattern file (`JUMP_UNCOND: BRA`) |

Each blob is encrypted with a byte-chained stream cipher: a glibc-LCG keystream plus a
256-byte inverse S-box, seeded with a 16-bit per-architecture key. The `md` blobs are
also LZ4 stream-compressed in `{u32 raw, u32 comp, data}` chunks.

The descriptions carry **dataflow semantics, not functional semantics**. They tell you
that FFMA writes one register `Rd` and reads `Ra`, `Sb` and `Rc`. They never say that it
computes `Rd = Ra*Sb + Rc`.

## Layout

```
out/SM80/latencies.txt            connectors, operation sets, latency tables
out/SM80/patterns.txt
out/SM80/md/_00_ARCHITECTURE.txt  md preamble, one file per top-level section
...                               (REGISTERS, TABLES, OPERATION PREDICATES, ...)
out/SM80/md/FFMA.txt              every CLASS definition for FFMA
out/SM80/semantics/FFMA.json      operand semantics for those classes
```

Each md is split losslessly: the files together contain every line of the original.
`sass.md.parse` reads these split files (not `out/raw/`), so decoding, generation and tests
need only the committed `out/`.

## Re-extracting

```sh
uv run sass-extract-md [nvdisasm]    # -> out/raw/{md,latencies,patterns}_SMxx.txt (gitignored)
uv run sass-layout-md                # -> out/SMxx/...
```

The semantics JSON is written by `sass-layout-md` (`src/sass/md/layout.py`).
`sass-extract-md` needs `objdump` and the `lz4` module; without an argument it uses the nvdisasm located by `src/sass/paths.py`.
The offsets are specific to the CUDA 13.4 build (V13.4.92); other builds will need new
addresses (see [`nvdisasm-binary.md`](nvdisasm-binary.md)).

## `semantics/<OPCODE>.json`

A list with one record per instruction class:

```json
{
  "cls": "ffma__RCR_RCR", "alternate": false, "opcodes": ["FFMA"], "pipes": ["fmalighter_pipe"],
  "inst_type": "INST_TYPE_COUPLED_MATH",
  "sizes": {"IDEST_SIZE": 32, "ISRC_A_SIZE": 32, ...},
  "operands": [
    {"name": "Pg", "type": "Predicate", "resource": "PRED", "role": ["read"],  "span": 1},
    {"name": "Rd", "type": "Register",  "resource": "GPR",  "role": ["write"], "span": 1},
    {"name": "Ra", "type": "Register",  "resource": "GPR",  "role": ["read"],  "span": 1},
    {"name": "Sb", "type": "C", ...},
    {"name": "Rc", "type": "Register",  "resource": "GPR",  "role": ["read"],  "span": 1}
  ],
  "implicit": [{"name": "sBoard", "resource": "SCOREBOARD", "role": ["read", "write"]}, ...],
  "true_latency": {"GPR:Rd,Rd2": [{"FMAI_WITHOUT_IMAD`{Ra,Rb,Rc}": "4", "MIO_SLOW_OPS`{Ra,Rb,Rc}": "8", ...}]}
}
```

- `alternate` marks ALTERNATE (assembler-only) classes; `sizes` is the class's `PREDICATES`
  (operand sizes in bits); `pipes` are the suffixes of the class's pipe-qualified `OPCODES` aliases (`FFMAfmalighter_pipe`).
- `role` comes from where the connector appears in the latency tables. TRUE columns and
  ANTI rows are reads; TRUE rows, OUTPUT, and ANTI columns are writes.
- `span` is the number of consecutive registers, `ceil(SIZE/32)`. Some sizes depend on
  modifiers (LDG `.64`/`.128`, HMMA formats; 4-7% of spans on SM75-SM120, counted 2026-10-22). For those, `span` is the size expression
  as a string (`ceil((EXPR)/32)`), which must be evaluated against the instruction's decoded
  modifiers (`src/sass/md/expression.py`). `role` and `span` are `null` for operands that no
  latency table names.
- `implicit` lists connectors that apply to the opcode but never appear in any `FORMAT`,
  such as scoreboards.
- Roles apply to the whole opcode set, so conditional operands aren't distinguished.
