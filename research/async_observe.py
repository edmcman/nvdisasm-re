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
    p.add_argument("--cls", default="ldgsts__RR64U")
    p.add_argument("--width", nargs="+", type=int, default=(32, 64, 128))
    a = p.parse_args()
    cases = []
    for width in a.width:
        for fill in ("nofillctrl", "ZFILL"):
            for inverse in (0, 1):
                fields = dict(Rb_URc=12, Ra_URd=13) if a.cls == "ldgsts_memdesc_" else {}
                word = bytes.fromhex(encoding.encode("SM89", a.cls, sz=str(width), e="E",
                                                     fc=fill, **fields, **{"Pnz@not": inverse}))
                form = modifiers.settings("SM89", pydecode.decode("SM89", word))
                for case in autocases.generate("SM89", a.cls, form, sample=100, discovery=True):
                    memory = case.request["memory"]
                    if int.from_bytes(bytes.fromhex(memory[0]["hex"]), "little") != 0x12345678: continue
                    request = dict(case.request, observe=["P0"], observe_memory=[
                        dict(space="shared", address=0x100, size=width // 8)])
                    cases.append(case._replace(request=request, hardware={"adapter": "async"}))
    data = device_cases.observe(cases, arch="SM89")
    for case, observation in zip(cases, data["observations"]):
        observation["input_predicate"] = case.request["registers"]["P0"]
        observation["inputs"] = case.request["registers"]
    a.out.write_text(json.dumps(data, indent=1) + "\n")
    print(len(data["observations"]), "observations;", sum("error" in row for row in data["observations"]), "errors")


if __name__ == "__main__":
    main()
