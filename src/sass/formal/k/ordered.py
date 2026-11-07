from dataclasses import dataclass
from sass.formal import k
from sass.semantics import bitvec, ordered


@dataclass(frozen=True)
class Export:
    source: str
    program: str
    operations: tuple
    contracts: tuple = ()

    def manifest(self):
        from dataclasses import asdict
        return dict(format=2, k_version=k.VERSION, operations=[asdict(spec) for spec in self.operations],
                    contracts=[asdict(contract) for contract in self.contracts])

    def response(self, response, environment):
        if any(contract.name == response["name"] for contract in self.contracts):
            return k.response_term(response, environment)
        name, values = response["name"], response["values"]
        spec = next((spec for spec in self.operations if spec.name == name), None)
        if spec is None: raise k.ExportError("response names an absent operation")
        widths = lambda types: [type_ if isinstance(type_, int) else 64 for type_ in types]
        for supplied, types in ((response["arguments"], spec.arguments), (values, spec.results)):
            if len(supplied) != len(types) or any(type(value) is not int or not 0 <= value < 1 << width
                                                for value, width in zip(supplied, widths(types))):
                raise k.ExportError("runtime response outside its declared types")
        return (f"orderedResponse({k.string(name)}, {k.klist(map(str, response['arguments']))}, "
                f"{k.klist(map(str, widths(spec.results)))}, {k.klist(map(str, values))}, "
                f"{response.get('events', '.List')}, {response.get('before', environment or '.Map')}, "
                f"{response.get('environment', environment or '.Map')})")


class Encoder:
    def __init__(self, pure=None):
        self.pure = pure or k.Encoder()
        self.operations = {}

    def expression(self, expression):
        if isinstance(expression, ordered.Handle): expression = bitvec.Expr("temp", (expression.index,), 64)
        if expression in self.pure.nodes: return self.pure.nodes[expression][1]
        if isinstance(expression, bitvec.Expr) and expression.op == "temp":
            ident = -len(self.pure.nodes) - 1
            self.pure.nodes[expression] = ident, f'node({ident}, "temp", {expression.w}, ListItem({expression.args[0]}))'
            return self.pure.nodes[expression][1]
        if not isinstance(expression, bitvec.Expr): raise k.ExportError("expected an expression")
        if expression.op in bitvec.DEFINED:
            expression = bitvec.lowered(expression, self.pure.lowering,
                                        lambda _, node: self.pure.definitions.add(node.op))
        if expression in self.pure.nodes: return self.pure.nodes[expression][1]
        if not bitvec.leaves(expression, "temp"): return self.pure.expr(expression)
        if expression.op == "mufu":
            name, *values = expression.args
            self.pure.contracts.add(k.Contract(bitvec.PRIMITIVE_OP + "mufu_" + name.lower(),
                                               tuple(value.w for value in values), expression.w, "pure",
                                               "hardware approximation"))
        arguments = [self.expression(arg) if isinstance(arg, bitvec.Expr) else
                     k.string(arg) if isinstance(arg, str) else str(arg) for arg in expression.args]
        ident = -len(self.pure.nodes) - 1
        text = f"node({ident}, {k.string(expression.op)}, {expression.w}, {k.klist(arguments)})"
        self.pure.nodes[expression] = ident, text
        return text

    def statement(self, statement, position):
        expr = self.expression
        match statement:
            case ordered.Move(target, value): return f"orderedMove({ordered.temp_index(target)}, {expr(value)})"
            case ordered.ReadRegister(target, (file, index, count)):
                return f"orderedRead({ordered.temp_index(target)}, {k.string(file)}, {index}, {count})"
            case ordered.WriteRegister((file, index, count), value):
                return f"orderedWrite({k.string(file)}, {index}, {count}, {expr(value)})"
            case ordered.ReadMemory(target, space, address):
                return f"orderedLoad({ordered.temp_index(target)}, {k.string(space)}, {expr(address)}, {target.w})"
            case ordered.WriteMemory(space, address, value):
                return f"orderedStore({k.string(space)}, {expr(address)}, {expr(value)})"
            case ordered.Special(spec, arguments, results):
                self.operations[spec.name] = spec
                arguments = tuple(map(expr, arguments))
                self.pure.runtime_sites.append(dict(id=position, name=spec.name, kind="operation",
                                                    arguments=arguments,
                                                    results=[type_ if isinstance(type_, int) else type_.name
                                                             for type_ in spec.results]))
                ids = k.klist(str(ordered.temp_index(target)) for target in results)
                widths = k.klist(str(type_) if isinstance(type_, int) else "64" for type_ in spec.results)
                return f"orderedSpecial({position}, {k.string(spec.name)}, {k.klist(arguments)}, {ids}, {widths})"
            case ordered.Branch(condition, yes, no): return f"orderedBranch({expr(condition)}, {yes}, {no})"
            case ordered.Jump(target): return f"orderedJump({target})"
            case ordered.Boundary(enter): return f"orderedBoundary({1 if enter else 0})"
            case ordered.Transfer(kind, target): return f"orderedControl({k.string(kind)}, {expr(target)})"
        raise k.ExportError(f"unsupported statement: {statement}")

    def instruction(self, body, address):
        try: ordered.validate(body, ordered.parameter_types(body))
        except TypeError as error: raise k.ExportError(str(error)) from error
        statements = [(str(index), self.statement(statement, index))
                      for index, statement in enumerate(ordered.linearize(body))]
        return f"orderedInstruction({address}, absent, {k.kmap(statements)}, {len(statements)})"


def export(body, address=0x100000):
    if type(address) is not int or not 0 < address < 1 << 64 or address % 16:
        raise k.ExportError("instruction address must be aligned and nonzero")
    encoder = Encoder()
    instruction = encoder.instruction(body, address)
    program = f"program(({address} |-> {instruction}), {address})"
    source = ('requires "ordered.md"\n\nmodule SASS-ORDERED-EXPORTED\n  imports SASS-ORDERED\n'
              '  syntax Program ::= exportedProgram() [function]\n'
              f"  rule exportedProgram() => {program}\nendmodule\n")
    return Export(source, program, tuple(encoder.operations.values()), tuple(sorted(encoder.pure.contracts)))
