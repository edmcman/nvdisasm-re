"""Lifted instructions as C++ translation units for `sass.emulate.build`: fragments (independent
single instructions, for differential tests) and whole kernels."""
from sass.decode import pydecode
from sass.emulate import cxx, definitions, metadata, ordered
from sass.semantics import lifter
import sass.semantics.ordered
from dataclasses import replace

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
        needs_runtime, resumable = ordered.requirements(semantics.body) if semantics.body is not None else (False, False)
        if needs_runtime:
            inner = function + "_ordered"
            body = ordered.function(semantics.body, inner, semantics.addr + 16, arch, uses) + f"""
SASS_HD static void {function}(sass::Ctx &c) {{
  sass::ordered::SchedulerRuntime runtime;
  {'sass::ordered::Frame frame;' if resumable else ''}
  runtime.standalone = true;
  runtime.bind(c, {'frame, ' if resumable else ''}c.warp.lane, 32);
  const auto status = {inner}(c, runtime{', frame' if resumable else ''});
  if (status == sass::ordered::Faulted) throw sass::Fault{{{'frame.error' if resumable else '"ordered runtime fault"'}}};
  if (status == sass::ordered::Suspended) throw sass::Fault{{"explicit context cannot complete the ordered operation"}};
}}
"""
        else: body = cxx.instruction(semantics, function, decoded, uses)
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
    prelude = PRELUDE + ('#include "sass/ordered_runtime.hpp"\n' if any("SchedulerRuntime" in body for body in bodies) else "")
    return prelude + (functions.source() if functions is not None else "") + "\n".join(bodies) + f"""
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
    return [(file, index + k) for file, index, count in semantics.written_locations() if file in TRACED for k in range(count)]


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
    for instruction in instructions:
        if instruction.semantics and instruction.semantics.body is not None:
            sass.semantics.ordered.prepare(instruction.semantics.body)
    empty_deferred = not any(ins.decoded and "dfrBlk" in ins.decoded.klass.name for ins in instructions)
    empty_deferred &= not any(statement.operation == sass.semantics.ordered.BLOCK_DEFER
                              for ins in instructions if ins.semantics and ins.semantics.body is not None
                              for statement in sass.semantics.ordered.linearize(ins.semantics.body)
                              if isinstance(statement, sass.semantics.ordered.Special))
    bodies, tables, runtime_functions, resumable = [], [], set(), set()
    functions = Definitions() if definition_calls else None
    for i, ins in enumerate(instructions):
        if ins.decoded is None: bodies.append(f"SASS_HD static void i{i}(sass::Ctx &) {{}}"); continue
        semantics = replace(ins.semantics, body=ordered.without_deferred_waits(ins.semantics.body)) \
            if empty_deferred else ins.semantics
        needs_runtime, suspends = ordered.requirements(semantics.body) if semantics.body is not None else (False, False)
        if needs_runtime:
            uses = set() if functions is not None else None
            bodies.append(ordered.function(semantics.body, f"r{i}", semantics.addr + 16, arch, uses))
            if functions is not None: functions.add(uses)
            bodies.append(f"SASS_HD static void i{i}(sass::Ctx &) {{}}")
            runtime_functions.add(i)
            if suspends: resumable.add(i)
        else: bodies.append(compiled(arch, semantics, ins.decoded, f"i{i}", functions))
        if dests := destinations(ins.semantics):
            tables.append(f"static const uint32_t D{i}[] = {{{', '.join(str(FILE_IDS[f] << 16 | n) for f, n in dests)}}};")
    count = len(instructions)
    ordered_table = (f'static const sass::OrderedStep ORDERED[] = '
                     f'{{{", ".join(f"r{i}" if i in resumable else "nullptr" for i in range(count))}}};\n'
                     if resumable else "")
    ordinary = runtime_functions - resumable
    ordinary_table = (f'static const sass::OrderedOrdinaryStep ORDINARY[] = '
                      f'{{{", ".join(f"r{i}" if i in ordinary else "nullptr" for i in range(count))}}};\n'
                      if ordinary else "")
    source = PRELUDE + '#include "sass/run.hpp"\n' + (functions.source() if functions is not None else "") + "\n".join(bodies + tables) + f"""
static void (*const CODE[])(sass::Ctx &) = {{{", ".join(f"i{i}" for i in range(count))}}};
{ordered_table}
{ordinary_table}
static const sass::Info INFO[] = {{{", ".join(ins.info(arch, i) for i, ins in enumerate(instructions))}}};
static const sass::Program PROGRAM{{UINT64_C({base:#x}), {count}, CODE, INFO, {'ORDERED' if resumable else 'nullptr'}, {'ORDINARY' if ordinary else 'nullptr'}}};
extern "C" uint32_t sass_run(const sass::Launch *launch, sass::Result *result) {{
  if (launch->step_lane) return sass::run<false, true>(PROGRAM, *launch, *result);
  return sass::run<{str(bool(runtime_functions)).lower()}, false, {str(bool(resumable)).lower()}>(PROGRAM, *launch, *result);
}}
"""
    return source, instructions
