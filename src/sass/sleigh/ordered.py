from collections import Counter
from dataclasses import dataclass, fields, is_dataclass, replace
from sass.semantics import bitvec, ordered
from sass.sleigh import lower


PREFIX = "sass_ordered_"
ATOMIC_ENTER = PREFIX + "atomic_enter"
ATOMIC_EXIT = PREFIX + "atomic_exit"


def prune(statements):
    kept = set(range(len(statements)))
    while True:
        used = {ordered.temp_index(temporary) for index in kept
                for value in ordered.statement_values(statements[index])
                for temporary in ((value,) if isinstance(value, ordered.Handle) else
                                  bitvec.leaves(value, "temp") if isinstance(value, bitvec.Expr) else ())}
        live = {index for index in kept
                if not (isinstance(statements[index], ordered.Move) or
                        isinstance(statements[index], ordered.ReadRegister) and statements[index].location[0] != "SR")
                or ordered.temp_index(statements[index].target) in used}
        if live == kept: break
        kept = live
    positions, count = {}, 0
    for index in range(len(statements) + 1):
        positions[index] = count
        count += index in kept
    return tuple(replace(statement, yes=positions[statement.yes], no=positions[statement.no])
                 if isinstance(statement, ordered.Branch) else
                 replace(statement, target=positions[statement.target]) if isinstance(statement, ordered.Jump) else statement
                 for index, statement in enumerate(statements) if index in kept)


def register_demands(roots):
    memo, maxima = {}, {}
    zero = bitvec.const(0, 32)

    def choice(keys, arms):
        return arms[0][1] if all(arm is arms[0][1] for _, arm in arms) else lower.sel(keys, arms, 32)

    def maximum(a, b):
        if a is b: return a
        if a is zero: return b
        if b is zero: return a
        if (a, b) in maxima: return maxima[a, b]
        match a.op, b.op:
            case "sel", "sel" if a.args[0] == b.args[0] and \
                    tuple(values for values, _ in a.args[1]) == tuple(values for values, _ in b.args[1]):
                result = choice(a.args[0], [(values, maximum(x, y)) for (values, x), (_, y) in zip(a.args[1], b.args[1])])
            case "sel", _: result = choice(a.args[0], [(values, maximum(arm, b)) for values, arm in a.args[1]])
            case _, "sel": result = maximum(b, a)
            case _: result = bitvec.const(max(bitvec.value(a), bitvec.value(b)), 32)
        maxima[a, b] = maxima[b, a] = result
        return result

    def union(maps):
        result = {}
        for demands in maps:
            for index, bits in demands.items(): result[index] = maximum(result.get(index, zero), bits)
        return result

    def visit(expression, bits):
        bits = min(bits, expression.w)
        key = expression, bits
        if key in memo: return memo[key]
        arguments = expression.args
        if not bits: result = {}
        else:
            match expression.op:
                case "temp": result = {arguments[0]: bitvec.const(bits, 32)}
                case "sel":
                    arms = [(values, visit(arm, bits)) for values, arm in arguments[1]]
                    result = {index: choice(arguments[0], [(values, demands.get(index, zero))
                                                          for values, demands in arms])
                              for index in set().union(*(demands.keys() for _, demands in arms))}
                case "extract": result = visit(arguments[0], bits + arguments[2])
                case "zext" | "sext" | "not" | "neg": result = visit(arguments[0], bits)
                case "add" | "sub" | "mul" | "and" | "or" | "xor":
                    result = union(visit(argument, bits) for argument in arguments)
                case "concat":
                    offset, parts = 0, []
                    for argument in reversed(arguments):
                        parts.append(visit(argument, max(0, bits - offset)))
                        offset += argument.w
                    result = union(parts)
                case "shl" if bitvec.is_const(arguments[1]):
                    result = visit(arguments[0], max(0, bits - bitvec.value(arguments[1])))
                case "shl":
                    result = union((visit(arguments[0], bits), visit(arguments[1], arguments[1].w)))
                case "shr" | "sar" if bitvec.is_const(arguments[1]):
                    result = visit(arguments[0], bits + bitvec.value(arguments[1]))
                case _:
                    result = union(visit(argument, argument.w) for argument in arguments if isinstance(argument, bitvec.Expr))
        memo[key] = result
        return result

    return union(visit(root, root.w) for root in roots)


def selected_prefix(source, demand):
    if demand.op == "sel":
        return lower.Merger().choice(demand.args[0], {
            values: selected_prefix(source, arm) for values, arm in demand.args[1]}, source.w)
    bits = min(source.w, 8 * lower.nbytes(bitvec.value(demand)))
    return bitvec.resize(bitvec.extract(source, bits - 1, 0), source.w) if bits else bitvec.const(0, source.w)


def operator_nodes(roots, operators):
    seen, pending = set(), list(roots)
    while pending:
        expression = pending.pop()
        if not isinstance(expression, bitvec.Expr) or expression in seen: continue
        seen.add(expression)
        if expression.op in operators:
            yield expression
        else:
            pending.extend([arm for _, arm in expression.args[1]] if expression.op == "sel" else expression.args)


def shared_temporaries(roots):
    return {ordered.temp_index(temporary) for expression in operator_nodes(roots, lower.SHARED | lower.SCRATCH)
            for temporary in bitvec.leaves(expression, "temp")}


class Lower(lower.Lower):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.aliases = {}
        self.expanded, self.lowered = {}, {}

    def expand(self, expression):
        def leaf(node):
            if node.op == "sel":
                if min(lower.operations(arm) for _, arm in node.args[1]) > 32:
                    return node
                return lower.sel(node.args[0], [(values, self.expand(arm)) for values, arm in node.args[1]], node.w)
            if node.op == "temp" and isinstance(source := self.aliases.get(node.args[0]), bitvec.Expr):
                return self.expand(source)
        return bitvec.substitute(expression, leaf, self.expanded)

    def value(self, expression):
        return super().value(bitvec.lowered(self.expand(expression), self.lowered, preserve=lower.SHARED | lower.SCRATCH))

    def param(self, name, width):
        value = super().param(name, width)
        return lower.expr(f"&:{lower.nbytes(width)} {value.text}", value) if value.kind == "target" else value

    def target(self, expression):
        expression = self.expand(expression)
        if expression.op == "param" and expression.args[0].startswith("__target:"):
            return super().param(expression.args[0], expression.w)
        return self.value(expression)

    def folded(self, expression):
        if expression.op == "temp": return None
        if not bitvec.leaves(expression, "sel"): return super().folded(expression)
        budget = lower.MAX_BODY_SPLIT
        def choices(tree):
            nonlocal budget
            try:
                lower.Fold(self.b).text(tree)
            except lower.Fold.No:
                selectors = bitvec.leaves(tree, "sel")
                if not selectors: raise
                selector = selectors[0]
                arms = [(values, choices(bitvec.substitute(tree, lambda node: arm if node is selector else None)))
                        for values, arm in selector.args[1]]
                return lower.sel(selector.args[0], arms, tree.w)
            budget -= 1
            if budget < 0: raise lower.Fold.No
            return tree
        try:
            decoded = choices(expression)
        except lower.Fold.No:
            return super().folded(expression)
        return self.sel(decoded) if decoded.op == "sel" else super().folded(decoded)

    def lower(self, expression):
        if expression.op == "temp":
            index = expression.args[0]
            if index in self.aliases:
                source = self.aliases[index]
                return self.value(source) if isinstance(source, bitvec.Expr) else source
            return lower.Operand(f"q{index}", "local")
        if expression.op in lower.SHARED: return self.scratch_call(expression)
        return super().lower(expression)

    def sel(self, expression):
        arms = [arm for _, arm in expression.args[1]]
        if all(arm is arms[0] for arm in arms): return self.value(arms[0])
        temporaries = bitvec.leaves(expression, "temp")
        if not temporaries: return super().sel(expression)
        demands = register_demands((expression,))
        replacements = {}
        for index, temporary in enumerate(temporaries):
            file = f"ORDEREDARG{lower.nbytes(temporary.w)}_"
            symbol = file + str(index)
            self.b.g.temporaries[symbol] = lower.nbytes(temporary.w)
            argument = self.expand(temporary)
            if not bitvec.leaves(argument, "temp"):
                argument = selected_prefix(argument, demands.get(ordered.temp_index(temporary), bitvec.const(0, 32)))
            source = self.value(argument)
            self.code.add(f"{symbol} = {source.text};", source)
            replacements[temporary] = bitvec.reg(file, index, 1, temporary.w)

        def substitute(tree):
            def leaf(node):
                if node.op == "sel":
                    keys, cases = node.args
                    return lower.sel(keys, [(values, substitute(arm)) for values, arm in cases], node.w)
                return replacements.get(node)
            return bitvec.substitute(tree, leaf)

        result = super().sel(substitute(expression))
        if ("sub", result.text) not in result.refs: return result
        return self.local(expression.w, self.build_call(result.text, export=True))


def emit(body, builder):
    types = ordered.validate(body, ordered.parameter_types(body))
    statements = prune(ordered.linearize(body))
    labels = {target for position, statement in enumerate(statements) for target in (
        tuple(target for target in (statement.yes, statement.no) if target != position + 1) if isinstance(statement, ordered.Branch) else
        (statement.target,) if isinstance(statement, ordered.Jump) else ())}
    code = lower.Code()
    roots = [bitvec.lowered(value, preserve=lower.SHARED | lower.SCRATCH) for statement in statements
             for value in ordered.statement_values(statement) if isinstance(value, bitvec.Expr)]
    low = Lower(builder, code, lower.count_uses(roots))
    shared_inputs = shared_temporaries(roots)
    definitions, uses = {}, {}
    for position, statement in enumerate(statements):
        targets = (statement.target,) if isinstance(statement, (ordered.Move, ordered.ReadRegister, ordered.ReadMemory)) else \
                  statement.results if isinstance(statement, ordered.Special) else ()
        for target in targets:
            index = ordered.temp_index(target)
            definitions[index] = definitions.get(index, 0) + 1
        for expression in ordered.statement_values(statement):
            indices = (expression.index,) if isinstance(expression, ordered.Handle) else \
                      (ordered.temp_index(temporary) for temporary in bitvec.leaves(expression, "temp"))
            for index in indices: uses.setdefault(index, set()).add(position)
    adjacent = {ordered.temp_index(statement.target) for position, statement in enumerate(statements)
                if isinstance(statement, (ordered.Move, ordered.ReadRegister)) and
                isinstance(statement.target, bitvec.Expr) and
                definitions[ordered.temp_index(statement.target)] == 1 and
                uses.get(ordered.temp_index(statement.target)) == {position + 1} and
                (not isinstance(statement, ordered.ReadRegister) or not isinstance(statements[position + 1], ordered.Move))}
    adjacent.difference_update(ordered.temp_index(statement.target) for statement in statements
                               if isinstance(statement, ordered.ReadRegister) and ordered.temp_index(statement.target) in shared_inputs)
    low.aliases = {ordered.temp_index(statement.target): statement.value for statement in statements
                   if isinstance(statement, ordered.Move) and isinstance(statement.value, bitvec.Expr)
                   and (statement.value.op in ("const", "param", "temp") or ordered.temp_index(statement.target) in adjacent)
                   and definitions[ordered.temp_index(statement.target)] == 1}
    def consumers(index):
        return {consumer for position in uses.get(index, ())
                for consumer in (consumers(ordered.temp_index(statements[position].target))
                                 if isinstance(statements[position], ordered.Move) and
                                 ordered.temp_index(statements[position].target) in low.aliases else {position})}

    for position, statement in enumerate(statements):
        if not isinstance(statement, ordered.ReadRegister): continue
        index = ordered.temp_index(statement.target)
        next_ = position + 1
        while next_ < len(statements) and isinstance(statements[next_], (
                ordered.Move, ordered.ReadRegister, ordered.ReadMemory, ordered.WriteMemory, ordered.Boundary)):
            next_ += 1
        positions = consumers(index)
        if index not in shared_inputs and definitions[index] == 1 and positions and all(position < use <= next_ for use in positions):
            adjacent.add(index)
    for statement in statements:
        if not isinstance(statement, ordered.ReadRegister) or ordered.temp_index(statement.target) not in adjacent: continue
        file, index, count = statement.location
        if file == "SR":
            adjacent.discard(ordered.temp_index(statement.target))
            continue
        low.aliases[ordered.temp_index(statement.target)] = (
            bitvec.param(index.name, statement.target.w) if isinstance(index, ordered.DecodedRegister) else
            bitvec.concat(*(bitvec.reg(file, index + k, 1, ordered.ELEMENT[file]) for k in reversed(range(count)))))
    expanded = [bitvec.lowered(low.expand(root), preserve=lower.SHARED | lower.SCRATCH) for root in roots]
    demands = register_demands(expanded)
    discarded = False
    for statement in statements:
        if not isinstance(statement, ordered.ReadRegister) or statement.location[0] == "SR": continue
        index = ordered.temp_index(statement.target)
        if bitvec.is_const(demand := demands.get(index), 0):
            low.aliases[index] = bitvec.const(0, statement.target.w)
            adjacent.add(index)
            discarded = True
    if discarded:
        low.expanded.clear()
        expanded = [bitvec.lowered(low.expand(root), preserve=lower.SHARED | lower.SCRATCH) for root in roots]
    low.uses = lower.count_uses(expanded)
    discarded_results = {ordered.temp_index(target) for statement in statements if isinstance(statement, ordered.Special)
                         for target in statement.results if ordered.temp_index(target) not in uses}
    for index, type_ in types.items():
        if index not in definitions or index in low.aliases or index in adjacent or index in discarded_results: continue
        width = type_ if isinstance(type_, int) else 64
        code.add(f"local q{index}:{lower.nbytes(width)};")

    def value(expr):
        if isinstance(expr, ordered.Handle): return lower.Operand(f"q{expr.index}", "local")
        return low.value(bitvec.lowered(expr, preserve=lower.SHARED | lower.SCRATCH))

    def call(name, arguments=(), result=None):
        builder.g.pcodeops.add(name)
        text = f"{name}({', '.join(arg.text for arg in arguments)});"
        code.add(text if result is None else f"{result} = {text}", *arguments)

    def register(location):
        file, index, count = location
        if isinstance(index, ordered.DecodedRegister): return low.param(index.name, count * ordered.ELEMENT[file])
        if file == "SR":
            builder.g.special_spans.add(count)
            return lower.Operand(builder.g.special_name(index, count), "name")
        if count == 1: return lower.Operand(f"{file}{index}", "name")
        expression = bitvec.concat(*(bitvec.reg(file, index + k, 1, ordered.ELEMENT[file]) for k in reversed(range(count))))
        return value(expression)

    for position, statement in enumerate(statements):
        if position in labels or isinstance(statement, (ordered.Branch, ordered.Jump, ordered.ReadRegister)):
            low.memo = [{}]
        if position in labels: code.add(f"<ordered_{position}>")
        match statement:
            case ordered.Move(target, expr):
                if ordered.temp_index(target) in low.aliases: continue
                source = value(expr)
                code.add(f"q{ordered.temp_index(target)} = {source.text};", source)
            case ordered.ReadRegister(target, location):
                if ordered.temp_index(target) in adjacent: continue
                file, index, count = location
                if file == "SR":
                    source = low.clean(target.w, register(location))
                else:
                    expression = bitvec.param(index.name, target.w) if isinstance(index, ordered.DecodedRegister) else \
                        bitvec.concat(*(bitvec.reg(file, index + k, 1, ordered.ELEMENT[file]) for k in reversed(range(count))))
                    source = low.clean(target.w, value(selected_prefix(
                        expression, demands.get(ordered.temp_index(target), bitvec.const(target.w, 32)))))
                code.add(f"q{ordered.temp_index(target)} = {source.text};", source)
            case ordered.WriteRegister(location, expr):
                file, index, count = location
                source = value(expr)
                if count > 1: source = low.named(source, expr.w)
                if isinstance(index, ordered.DecodedRegister):
                    symbol = builder.outputs[index.name].symbol
                    ref = builder.ref(symbol)
                    destination = lower.Operand(symbol, "name", frozenset([ref] if ref else []))
                    code.add(f"{symbol} = {source.text};", destination, source)
                    continue
                for k in range(count):
                    element = ordered.ELEMENT[file]
                    slice_ = source if count == 1 else low.truncated(
                        lower.expr(f"{source} >> {k * element}", source), expr.w, lower.nbytes(element))
                    code.add(f"{file}{index + k} = {slice_.text};", slice_)
            case ordered.ReadMemory(target, space, address):
                addr = low.named(value(address), 64)
                code.add(f"q{ordered.temp_index(target)} = *[{space}]:{lower.nbytes(target.w)} {addr.text};", addr)
            case ordered.WriteMemory(space, address, expr):
                addr, source = low.named(value(address), 64), value(expr)
                code.add(f"*[{space}]:{lower.nbytes(expr.w)} {addr.text} = {source.text};", addr, source)
            case ordered.Special(spec, arguments, results):
                arguments = list(map(value, arguments))
                if len(results) <= 1:
                    output = f"q{ordered.temp_index(results[0])}" if results and ordered.temp_index(results[0]) in uses else None
                    call(spec.runtime or PREFIX + spec.name, arguments, output)
                else:
                    record = low.label("record")
                    code.add(f"local {record}:8;")
                    call(PREFIX + spec.name, arguments, record)
                    for index, target in enumerate(results):
                        if ordered.temp_index(target) not in uses: continue
                        type_ = ordered.value_type(target)
                        width = type_ if isinstance(type_, int) else 64
                        call(PREFIX + f"result_{width}", [lower.Operand(record, "local"), lower.Operand(f"{index}:4", "const")],
                             f"q{ordered.temp_index(target)}")
                    call(PREFIX + "release_record", [lower.Operand(record, "local")])
            case ordered.Branch(condition, yes, no):
                cond = value(condition)
                if yes == position + 1:
                    code.add(f"if (!({cond.text})) goto <ordered_{no}>;", cond)
                elif no == position + 1:
                    code.add(f"if ({cond.text}) goto <ordered_{yes}>;", cond)
                else:
                    code.add(f"if ({cond.text}) goto <ordered_{yes}>; goto <ordered_{no}>;", cond)
            case ordered.Jump(target): code.add(f"goto <ordered_{target}>;")
            case ordered.Boundary(enter): call(ATOMIC_ENTER if enter else ATOMIC_EXIT)
            case ordered.Transfer(kind, target):
                destination = value(target) if kind == "return" else low.target(target)
                if destination.kind != "target": destination = low.named(destination, 64)
                lower.lower_flow(low, kind, destination, None, True)
    if len(statements) in labels: code.add(f"<ordered_{len(statements)}>")
    return code


@dataclass(frozen=True)
class Choice:
    keys: tuple
    cases: tuple


def normalize(body, *, keep_shared=True):
    ordered.validate(body, ordered.parameter_types(body))
    uses = Counter(temporary for statement in ordered.linearize(body)
                   for expression in ordered.statement_values(statement) if isinstance(expression, bitvec.Expr)
                   for temporary in bitvec.leaves(expression, "temp"))
    def block(body, aliases):
        def value(expression):
            if not isinstance(expression, bitvec.Expr): return expression
            def leaf(node):
                if node.op == "sel":
                    return lower.sel(node.args[0], [(selections, value(arm)) for selections, arm in node.args[1]], node.w)
                return aliases.get(node)
            return bitvec.substitute(expression, leaf)
        statements = []
        for statement in body.statements:
            match statement:
                case ordered.Move(target, expression) if isinstance(target, bitvec.Expr):
                    expression = value(expression)
                    if keep_shared and uses[target] > 1 and expression.op not in ("const", "param", "temp"):
                        statements.append(ordered.Move(target, expression))
                    else: aliases[target] = expression
                case ordered.If(condition, yes, no, results):
                    condition, yes, no = value(condition), block(yes, dict(aliases)), block(no, dict(aliases))
                    if not yes.statements and not no.statements and all(isinstance(target, bitvec.Expr) for target in results):
                        for target, a, b in zip(results, yes.results, no.results):
                            aliases[target] = bitvec.ite(condition, a, b)
                    else:
                        statements.append(ordered.If(condition, yes, no, results))
                case ordered.Atomic(inner):
                    statements.append(ordered.Atomic(block(inner, dict(aliases))))
                case _:
                    statements.extend(ordered.map_values(ordered.Block((statement,)), value).statements)
        return ordered.Block(tuple(statements), tuple(map(value, body.results)))

    result = block(body, {})
    indices = {}
    for statement in ordered.linearize(result):
        targets = (statement.target,) if isinstance(statement, (ordered.Move, ordered.ReadRegister, ordered.ReadMemory)) else \
                  statement.results if isinstance(statement, ordered.Special) else ()
        for target in targets: indices.setdefault(ordered.temp_index(target), len(indices))
    def rename(value):
        if isinstance(value, ordered.Handle): return ordered.Handle(indices[value.index], value.type)
        def leaf(node):
            if node.op == "temp": return ordered.temp(indices[node.args[0]], node.w)
            if node.op == "sel":
                return lower.sel(node.args[0], [(selections, rename(arm)) for selections, arm in node.args[1]], node.w)
        return bitvec.substitute(value, leaf)
    return ordered.map_values(result, rename)


def structure(value, *, values=False):
    if isinstance(value, bitvec.Expr):
        return bitvec.identity(value) if values else ("value", value.w)
    visit = (lambda value: structure(value, values=True)) if values else structure
    if isinstance(value, tuple): return tuple(map(visit, value))
    if is_dataclass(value):
        bindings = {"target"} if isinstance(value, (ordered.Move, ordered.ReadRegister, ordered.ReadMemory)) else \
                   {"results"} if isinstance(value, (ordered.Special, ordered.If)) else set()
        def binding(value):
            if isinstance(value, bitvec.Expr): return "temp", value.w, ordered.temp_index(value)
            if isinstance(value, tuple): return tuple(map(binding, value))
            return visit(value)
        return type(value), tuple((binding if field.name in bindings else visit)(getattr(value, field.name))
                                  for field in fields(value))
    return value


def common_prefix(bodies):
    if any(not isinstance(body, ordered.Block) or body.results for body in bodies): return 0
    signatures = [tuple(structure(statement, values=True) for statement in body.statements) for body in bodies]
    count, limit = 0, min(map(len, signatures))
    while count < limit and all(body[count] == signatures[0][count] for body in signatures):
        count += 1
    def definitions(statements):
        return {ordered.temp_index(target) for statement in ordered.linearize(ordered.Block(statements))
                for target in ((statement.target,) if isinstance(statement, (
                    ordered.Move, ordered.ReadRegister, ordered.ReadMemory)) else
                               statement.results if isinstance(statement, ordered.Special) else ())}
    def uses(statements):
        return {ordered.temp_index(value)
                for statement in ordered.linearize(ordered.Block(statements))
                for expression in ordered.statement_values(statement)
                for value in ((expression,) if isinstance(expression, ordered.Handle) else
                              bitvec.leaves(expression, "temp"))}
    while count and definitions(bodies[0].statements[:count]) & set().union(
            *(uses(body.statements[count:]) for body in bodies)):
        count -= 1
    return count


def merge(rows):
    return merge_normalized([(assignment, normalize(body)) for assignment, body in rows])


def merge_normalized(rows):
    assignments, bodies = map(list, zip(*rows))
    def shape(body):
        roots = [bitvec.lowered(value, preserve=lower.SHARED | lower.SCRATCH)
                 for statement in ordered.linearize(body)
                 for value in ordered.statement_values(statement) if isinstance(value, bitvec.Expr)]
        return structure(body), frozenset(shared_temporaries(roots))
    shapes = [shape(body) for body in bodies]
    if any(shape != shapes[0] for shape in shapes):
        keys = lower.determining_keys(shapes, assignments)
        key = min(keys, key=lambda k: (len({a.get(k) for a in assignments}), repr(k)))
        values = sorted({a[key] for a in assignments if key in a}, key=repr)
        if len(keys) == 1:
            groups = {}
            for value in values:
                selected = next(shape(body) for a, body in rows if a.get(key, value) == value)
                groups.setdefault(selected, set()).add(value)
            return Choice((key,), tuple(((next(iter(values)) if len(values) == 1 else frozenset(values),),
                                         merge_normalized([(a, body) for a, body in rows if key not in a or a[key] in values]))
                                        for values in groups.values()))
        return Choice((key,), tuple(((value,), merge_normalized([(a, body) for a, body in rows if a.get(key, value) == value]))
                                   for value in values))
    merger = lower.Merger()

    def combine(values, alternatives=False):
        first = values[0]
        if isinstance(first, bitvec.Expr):
            merged = merger.merge(values, assignments)
            floating = {"fadd", "fmul", "ffma", "fconv", "flt", "fle", "feq", "fnan", "nanresult"}
            if not alternatives or not bitvec.leaves(merged, "sel") or next(
                    operator_nodes((merged,), lower.SHARED | lower.SCRATCH | floating), None) is not None:
                return merged
            trees = [bitvec.Expr("tuple", (value,), 0) for value in values]
            def unpack(tree):
                if tree.op == "tuple": return tree.args[0]
                return lower.sel(tree.args[0], [(selections, unpack(arm)) for selections, arm in tree.args[1]], first.w)
            return unpack(merger.bodies(trees, assignments))
        if isinstance(first, tuple): return tuple(combine([value[index] for value in values], alternatives) for index in range(len(first)))
        if is_dataclass(first):
            return replace(first, **{field.name: combine([getattr(value, field.name) for value in values], alternatives)
                                     for field in fields(first)})
        return first
    baseline, candidate = combine(bodies), combine(bodies, True)
    if baseline == candidate: return baseline
    def tree(body):
        return bitvec.Expr("tuple", tuple(value for statement in ordered.linearize(body)
                                        for value in ordered.statement_values(statement)
                                        if isinstance(value, bitvec.Expr)), 0)
    def operations(expression, assignment):
        seen = set()
        def visit(value):
            if not isinstance(value, bitvec.Expr) or value in seen or value.op in ("const", "reg", "temp", "param"):
                return 0
            seen.add(value)
            if value.op == "sel":
                keys, cases = value.args
                matches = [arm for selections, arm in cases if all(
                    selection is None or key not in assignment or
                    (assignment[key] in selection if isinstance(selection, frozenset) else assignment[key] == selection)
                    for key, selection in zip(keys, selections))]
                if not matches: return float("inf")
                if all(key in assignment for key in keys): matches = matches[:1]
                return 1 + max(operations(arm, assignment) for arm in matches)
            return (value.op != "tuple") + sum(visit(argument) for argument in value.args)
        return visit(expression)
    before, after = tree(baseline), tree(candidate)
    improved = False
    for assignment in assignments:
        old, new = operations(before, assignment), operations(after, assignment)
        if old == float("inf") or new > old: return baseline
        improved |= new < old
    return candidate if improved else baseline


def emit_choice(body, builder, context=()):
    if isinstance(body, ordered.Block): return emit(body, builder)
    count = common_prefix([arm for _, arm in body.cases])
    prefix = emit(ordered.Block(body.cases[0][1].statements[:count]), builder) if count else lower.Code()
    constructors = []
    for values, arm in body.cases:
        clauses = [clause for key, value in zip(body.keys, values) if value is not None
                   for clause in lower.clauses(builder, key, value)]
        if count: arm = ordered.Block(arm.statements[count:])
        code = emit_choice(arm, builder, context + tuple(clauses))
        constructors.append(('""', code.pattern(clauses), "", code.body()))
    table = builder.g.subtable("body", constructors)
    builder.refs.append(("sub", table))
    code = prefix
    code.lines.append(f"build {table};")
    code.refs.add(("sub", table))
    return code


def emit_rows(builder, rows):
    from sass.semantics.family import primitive_names
    from sass.semantics.operations import dispatch
    native = [(assignment, body) for assignment, body in rows if not isinstance(body, Exception)]
    normalized = [(assignment, normalize(body)) for assignment, body in native]
    body = merge_normalized(normalized)
    optional = sorted({index.name for _, row in normalized for statement in ordered.linearize(row)
                       if isinstance(statement, ordered.WriteRegister)
                       for file, index, _ in (statement.location,)
                       if file in ("P", "UP") and isinstance(index, ordered.DecodedRegister)
                       and not bitvec.is_const(statement.value)
                       and lower.clauses(builder, ("zero", index.name), True) not in (None, [])})
    def inexpensive(body):
        body = normalize(body, keep_shared=False)
        writes = [statement for statement in ordered.linearize(body) if isinstance(statement, ordered.WriteRegister)]
        full = [statement.value for statement in writes]
        kept = [statement.value for statement in writes if not (
            isinstance(statement.location[1], ordered.DecodedRegister) and statement.location[1].name in optional)]
        inputs = lambda values: set().union(*(set(bitvec.leaves(value, "temp")) for value in values))
        return bool(kept) and inputs(full) <= inputs(kept) and \
            lower.operations(bitvec.Expr("tuple", tuple(full), 0)) - \
            lower.operations(bitvec.Expr("tuple", tuple(bitvec.narrow(kept)), 0)) <= 3
    if optional and all(inexpensive(body) for _, body in normalized): optional = []
    code = emit_choice(body, builder)
    if optional:
        from sass.semantics.family import family_for
        dropped_rows = lower.enumerate_rows(builder, family_for(builder.k), {("zero", name): True for name in optional})
        dropped = emit_choice(merge([(assignment, body) for assignment, body in dropped_rows
                                     if not isinstance(body, Exception)]), builder)
        conditions = [clause for name in optional for clause in lower.clauses(builder, ("zero", name), True)]
        table = builder.g.subtable("live", [('""', result.pattern(clauses), "", result.body())
                                          for clauses, result in ((conditions, dropped), ([], code))])
        builder.refs.append(("sub", table))
        code = lower.Code()
        code.lines.append(f"build {table};")
        code.refs.add(("sub", table))
    builder.body += code.lines
    lower.guard(builder, rows, [not isinstance(body, Exception) for _, body in rows])
    dispatch(builder, refs=sorted(code.refs, key=repr))
    primitives = [primitive_names(body) for _, body in native]
    status = "primitive" if all(primitives) else "native"
    return status, "ordered semantics family"
