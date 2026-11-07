from sass.emulate import cxx
from sass.semantics import ordered
from dataclasses import replace


def without_deferred_waits(body):
    if body is None: return None
    def statement(value):
        match value:
            case ordered.If(): return replace(value, yes=without_deferred_waits(value.yes), no=without_deferred_waits(value.no))
            case ordered.Atomic(): return replace(value, body=without_deferred_waits(value.body))
            case _: return value
    return replace(body, statements=tuple(statement(value) for value in body.statements
                                         if not isinstance(value, ordered.Special) or
                                         value.operation != ordered.BLOCK_WAIT_DEFERRED))


def requirements(body):
    operations = [statement.operation for statement in ordered.linearize(body)
                  if isinstance(statement, ordered.Special)]
    return (any(operation.suspends or operation.runtime is None for operation in operations),
            any(operation.suspends for operation in operations))


def function(body, name, next_pc=0, arch="SM89", definition_uses=None, *, runtime=True):
    types, code = ordered.prepare(body)
    if width := max((type_ for type_ in types.values() if isinstance(type_, int) and type_ > cxx.WIDEST), default=0):
        raise NotImplementedError(f"{width}-bit temporary (values hold at most {cxx.WIDEST} bits)")
    suspends = any(isinstance(s, ordered.Special) and s.operation.suspends for s in code)
    if not runtime and any(isinstance(s, ordered.Special) and (
                           s.operation.suspends or not s.operation.runtime or
                           s.results and (len(s.results) != 1 or isinstance(s.results[0], ordered.Handle) or
                                          not s.operation.runtime.startswith("sass_prim_")))
                           for s in code):
        raise ValueError("ordered body requires a runtime")
    retained = {index: slot for slot, index in enumerate(sorted(ordered.retained_temporaries(code)))}
    locals_ = [f"{cxx.ty(type_ if isinstance(type_, int) else 64)} q{index}{{}};"
               for index, type_ in types.items() if index not in retained]
    arguments = "sass::Ctx &c" + (", sass::ordered::Runtime &runtime" if runtime else "")
    if suspends: arguments += ", sass::ordered::Frame &f"
    returns = "sass::ordered::Status" if runtime else "void"
    prefix = "static" if runtime else "SASS_HD static"
    complete = "return sass::ordered::Complete;" if runtime else "return;"
    lines = [f"{prefix} {returns} {name}({arguments}) {{", "auto &t = c.t;", *locals_]
    if not runtime and any(isinstance(s, ordered.Boundary) for s in code): lines.append("sass::Atomic atomic(c);")
    if suspends:
        lines += ["if (f.status != sass::ordered::Suspended) return f.status;", f"f.values.resize({len(retained)});",
                  "try {", "switch (f.position) {", "case 0: goto ordered_0;"]
        lines += [f"case {position}: goto ordered_{position};" for position, statement in enumerate(code)
                  if position and isinstance(statement, ordered.Special) and statement.operation.suspends]
        lines += ['default: throw sass::Fault{"invalid resume position"};', "}"]
    else:
        lines += ["bool atomic = false;", "try {"] if runtime else []

    def target(value):
        index = ordered.temp_index(value)
        return f"f.values[{retained[index]}]" if index in retained else f"q{index}"

    def low():
        lower = cxx.Lower(arch, definition_uses)
        for index, type_ in types.items():
            if isinstance(type_, int):
                name = target(ordered.temp(index, type_))
                lower.scopes[0][id(ordered.temp(index, type_))] = f"{cxx.ty(type_)}({name})" if index in retained else name
        return lower

    for position, statement in enumerate(code):
        lower = low()
        lines.append(f"ordered_{position}: {{")
        value = lambda e: target(e) if isinstance(e, ordered.Handle) else lower.value(e)
        match statement:
            case ordered.Move(destination, expr): lower.emit(f"{target(destination)} = {value(expr)};")
            case ordered.ReadRegister(destination, location):
                lower.emit(f"{target(destination)} = {cxx.masked(lower.read(*location), destination.w)};")
            case ordered.WriteRegister(location, expr): lower.write(*location, value(expr))
            case ordered.ReadMemory(destination, space, address):
                lower.emit(f"{target(destination)} = c.space({cxx.SPACES[space]}).load<{destination.w}>({cxx.SPACES[space]}, {value(address)});")
            case ordered.WriteMemory(space, address, expr):
                lower.emit(f"c.space({cxx.SPACES[space]}).store<{expr.w}>({cxx.SPACES[space]}, {value(address)}, {value(expr)});")
            case ordered.Special(spec, args, results):
                if (spec.runtime or spec.native) and not spec.suspends:
                    if spec.native:
                        expression = f"runtime.{spec.native}({', '.join(value(arg) for arg in args)})"
                        lower.emit(f"{target(results[0])} = {expression};" if results else f"{expression};")
                    elif results:
                        if len(results) != 1 or not spec.runtime.startswith("sass_prim_"):
                            raise NotImplementedError(f"native operation binding {spec.name}")
                        expression = lower.primitive(spec.runtime.removeprefix("sass_prim_"), [value(arg) for arg in args])
                        lower.emit(f"{target(results[0])} = {cxx.masked(expression, results[0].w)};")
                    else: lower.operation(spec, args)
                    lines += lower.lines
                    lines.append("}")
                    continue
                if spec.suspends:
                    lower.emit(f"f.position = {position};")
                    lower.emit("sass::ordered::Reply reply;")
                    lower.emit("if (f.pending) reply = runtime.poll(f.ticket);")
                    lower.emit("else {")
                for argument, type_ in zip(args, spec.arguments):
                    if isinstance(type_, ordered.Opaque):
                        lower.emit(f"runtime.arena.check({value(argument)}, {cxx.string(type_.name)});")
                arguments = ", ".join(value(arg) for arg in args)
                call = f"runtime.{spec.native}({arguments})" if spec.native else \
                       f"runtime.call({cxx.string(spec.name)}, std::array<sass::u128, {len(args)}>{{{arguments}}})"
                if spec.suspends:
                    lower.emit(f"reply = {call};")
                    lower.emit("}")
                    lower.emit("if (reply.status == sass::ordered::Suspended) { f.pending = true; f.ticket = reply.ticket; return reply.status; }")
                    lower.emit("f.pending = false;")
                else:
                    lower.emit(f"auto reply = {call};")
                    lower.emit('if (reply.status == sass::ordered::Suspended) throw sass::Fault{"non-suspending operation suspended"};')
                lower.emit('if (reply.status == sass::ordered::Faulted) throw sass::Fault{"ordered runtime fault"};')
                lower.emit(f'if (reply.values.size() != {len(results)}) throw sass::Fault{{"runtime result arity mismatch"}};')
                for index, result in enumerate(results):
                    type_ = ordered.value_type(result)
                    expr = f"reply.values[{index}]"
                    width = type_ if isinstance(type_, int) else 64
                    lower.emit(f'if ({expr} > {cxx.literal((1 << width) - 1, width)}) throw sass::Fault{{"runtime result outside its declared width"}};')
                    if isinstance(type_, ordered.Opaque):
                        lower.emit(f"runtime.arena.check(uint64_t({expr}), {cxx.string(type_.name)});")
                    lower.emit(f"{target(result)} = {expr};")
            case ordered.Branch(condition, yes, no):
                cond = value(condition)
                lower.emit(f"if ({cond}) goto ordered_{yes}; goto ordered_{no};")
            case ordered.Jump(destination):
                lower.emit(f"goto ordered_{destination};")
            case ordered.Boundary(enter):
                if runtime:
                    lower.emit(f"runtime.{'acquire' if enter else 'release'}();")
                    lower.emit(f"{'f.atomic' if suspends else 'atomic'} = {str(enter).lower()};")
                else: lower.emit(f"atomic.{'enter' if enter else 'leave'}();")
            case ordered.Transfer(_, destination):
                lower.emit(f"t.pc = {value(destination)};")
                lower.emit("f.status = sass::ordered::Complete; return f.status;" if suspends else complete)
        lines += lower.lines
        lines.append("}")
    lines.append(f"ordered_{len(code)}:;")
    lines += [f"t.pc = UINT64_C({next_pc});"]
    lines += ["f.status = sass::ordered::Complete; return f.status;" if suspends else complete]
    if not runtime: return "\n".join([*lines, "}"])
    lines += ["} catch (const sass::Fault &error) {"]
    if suspends:
        lines += ["if (f.atomic) { f.atomic = false; runtime.release(); }", "f.pending = false;",
                  "f.error = error.message; f.status = sass::ordered::Faulted; return f.status;"]
    else:
        lines += ["if (atomic) runtime.release();", "throw;"]
    lines += ["}", "}"]
    return "\n".join(lines)
