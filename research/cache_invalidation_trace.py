import argparse
import json
from pathlib import Path

from sass import paths
from sass.sleigh.build import ARCHES
from research.additional_opcodes import tool
from research.compiler_additional_opcodes import trace

QUALIFIERS = ("acquire.cta", "acquire.gpu", "acquire.sys",
              "relaxed.cta", "relaxed.gpu", "relaxed.sys", "volatile", "")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--sources", type=Path, required=True)
    args = parser.parse_args()
    template = (paths.ROOT / "research/additional-opcodes/control-acquire.ptx").read_text()
    probes = args.sources / "input"
    probes.mkdir(parents=True, exist_ok=True)
    for qualifier in QUALIFIERS:
        operation = f"ld.{qualifier}.global" if qualifier else "ld.global"
        (probes / f"{qualifier or 'default'}.ptx").write_text(
            "// sass: CCTL YIELD\n" + template.replace("ld.acquire.gpu.global", operation))
    rows = []
    for arch in ARCHES:
        rows.extend(trace(probe, arch, args.sources, "9.4", "SM110" if arch == "SM101" else None,
                          include_all=True)
                    for probe in sorted(probes.glob("*.ptx")))
        args.out.write_text(json.dumps(dict(compiler=tool(paths.PTXAS),
            kind="compiler encoding evidence; no functional verification",
            target_mapping={"SM101": "SM110"}, changed_operand="load semantics and scope",
            unchanged_producer="st.release.gpu.global.u32",
            probes=rows), indent=1) + "\n")
        print(arch, [(Path(row["source"]).stem, row["status"],
                      [(i["opcode"], i["cls"], i["settings"]) for i in row["instructions"]
                       if i["opcode"] in ("CCTL", "YIELD")])
                     for row in rows if row["architecture"] == arch], flush=True)


if __name__ == "__main__": main()
