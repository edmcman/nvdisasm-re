import argparse
import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path

from sass import paths
from sass.decode import cubin, pydecode
from sass.sleigh import modifiers
from sass.sleigh.build import ARCHES
from research.additional_opcodes import OPS, tool


def trace(probe, arch, directory):
    text = re.sub(r"(?m)^\.target\s+sm_\d+\w*", f".target sm_{arch[2:]}", probe.read_text())
    text = re.sub(r"(?m)^\.version\s+[\d.]+", ".version 9.0", text)
    source = directory / arch / probe.parent.name / probe.name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(text)
    wanted = sorted({opcode.split(".")[0] for line in re.findall(r"^// sass: (.+)$", text, re.M)
                     for opcode in line.split()} & set(OPS))
    row = dict(architecture=arch, source=str(source), source_sha256=hashlib.sha256(text.encode()).hexdigest(),
               requested_opcodes=wanted)
    with tempfile.TemporaryDirectory() as temporary:
        output = Path(temporary) / "probe.cubin"
        command = [paths.PTXAS, f"-arch=sm_{arch[2:]}", "-O3", str(source), "-o", str(output)]
        result = subprocess.run(command, capture_output=True, text=True)
        row.update(command=command, returncode=result.returncode, diagnostics=result.stderr)
        if result.returncode: return row | dict(status="compiler rejected", instructions=[])
        image = output.read_bytes()
        instructions = []
        for word in cubin.text_words(image):
            decoded = pydecode.decode(arch, word)
            if decoded.klass.mnemonic not in OPS: continue
            operands = {"@".join(key) if isinstance(key, tuple) else key: value
                        for key, value in decoded.env.items() if not isinstance(key, str) or not key.startswith("__")}
            instructions.append(dict(opcode=decoded.klass.mnemonic, cls=decoded.klass.name, word=word.hex(),
                                     settings=modifiers.settings(arch, decoded), operands=operands))
        return row | dict(status="compiled", image_sha256=hashlib.sha256(image).hexdigest(),
                          instructions=instructions,
                          missing_opcodes=sorted(set(wanted) - {i["opcode"] for i in instructions}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--sources", type=Path, required=True)
    args = parser.parse_args()
    probes = [paths.ROOT / "tests/ptx" / family / name for family, name in
              (("atomg", "integer.ptx"), ("b2r", "reductions.ptx"), ("cctl", "prefetch.ptx"),
               ("hmma", "dense.ptx"), ("hmma", "bf16.ptx"), ("hmma", "tf32_1684.ptx"), ("hmma", "tf32_1688.ptx"),
               ("ldgsts", "groups.ptx"), ("ldsm", "m8n8.ptx"))]
    data = dict(compiler=tool(paths.PTXAS), kind="compiler encoding evidence; no functional verification",
                probes=[trace(probe, arch, args.sources) for arch in ARCHES for probe in probes])
    args.out.write_text(json.dumps(data, indent=1) + "\n")
    for arch in ARCHES:
        rows = [row for row in data["probes"] if row["architecture"] == arch]
        print(arch, sum(row["status"] == "compiled" for row in rows), "/", len(rows), flush=True)


if __name__ == "__main__":
    main()
