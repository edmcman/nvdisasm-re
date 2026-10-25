"""Synthetic SM89 LD.noe discovery, using the shared generated GPU wrappers.

Each scenario runs in a fresh process and CUDA context. Global targets live in
registered low-address host memory; observations never consult a reference.
Run: nice uv run python -m research.ld32 --out /tmp/ld32.json
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

from tests.harness import device_cases
from tests.harness.case import Cases
from tests.harness.gpu import Driver


SCENARIOS = ["plain", "positive_offset", "negative_offset", "overflow",
             "uniform", "uniform_overflow", "uniform_zero", "absolute",
             "U8", "S8", "U16", "S16", "64", "128", "alias", "discard", "zero_fill",
             "upper_half", "underflow"]


def observe(name):
    driver = Driver("SM89")
    try:
        data = bytes.fromhex("785634120200008003000000ffffffff")
        with driver.arena(bytes(256) + data, low=True,
                          address=0x90000000 if name in ("upper_half", "underflow") else None) as arena:
            target = arena.value + 256
            fields = dict(Rd=8, Ra=2, Ra_offset=0, e="noe", sz="32")
            registers = dict(R2=target, R3=0xdeadbeef, UR4=0, UR5=0xabcdef01)
            cls = "ld__sImmOffset"
            if name in ("positive_offset", "negative_offset"):
                offset = 16 if name == "positive_offset" else -16
                fields["Ra_offset"] = offset
                registers["R2"] = target - offset
            elif name == "overflow":
                registers["R2"] = 0xfffffff0
                fields["Ra_offset"] = target + 16
            elif name == "underflow":
                registers["R2"] = 0x10
                fields["Ra_offset"] = target - (1 << 32) - 0x10
            elif name.startswith("uniform"):
                cls = "ld_uniform__RaRZ" if name == "uniform_zero" else "ld_uniform__Ra32"
                fields.update(Ra="RZ" if name == "uniform_zero" else 2, Ra_URb=4)
                if name == "uniform_zero": registers.update(R2=0, UR4=target)
                elif name == "uniform_overflow": registers.update(R2=0xfffffffc, UR4=target + 4)
                else: registers.update(R2=0x100, UR4=target - 0xfc); fields["Ra_offset"] = -4
            elif name == "absolute":
                cls = "ld__uImmOffset"; fields.update(Ra="RZ", Ra_offset=target)
            elif name in ("U8", "S8", "U16", "S16", "64", "128"):
                fields["sz"] = name
            elif name == "alias": fields["Rd"] = 2
            elif name == "discard": fields["Rd"] = "RZ"
            elif name == "zero_fill": fields["Pnz"] = 0; registers.update(R2=1, P0=0)
            c = Cases("SM89")
            c.probe(cls, fields, registers, observe=["R2", "R3", "R8", "R9", "R10", "R11", "UR4", "UR5", "P0"])
            scenario = device_cases.Scenario("probe", c[0], "scalar", arch="SM89")
            result = dict(name=name, arch="SM89", device=driver.device, pointer=hex(target),
                          word=c[0].request["word"],
                          instruction=device_cases.decoded(c[0].request["word"]).text,
                          inputs=registers, fields=fields)
            try:
                raw = device_cases.execute(driver, device_cases.kernels([scenario], raw=True))[scenario.id]
                result.update(device_cases.observation(c[0], **raw))
            except RuntimeError as error:
                result["error"] = str(error)
            return result
    finally:
        driver.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--one", choices=SCENARIOS)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.one:
        print(json.dumps(observe(args.one)))
        return
    observations = []
    for name in SCENARIOS:
        child = subprocess.run([sys.executable, "-m", "research.ld32", "--one", name],
                               capture_output=True, text=True, timeout=30)
        observations.append(json.loads(child.stdout) if child.returncode == 0
                            else dict(name=name, error=child.stderr))
    text = json.dumps(observations, indent=1) + "\n"
    if args.out: args.out.write_text(text)
    else: print(text, end="")


if __name__ == "__main__":
    main()
