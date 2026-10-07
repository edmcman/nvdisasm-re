# nvdisasm-re

Reverse engineering NVIDIA's `nvdisasm` (CUDA 13.4) to recover how it models SASS instructions.

`nvdisasm` doesn't hard-code instruction knowledge. It embeds an encrypted, compressed
**machine description** per GPU architecture and parses it at startup. This repo extracts
those descriptions and turns them into a per-instruction table of operand semantics:
which registers are read and written, how many registers each operand spans, which
pipeline it issues to, and its dependency latencies.

## What's embedded

For each of SM75, SM80, SM86, SM89, SM90, SM100, SM101, SM103 and SM120, `.data` holds
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

## Usage

```sh
uv run extract_md.py /path/to/nvdisasm   # -> out/raw/{md,latencies,patterns}_SMxx.txt (gitignored)
python3 build_out.py                     # -> out/SMxx/...
```

`extract_md.py` needs `objdump` and pulls in `lz4` through its inline uv script metadata.
The offsets are specific to the CUDA 13.4 build (V13.4.92); other builds will need new
addresses (see `AGENTS.md`).

The generated output from CUDA 13.4 is committed, so you can browse it without
running anything:

```
out/SM80/latencies.txt            connectors, operation sets, latency tables
out/SM80/patterns.txt
out/SM80/md/_00_ARCHITECTURE.txt  md preamble, one file per top-level section
...                               (REGISTERS, TABLES, OPERATION PREDICATES, ...)
out/SM80/md/FFMA.txt              every CLASS definition for FFMA
out/SM80/semantics/FFMA.json      operand semantics for those classes
```

Each md is split losslessly: the files together contain every line of the original.

## `semantics/<OPCODE>.json`

A list with one record per instruction class:

```json
{
  "cls": "ffma__RCR_RCR", "opcodes": ["FFMA"], "pipes": ["fmalighter_pipe"],
  "inst_type": "INST_TYPE_COUPLED_MATH",
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

- `role` comes from where the connector appears in the latency tables. TRUE columns and
  ANTI rows are reads; TRUE rows, OUTPUT, and ANTI columns are writes.
- `span` is the number of consecutive registers, `ceil(SIZE/32)`. About 10% of sizes depend
  on modifiers (LDG `.64`/`.128`, HMMA formats). For those, `span` is the size expression
  as a string, which must be evaluated against the instruction's decoded modifiers.
- `implicit` lists connectors that apply to the opcode but never appear in any `FORMAT`,
  such as scoreboards.
- Roles apply to the whole opcode set, so conditional operands aren't distinguished.

## Other findings

- Opcode and operand names inside the binary are ROT13-obfuscated, so `strings` finds none.
- The default "dataflow analysis" models the pre-Volta branch stack (SSY/PBK/PCNT/PRET/...
  pushes, SYNC/BRK/CONT/RET/... pops) to infer the targets of indirect branches, which it
  prints as `TARGET=` annotations. `--print-life-ranges` builds on the same framework,
  using the connector def/use data above.

Function addresses and details are in [`AGENTS.md`](AGENTS.md).

## Disclaimer

Independent interoperability research. Not affiliated with or endorsed by NVIDIA. The
contents of `out/` are derived from NVIDIA's `nvdisasm` binary.
