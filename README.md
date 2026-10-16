# nvdisasm-re

[![opcode semantics](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Fedmcman%2Fnvdisasm-re%2Fmain%2Fprocessor%2FSASS%2Fsemantics%2Fbadge.json)](https://raw.githack.com/edmcman/nvdisasm-re/main/processor/SASS/semantics/coverage.html)
[![CI](https://github.com/edmcman/nvdisasm-re/actions/workflows/ci.yml/badge.svg)](https://github.com/edmcman/nvdisasm-re/actions/workflows/ci.yml)

Reverse engineering NVIDIA's `nvdisasm` (CUDA 13.4) to recover how it models SASS instructions,
and building on that a SASS assembler, disassembler, and Ghidra processor with instruction semantics.

`nvdisasm` doesn't hard-code instruction knowledge: it embeds an encrypted, compressed
**machine description** per GPU architecture. This repo extracts them and derives a
per-instruction table of operand semantics (registers read and written, register spans,
pipes, latencies). The results for CUDA 13.4 are committed, so you can browse them without
running anything:

```
out/SM80/md/FFMA.txt              every md CLASS definition for FFMA
out/SM80/semantics/FFMA.json      operand semantics for those classes
out/SM80/latencies.txt            connectors, operation sets, latency tables
```

[`docs/machine-descriptions.md`](docs/machine-descriptions.md) describes the embedded blobs,
the `out/` layout and JSON format, and how to re-extract them.

The machine descriptions are complete enough to decode instructions. `src/sass/` turns them
into an assembler and two disassemblers, checked against `nvdisasm` itself. Install with `uv sync`.

## Assembler (`src/sass/assemble/`)

Assemble nvdisasm-style SASS into raw code or native CUDA cubins, with labels,
explicit scheduling controls, parameters, shared memory, constants, and multiple kernels:

```sh
uv run sass-assemble examples/assembler/write_value.sass -o write_value.cubin
uv run sass-assemble --format raw --arch SM89 code.sass -o code.bin
```

Assembly needs no CUDA tools. Raw output supports every committed machine description;
cubin output supports established ABI profiles (SM107 is raw-only). Native execution
is verified on SM89. See [`docs/assembling.md`](docs/assembling.md) for syntax, the
Python API, scheduling responsibilities, and architecture-specific limits.

## Reference decoder (`src/sass/decode/pydecode.py`)

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

## Ghidra processor (`processor/SASS/`)

Build and install with Python 3 and a working Ghidra installation (tested with Ghidra 12.1.4).
The generator uses the committed `out/` descriptions.

1. From the repository root, set your Ghidra installation path and build the languages
   for every architecture:

   ```sh
   export GHIDRA_INSTALL_DIR=/path/to/ghidra_12.1.4_PUBLIC
   uv run sass-build-languages
   ```

   To build only selected architectures, pass them as arguments, for example
   `uv run sass-build-languages SM89 SM90`. The default uses Ghidra's
   `support/sleigh` launcher (`support/sleigh.bat` on Windows). For another compiler
   location, pass `--sleigh /path/to/sleigh` (or `sleigh.exe`).

2. Close Ghidra and copy the complete processor module into its installation:

   ```sh
   cp -a processor/SASS "$GHIDRA_INSTALL_DIR/Ghidra/Processors/"
   ```

   When updating, replace the installed `SASS` directory with the newly built module.
   Keep `.sla`, `.slaspec`, `sass_common.sinc`, `sass.pspec`, `sass.cspec`, and
   `sass.ldefs` from the same build together. Mixing versions can fail at import with
   `Unknown register: R1`.

3. Restart Ghidra and import a `.cubin` (extract one from a fat binary with
   `cuobjdump -xelf`). Ghidra's ELF loader recognizes it and picks the language
   `SASS:LE:64:smXX` from the ELF header. Each architecture also has a
   `smXX_readable` variant, which writes floating-point arithmetic as the plain
   formula (`a f* b f+ c`), ignoring rounding modes, denormal flushing, fused
   rounding and NaN/signed-zero selection; choose it in the import dialog's
   Language field. The default variant computes hardware-exact results.

Semantics, their coverage and their limits are documented in
[`docs/semantics.md`](docs/semantics.md); generator details and Ghidra quirks in
[`docs/sleigh.md`](docs/sleigh.md).

## Disclaimer

Independent interoperability research. Not affiliated with or endorsed by NVIDIA. The
contents of `out/` are derived from NVIDIA's `nvdisasm` binary.
