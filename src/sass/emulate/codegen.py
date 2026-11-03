"""Lifted instructions as C++ translation units for `sass.emulate.build`: fragments (independent
single instructions, for differential tests) and whole kernels."""
from sass.emulate import cxx, definitions, metadata
from sass.semantics import lifter

PRELUDE = '#include "sass/machine.hpp"\n#include "sass/abi.hpp"\n'


class Definitions:
    """Validated functions for one translation unit; failed instructions add nothing."""

    def __init__(self):
        self.functions, self.signatures = {}, {}

    def add(self, uses):
        functions, signatures = {}, {}
        for use in sorted(uses - self.functions.keys()):
            label, widths = use
            signature = definitions.definition_name(label, widths), tuple(cxx.ty(w) for w in widths)
            if signature in self.signatures or signature in signatures:
                raise NotImplementedError(f"{label!r} {widths!r}: ambiguous C++ function signature")
            functions[use] = definitions.definition_function(label, widths)
            signatures[signature] = use
        self.functions.update(functions)
        self.signatures.update(signatures)

    def source(self):
        return "".join(self.functions[use] + "\n" for use in sorted(self.functions))


def compiled(arch, semantics, decoded, function, functions=None):
    """The C++ function executing one instruction; an IR form the lowering lacks becomes its fault."""
    try:
        uses = None if functions is None else set()
        body = cxx.instruction(semantics, function, decoded, uses)
        if functions is not None: functions.add(uses)
        return body
    except (NotImplementedError, ValueError) as error:
        return f"SASS_HD static void {function}(sass::Ctx &) {{ throw sass::Fault{{{cxx.string(f'not compiled: {error}')}}}; }}"


def fragments(instructions, fp="exact", definition_calls=True):
    """A library whose `sass_fragment(id, ...)` executes instruction `id` of [(arch, word, address)]."""
    functions = Definitions() if definition_calls else None
    bodies = [compiled(arch, lifter.lift(arch, word, address, fp), pydecode.decode(arch, word), f"f{i}", functions)
              for i, (arch, word, address) in enumerate(instructions)]
    return library(bodies, functions)


def semantic_fragments(semantics, definition_calls=True):
    """`fragments` of lifted (non-opaque) `Semantics` built directly, as IR tests do."""
    functions = Definitions() if definition_calls else None
    return library([compiled(s.arch, s, None, f"f{i}", functions) for i, s in enumerate(semantics)], functions)


def library(bodies, functions=None):
    table = ", ".join(f"f{i}" for i in range(len(bodies)))
    return PRELUDE + (functions.source() if functions is not None else "") + "\n".join(bodies) + f"""
static void (*const FRAGMENTS[])(sass::Ctx &) = {{{table}}};
extern "C" int sass_fragment(int id, sass::Thread *t, sass::Memory *m, const sass::WarpContext *w, const sass::Environment *e,
                             char *message, size_t size) {{
  sass::Ctx c{{*t, *m, *m, *w, *e}};
  try {{ FRAGMENTS[id](c); return 0; }} catch (const sass::Fault &f) {{ return sass::report(f, message, size); }}
}}
"""


FILE_IDS = metadata.FILE_IDS
TRACED = ("R", "UR", "P", "UP")   # the files a trace reports (schedule's architectural registers)


def destinations(semantics):
    """(file, index) of each register element the instruction writes, for traces."""
    return [(file, index + k) for location, _ in semantics.writes for file, index, count in [location.args] if file in TRACED for k in range(count)]


class Instruction(metadata.Instruction):
    def __init__(self, arch, word, address, fp):
        super().__init__(arch, word)
        self.semantics = lifter.lift(arch, word, address, fp) if self.decoded else None

    def info(self, arch, i):
        return super().info(arch, i, destinations(self.semantics) if self.semantics else ())


def kernel(arch, code, base, fp="exact", definition_calls=True):
    """(source, instructions): a library whose `sass_run(launch, result)` runs `code` (at `base`) in
    model 1 or 2 (include/sass/run.hpp)."""
    instructions = [Instruction(arch, code[i:i + 16], base + i, fp) for i in range(0, len(code), 16)]
    bodies, tables = [], []
    functions = Definitions() if definition_calls else None
    for i, ins in enumerate(instructions):
        if ins.decoded is None: bodies.append(f"SASS_HD static void i{i}(sass::Ctx &) {{}}"); continue
        bodies.append(compiled(arch, ins.semantics, ins.decoded, f"i{i}", functions))
        if dests := destinations(ins.semantics):
            tables.append(f"static const uint32_t D{i}[] = {{{', '.join(str(FILE_IDS[f] << 16 | n) for f, n in dests)}}};")
    count = len(instructions)
    source = PRELUDE + '#include "sass/run.hpp"\n' + (functions.source() if functions is not None else "") + "\n".join(bodies + tables) + f"""
static void (*const CODE[])(sass::Ctx &) = {{{", ".join(f"i{i}" for i in range(count))}}};
static const sass::Info INFO[] = {{{", ".join(ins.info(arch, i) for i, ins in enumerate(instructions))}}};
static const sass::Program PROGRAM{{UINT64_C({base:#x}), {count}, CODE, INFO}};
extern "C" uint32_t sass_run(const sass::Launch *launch, sass::Result *result) {{ return sass::run(PROGRAM, *launch, *result); }}
"""
    return source, instructions
