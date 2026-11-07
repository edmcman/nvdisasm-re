import argparse
import json
from pathlib import Path

from sass.assemble import encoding
from sass.decode import ir, pydecode
from sass.sleigh import modifiers
from tests.harness import autocases, device_cases


def retain_inputs(data, cases):
    contexts, indices = [], {}
    for case, observation in zip(cases, data["observations"]):
        context = case.request["context"]
        context = context | {"warp": {name: value for name, value in context["warp"].items() if name != "lane"}}
        key = json.dumps(context, sort_keys=True)
        if key not in indices:
            indices[key] = len(contexts)
            contexts.append(context)
        observation.update(context=indices[key], input_registers=case.request["registers"])
    data["contexts"] = contexts


def check_capture(data):
    from tests.refs import matrix
    failures = []
    for row in data["observations"]:
        if "error" in row:
            failures.append(row)
            continue
        decoded = pydecode.decode(data["arch"], bytes.fromhex(row["word"]))
        env, registers = decoded.env, row["input_registers"]
        def predicate(prefix, name):
            index = env.get(name, 7)
            return bool(True if index == 7 else registers.get(f"{prefix}{index}", 0)) ^ bool(env.get((name, "not"), 0))
        destination = env["Rd"]
        if destination == 255 or not predicate("P", "Pg"):
            expected = {name: registers.get(name, 0) for name in row["registers"]}
        else:
            form = modifiers.settings(data["arch"], decoded)
            context = data["contexts"][row["context"]]
            values = matrix.hmma(
                context["warp"]["operand_groups"], row["lane"], data["arch"], data["source"],
                int(data["size"][3:]), predicate("UP", "UPp"),
                tuple(form.get(name + "@negate") == "set" for name in ("Ra", "Rb")), data["destination"])
            expected = {f"R{destination + j}": value for j, value in enumerate(values)}
        if expected != row["registers"]:
            failures.append(dict(id=row["id"], expected=expected, actual=row["registers"]))
    print(len(data["observations"]), "observations;", len(failures), "mismatches")
    if failures:
        print(json.dumps(failures[:5], indent=1))
        raise SystemExit(1)


def main():
    p = argparse.ArgumentParser()
    action = p.add_mutually_exclusive_group(required=True)
    action.add_argument("--out", type=Path)
    action.add_argument("--check-capture", type=Path)
    p.add_argument("--retain-inputs", action="store_true")
    p.add_argument("--source", choices=("F16", "BF16", "E6M9", "TF32"), default="F16")
    p.add_argument("--destination", choices=("F16", "F32"), default="F32")
    p.add_argument("--size", choices=("1684", "1688", "16816"), default="16816")
    p.add_argument("--pattern", action="append")
    p.add_argument("--predicate", choices=("true", "false"))
    p.add_argument("--invert", action="store_true")
    p.add_argument("--negate", action="append", choices=("Ra", "Rb"), default=[])
    a = p.parse_args()
    if a.check_capture:
        check_capture(json.loads(a.check_capture.read_text()))
        return
    word = bytes.fromhex(encoding.encode("SM89", "hmma_x8_", size=a.size, srcfmt=a.source, dstfmt=a.destination,
                                        Ra=16, Rb=24, Rc=32, Rd=40))
    form = modifiers.settings("SM89", pydecode.decode("SM89", word))
    cases = autocases.generate("SM89", "hmma_x8_", form, sample=100, discovery=True)
    if a.pattern:
        cases = [case for case in cases if case.hardware["pattern"] in a.pattern]
    if a.predicate is not None or a.negate:
        for case in cases:
            decoded = pydecode.decode("SM89", bytes.fromhex(case.request["word"]))
            fields = {(name, "negate"): 1 for name in a.negate}
            if a.predicate is not None:
                fields.update({"UPp": 0, ("UPp", "not"): int(a.invert)})
                case.request["registers"]["UP0"] = int(a.predicate == "true")
            case.request["word"] = encoding.encode_environment(
                ir.load("SM89"), decoded.klass, fields, base=decoded.env)[0].hex()
    data = device_cases.observe(cases, arch="SM89")
    data.update(source=a.source, destination=a.destination, size=a.size,
                predicate=a.predicate, inverted=a.invert, negate=a.negate)
    for case, observation in zip(cases, data["observations"]):
        observation.update(pattern=case.hardware["pattern"], lane=case.request["context"]["warp"]["lane"])
    if a.retain_inputs: retain_inputs(data, cases)
    a.out.write_text(json.dumps(data, indent=1) + "\n")
    print(len(data["observations"]), "observations;", sum("error" in r for r in data["observations"]), "errors")


if __name__ == "__main__":
    main()
