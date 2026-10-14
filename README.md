# nvdisasm-re

Reverse engineering NVIDIA's `nvdisasm` (CUDA 13.4) to recover how it models SASS instructions.

`nvdisasm` doesn't hard-code instruction knowledge. It embeds an encrypted, compressed
**machine description** per GPU architecture and parses it at startup. This repo extracts
those descriptions and turns them into a per-instruction table of operand semantics:
which registers are read and written, how many registers each operand spans, which
pipeline it issues to, and its dependency latencies.

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

## Usage

```sh
uv sync                              # installs the `sass` package (src/sass) and its console scripts
uv run sass-extract-md [nvdisasm]    # -> out/raw/{md,latencies,patterns}_SMxx.txt (gitignored)
uv run sass-layout-md                # -> out/SMxx/...
```

`sass-extract-md` needs `objdump`; without an argument it uses the nvdisasm located by `src/sass/paths.py`.
The offsets are specific to the CUDA 13.4 build (V13.4.92); other builds will need new
addresses (see [`docs/nvdisasm-binary.md`](docs/nvdisasm-binary.md)).

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

## Decoding SASS from the machine descriptions

The `md` files are complete enough to decode instructions. `src/sass/` turns them into two
disassemblers, both checked against `nvdisasm` itself.

### Reference decoder (`src/sass/decode/pydecode.py`)

A pure-Python decoder that reproduces `nvdisasm`'s text exactly:

```sh
uv run sass-decode kernel.cubin                               # arch from the ELF header, every .text.* section
uv run sass-decode --arch SM89 code.bin                       # raw instruction binary
uv run sass-decode --arch SM89 0278050004000000000f000000e20f00 47790000f02700000000800300ea0f00
```

```
        /*0000*/  MOV R5, 0x4 ;                        /* 0278050004000000000f000000e20f00 */
        /*0010*/  BRA 0x2810 ;                         /* 47790000f02700000000800300ea0f00 */
```

It runs on the committed `out/` files with no dependencies. `--float-hex` prints float
immediates as raw bits, the way the Ghidra decoder does. From Python:

```python
from sass.decode import pydecode
d = pydecode.decode("SM89", bytes.fromhex("247210ffff00000002008e0700e40f10"), addr=0)
d.text        # 'IMAD.MOV.U32 R16, RZ, RZ, R2.reuse'
d.klass.name  # 'imad_pseudo__RRR_RRR' (the md CLASS that matched)
d.env         # decoded field values, e.g. d.env["Rd"] == 16
```

Words that don't decode raise `pydecode.NoMatch`. The rules it implements (how classes are
matched, which md conventions `nvdisasm` honours, and its printing quirks) are listed in
[`docs/decoding.md`](docs/decoding.md).

### Ghidra processor (`processor/SASS/`)

Build and install with Python 3 and a working Ghidra installation. The generator
uses the committed `out/` descriptions, so extracting `nvdisasm` again is unnecessary.
The commands below use the native Linux compiler; this project has been tested with
Ghidra 12.1.4.

1. From the repository root, set your Ghidra installation path and build all nine
   architecture languages:

   ```sh
   export GHIDRA_INSTALL_DIR=/path/to/ghidra_12.1.4_PUBLIC
   uv run sass-build-languages
   ```

   To build only selected architectures, pass them as arguments, for example
   `uv run sass-build-languages SM89 SM90`. The default uses Ghidra's
   `support/sleigh` launcher (`support/sleigh.bat` on Windows). For another compiler
   location, pass `--sleigh /path/to/sleigh` (or `sleigh.exe`). The build generates
   `.slaspec`, `.sla`, `sass.ldefs`, and coverage manifests in
   `processor/SASS/data/languages/`, and checks compiler diagnostics and Ghidra's
   decompressed language size limit.

2. Close Ghidra and copy the complete processor module into its installation:

   ```sh
   cp -a processor/SASS "$GHIDRA_INSTALL_DIR/Ghidra/Processors/"
   ```

   The destination must be writable; use the permissions appropriate for your
   installation. When updating, replace the installed `SASS` directory with the
   newly built module. Keep `.sla`, `.slaspec`, `sass_common.sinc`, `sass.pspec`,
   `sass.cspec`, and `sass.ldefs` from the same build together. Mixing versions can
   fail at import with `Unknown register: R1`.

3. Restart Ghidra. Import raw SASS instruction bytes using **Raw Binary**, then
   choose **SASS**, **little endian**, **64 bit**, and the matching variant (such
   as `sm89`, language ID `SASS:LE:64:sm89`). Each architecture also has a
   `smXX_readable` variant. It writes floating-point arithmetic as the plain
   formula (`a f* b f+ c`), ignoring rounding modes, denormal flushing, fused
   rounding and NaN/signed-zero selection. The default variant computes
   hardware-exact results. Each instruction is 16 bytes. Use the
   kernel's extracted `.text.*` bytes and its code address; importing a whole cubin
   as raw bytes includes ELF headers and other sections. This module supplies the
   processor languages; it does not install a CUDA cubin loader or populate kernel
   parameters automatically. Disassemble the code and run analysis to view p-code
   and supported instruction semantics in the decompiler.

`uv run sass-gen-sleigh SM75 SM80 ...` generates a SLEIGH spec per architecture
(`sass_smXX.slaspec`, not committed: run the generator first) and `sass.ldefs` with languages `SASS:LE:64:smXX`
(`--fp=readable` writes `sass_smXX_readable.slaspec`, language `SASS:LE:64:smXX_readable`). Ghidra
compiles the `.sla` on first use. P-code preserves register reads and writes for every
class. Instruction semantics add native integer, move, memory, control-flow and
floating-point overrides, plus runtime primitives for other floating-point and warp
operations. Unsupported variants retain explicit opaque calls. Operand values,
coverage, runtime interfaces and verification commands are documented in
[`docs/semantics.md`](docs/semantics.md).
Predicates print glued to the mnemonic
(`@P0:IMAD ...`) because SLEIGH can't put a space there, and float immediates print as raw bits.
Computed 64-bit immediates may print as negative hexadecimal; the comparison tool checks
their bits modulo 2^64 for `MOV.64`, `UMOV.64`, and `MOV64IUR`.

Modifier-dependent spans are exact, including zero and non-power-of-two counts, and
are selected inside per-class semantic subtables. Shared display fields and positive bit
constraints keep compiled languages below Ghidra's 16 MiB decompressed `.sla` limit.
Operand types identify register resources: an immediate with a register-like latency
connector (such as IPA's attribute offset) is not treated as a register. Spans extending
past the register bank use a sink so unusual words still decode; their dataflow is undefined.

For a scratch build, run `uv run sass-build-languages SM89 --output /tmp/sass-languages`
(the build copies shared definitions automatically), then set
`SASS_SLEIGH_OUT=/tmp/sass-languages` when running Ghidra tests. When using
`sass.sleigh.gen` directly, copy `sass_common.sinc`, `sass.pspec`, and `sass.cspec`
to the scratch directory first. Register dataflow is checked by
`uv run pytest tests -m ghidra -k dataflow` (requires pyghidra; see Testing).

### GPU semantics oracle

`uv run pytest tests -m gpu --require-gpu` compiles 100 SM89 CUDA kernels
(and patched variants), runs them on the GPU, then executes the same kernel text in Ghidra's p-code
emulator. It checks 12,800 random/edge output values, including address setup,
constant parameters, memory operations, predication, and loops. Patched fixtures
select specific FP16 lane/MMA modes and FMNMX NaN modes; both the GPU and emulator
execute the resulting cubin. A 64-bit PTX comparison checks the ptxas ISETP.EX
lowering. F2FP fixtures cover packed F16/BF16 RN/RZ, E4M3/E5M2 FP8 packing
with H1 C merges, and E4M3/E5M2 FP8-to-F16 UNPACK_B H0/H1 across all byte
encodings. See
[semantic coverage and limitations](docs/semantics.md) for the
verified modes and excluded warp/concurrency operations.

### Testing against nvdisasm

The tests use `nvdisasm -b SMxx` as an oracle, over three corpora:

- **real**: every instruction in the cubins inside the CUDA libraries (`cuobjdump -xelf`
  of cuBLASLt, cuSPARSE, ...), about 3–6M unique words per architecture
- **synthetic**: 64 random words per md class that satisfy its encoding constraints
- **mutated**: real words with one or two bits flipped

```sh
uv run sass-compare-pydecode SM89 real 0       # pydecode vs nvdisasm (0 = whole corpus)
uv run sass-compare-ghidra SM89 synthetic 0    # Ghidra (generated SLEIGH) vs pydecode
uv run pytest tests                    # layers 0-1: helpers, per-class ratchet vs tests/baseline/
uv run pytest tests -m ghidra --arch all --fp all   # layers 2-4 (compiled languages)
uv run pytest tests -m gpu --require-gpu            # layer 5 (SM89 GPU)
```

`nvdisasm` results are cached in `.cache/oracle.sqlite`. Tool locations live in `src/sass/paths.py`:
`NVDISASM` (default: the CUDA 13.4 copy in uv's cache or `/tmp/.venv`), `NVCC`, `CUOBJDUMP`,
`GHIDRA_PY` and `SASS_SLEIGH_OUT` override them.

Current results (CUDA 13.4 `nvdisasm`):

- pydecode matches `nvdisasm` on every real word for all eight architectures that have
  cubins (about 38M words), and on all synthetic and mutated words except 17 SM75
  HMMA words. Some valid encodings print as blank lines in `nvdisasm`; the cause is
  still unknown, so those are excluded from the counts.
- The generated SLEIGH matches pydecode on every SM75 and SM89 word in all three corpora.
  The other architectures are still being run.

## Other findings

- Opcode and operand names inside the binary are ROT13-obfuscated, so `strings` finds none.
- The default "dataflow analysis" models the pre-Volta branch stack (SSY/PBK/PCNT/PRET/...
  pushes, SYNC/BRK/CONT/RET/... pops) to infer the targets of indirect branches, which it
  prints as `TARGET=` annotations. `--print-life-ranges` builds on the same framework,
  using the connector def/use data above.

Function addresses and details are in [`docs/nvdisasm-binary.md`](docs/nvdisasm-binary.md).

## Disclaimer

Independent interoperability research. Not affiliated with or endorsed by NVIDIA. The
contents of `out/` are derived from NVIDIA's `nvdisasm` binary.
