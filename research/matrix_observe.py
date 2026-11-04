import argparse
import json
from pathlib import Path

from sass.assemble import encoding
from sass.decode import pydecode
from sass.sleigh import modifiers
from tests.harness import autocases, device_cases


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    word = bytes.fromhex(encoding.encode("SM89", "hmma_x8_", size="16816", srcfmt="F16", dstfmt="F32",
                                        Ra=16, Rb=24, Rc=32, Rd=40))
    form = modifiers.settings("SM89", pydecode.decode("SM89", word))
    cases = autocases.generate("SM89", "hmma_x8_", form, sample=100, discovery=True)
    data = device_cases.observe(cases, arch="SM89")
    for case, observation in zip(cases, data["observations"]):
        observation.update(pattern=case.hardware["pattern"], lane=case.request["context"]["warp"]["lane"])
    a.out.write_text(json.dumps(data, indent=1) + "\n")
    print(len(data["observations"]), "observations;", sum("error" in r for r in data["observations"]), "errors")


if __name__ == "__main__":
    main()
