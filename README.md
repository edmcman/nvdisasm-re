# nvdisasm-re

[![opcode semantics](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Fedmcman%2Fnvdisasm-re%2Fmain%2Fprocessor%2FSASS%2Fsemantics%2Fbadge.json)](https://raw.githack.com/edmcman/nvdisasm-re/main/processor/SASS/semantics/coverage.html)
[![CI](https://github.com/edmcman/nvdisasm-re/actions/workflows/ci.yml/badge.svg)](https://github.com/edmcman/nvdisasm-re/actions/workflows/ci.yml)

Reverse engineering NVIDIA's `nvdisasm` (CUDA 13.4) to recover how it models SASS instructions,
and building on that a SASS assembler, disassembler, and Ghidra processor with instruction semantics.

`nvdisasm` doesn't hard-code instruction knowledge: it embeds an encrypted, compressed
**machine description** per GPU architecture. This repo extracts them and derives a
per-instruction table of operand semantics (registers read and written, register spans,
pipes, latencies). The results for CUDA 13.4 are committed, so you can browse them without
running anything, in `src/sass/md/data/`:

```
SM80/md/FFMA.txt               every md CLASS definition for FFMA
SM80/semantics/FFMA.json       operand semantics for those classes
SM80/latencies.txt             connectors, operation sets, latency tables
SM80/patterns.txt              a tiny pattern file (JUMP_UNCOND: BRA)
```

[`docs/machine-descriptions.md`](docs/machine-descriptions.md) describes the embedded blobs,
the `src/sass/md/data/` layout and JSON format, and how to re-extract them.

The machine descriptions are complete enough to decode instructions. `src/sass/` turns them
into an assembler and two disassemblers, checked against `nvdisasm` itself. Install with `uv sync`.

## Using `sass` as a library

The `sass` package installs from git like any other dependency; the wheel carries the machine
descriptions, and needs no CUDA tools, Ghidra or GPU:

```sh
uv add git+https://github.com/edmcman/nvdisasm-re   # or: pip install git+https://github.com/edmcman/nvdisasm-re
```

It caches derived data (decode tables, `nvdisasm` results) in `$SASS_CACHE`, else `.cache/` in a
checkout, else `$XDG_CACHE_HOME/sass` (`~/.cache/sass`). Lifting every instruction of a cubin
to semantics ([`examples/lift_cubin.py`](examples/lift_cubin.py)):

```python
import sys
from pathlib import Path
from sass.decode import cubin, pydecode
from sass.semantics import lifter

data = Path(sys.argv[1]).read_bytes()
arch = cubin.elf_arch(data)
for name, body in cubin.text_sections(data).items():
    print(arch, name)
    for addr, word in enumerate(cubin.words(body)):
        addr *= 16
        try: sem = lifter.lift(arch, word, addr)
        except pydecode.NoMatch as e: print(f"  {addr:#06x} undecodable: {e}"); continue
        print(f"  {addr:#06x} {sem.text:40} {sem.status} {sem.reason}")
        for loc, v in sem.writes: print(f"      {loc} <- {v}")
        for s in sem.stores: print(f"      {s.space}[{s.address}] <- {s.value}")
        if sem.flow: print(f"      flow {sem.flow.kind} -> {sem.flow.target}")
```

On [`examples/assembler/write_value.sass`](examples/assembler/write_value.sass) (`sass-assemble` it to a cubin first):

```
SM89 .text.write_value
  0x0000 MOV R1, c[0x0][0x28]                     native
      R1 <- cbank[0x28:64]:32
  ...
  0x0050 STG.E [R2.64], R5                        native
      ram[R2:R3] <- R5
  0x0060 EXIT                                     native
      flow return -> 0x0:64
```

`sem.status` is `native` when the instruction has modelled semantics and `opaque` otherwise
(`sem.reason` says why). [`examples/assemble_and_lift.py`](examples/assemble_and_lift.py) goes
from SASS text to a word, nvdisasm's text and the lifted IR, and
[`examples/evaluate.py`](examples/evaluate.py) runs a lifted instruction on concrete registers
(`sass.semantics.evaluate`). The Ghidra processor and the test suite need a checkout.

Whole kernels run on the native emulator, which compiles each kernel's lifted semantics to C++
(`sass.emulate`, a C++20 compiler on `PATH`; [`docs/emulation.md`](docs/emulation.md)):
[`examples/emulator/`](examples/emulator) has three examples: `run_kernel.py` launches one over
4096 threads, `trace_kernel.py` prints each step's register writes and the outputs, and
`equivalence.py` compares kernels thread by thread on the same launches, and `show_cpp.py`
prints the C++ the emulator compiles for a kernel (all three run the kernels of
its `times3.sass`, assembled without CUDA tools).

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
        /*0000*/  MOV R5, 0x4 ;                                                /* 0278050004000000000f000000e20f00 */
        /*0010*/  BRA 0x2810 ;                                                 /* 47790000f02700000000800300ea0f00 */
```

It runs on the committed `src/sass/md/data/` files with no dependencies. `--float-hex` prints float
immediates as raw bits, the way the Ghidra decoder does. From Python:

```python
from sass.decode import pydecode
d = pydecode.decode("SM89", bytes.fromhex("247210ffff00000002008e0700e40f10"), addr=0)
d.text        # 'IMAD.MOV.U32 R16, RZ, RZ, R2.reuse'
d.klass.name  # 'imad_pseudo__RRR_RRR' (the md CLASS that matched)
d.env         # decoded field values, e.g. d.env["Rd"] == 16
```

`sass.provenance.evidence("SM89", word)` says which evidence ran a word's class and modifier form,
e.g. `{"gpu": {"SM89", "SM90"}, "reference": {"SM89"}}`, from the committed
`processor/SASS/semantics/evidence.json`; the wheel leaves it out (85 MB), so outside a checkout
point `$SASS_EVIDENCE` at a copy.
Words that don't decode raise `pydecode.NoMatch`. The rules it implements (how classes are
matched, which md conventions `nvdisasm` honours, and its printing quirks) are listed in
[`docs/decoding.md`](docs/decoding.md).

## Ghidra processor (`processor/SASS/`)

Build and install with Python 3 and a working Ghidra installation (tested with Ghidra 12.1.4).
The generator uses the committed `src/sass/md/data/` descriptions.

1. From the repository root, set your Ghidra installation path and build the languages
   for every architecture:

   ```sh
   export GHIDRA_INSTALL_DIR=/path/to/ghidra_12.1.4_PUBLIC
   uv run sass-build-languages
   ```

   To build only selected architectures, pass them as arguments, for example
   `uv run sass-build-languages SM89 SM90`; `--fp exact` or `--fp readable` builds
   one floating-point variant (default: both). Detailed semantic coverage is opt-in
   with `--coverage`; normal builds write only the small runtime calling-convention
   manifest. The SLEIGH compiler defaults to Ghidra's `support/sleigh` launcher
   (`support/sleigh.bat` on Windows); pass `--sleigh /path/to/sleigh` (or `sleigh.exe`)
   to use another.

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

## Development

[`AGENTS.md`](AGENTS.md) maps the source tree and the workflow for adding semantics;
[`tests/README.md`](tests/README.md) describes the tests (`uv run pytest`, plus Ghidra and GPU
markers). Reverse-engineering findings live in [`docs/`](docs/): the `nvdisasm` binary
([`nvdisasm-binary.md`](docs/nvdisasm-binary.md)), hardware-established behavior
([`hardware-facts.md`](docs/hardware-facts.md)), the test harness ([`testing.md`](docs/testing.md))
and open questions ([`open.md`](docs/open.md)).

## Disclaimer

Independent interoperability research. Not affiliated with or endorsed by NVIDIA. The
contents of `src/sass/md/data/` are derived from NVIDIA's `nvdisasm` binary.
