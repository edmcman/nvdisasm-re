import argparse
import json
from pathlib import Path

from sass.assemble import encoding
from sass.decode import ir, pydecode
from sass.sleigh import modifiers
from tests.harness import autocases, device_cases


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--source", choices=("F16", "BF16", "TF32"), default="F16")
    p.add_argument("--destination", choices=("F16", "F32"), default="F32")
    p.add_argument("--size", choices=("1684", "1688", "16816"), default="16816")
    p.add_argument("--pattern", action="append")
    p.add_argument("--predicate", choices=("true", "false"))
    p.add_argument("--invert", action="store_true")
    p.add_argument("--negate", action="append", choices=("Ra", "Rb"), default=[])
    a = p.parse_args()
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
    a.out.write_text(json.dumps(data, indent=1) + "\n")
    print(len(data["observations"]), "observations;", sum("error" in r for r in data["observations"]), "errors")


if __name__ == "__main__":
    main()
