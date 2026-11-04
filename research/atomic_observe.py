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
    p.add_argument("--floating", action="store_true")
    p.add_argument("--safeadd", action="store_true")
    a = p.parse_args()
    cases = []
    forms = (("ADD", "F32.FTZ.RN"), ("ADD", "F64.RN"), ("ADD", "F16x2.RN")) if a.floating else (
        ("ADD", "32"), ("MIN", "S32"), ("MAX", "64"), ("INC", "32"),
        ("DEC", "32"), ("EXCH", "64"), ("CAS", "32"), ("CAS", "64"))
    if a.safeadd: forms = (("SAFEADD", "64"),)
    for op, size in forms:
        cls = "atomg_cas__RaNonRZ" if op == "CAS" else "atomg__RaNonRZ"
        fields = dict(e="E", sz=size) | ({} if op == "CAS" else dict(op=op))
        word = encoding.encode("SM89", cls, Ra=4, Rd=8, Rb=12, Pu=1, **fields)
        form = modifiers.settings("SM89", pydecode.decode("SM89", bytes.fromhex(word)))
        cases.extend(autocases.generate("SM89", cls, form, sample=100, discovery=True))
    data = device_cases.observe(cases, arch="SM89")
    a.out.write_text(json.dumps(data, indent=1) + "\n")
    print(len(data["observations"]), "observations;", sum("error" in r for r in data["observations"]), "errors")


if __name__ == "__main__":
    main()
