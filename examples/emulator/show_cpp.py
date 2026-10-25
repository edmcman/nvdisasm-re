"""Show the C++ the native emulator compiles for a kernel: python examples/emulator/show_cpp.py [kernel] [out.cpp]
(a kernel of times3.sass; default times3). It prints one function per instruction word, each executing
the instruction's lifted semantics, or writes the whole translation unit (with its dispatch and
scheduler tables, run by include/sass/run.hpp) to out.cpp."""
import sys
from pathlib import Path
from sass.assemble import assemble_cubin
from sass.decode import cubin
from sass.emulate import codegen

name = sys.argv[1] if len(sys.argv) > 1 else "times3"
image = cubin.kernel_image(assemble_cubin((Path(__file__).parent / "times3.sass").read_text()), name)
source, instructions = codegen.kernel(image["arch"], image["code"], 0x100000)   # the request's default code address
if len(sys.argv) > 2:
    Path(sys.argv[2]).write_text(source)
    print(f"{name}: {len(instructions)} instructions, {len(source.splitlines())} lines of C++ in {sys.argv[2]}")
else:
    functions = source[source.index("static void i0"):source.index("static void (*const CODE[])")]
    print(functions.rstrip())
