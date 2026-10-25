"""SM89 POPC.INC discovery using the shared native GPU probe runner.

Run outside the sandbox:
    nice uv run python -m research.popc_inc.probe --output /tmp/popc-inc
    nice uv run python -m research.popc_inc.probe --controls --output /tmp/popc-inc-controls

Validate a saved capture without a GPU:
    nice uv run python -m research.popc_inc.probe --check research/popc_inc/observations.json

The existing memory and warp adapters do not combine shared atomics with lane selection.
This research wrapper uses their instruction builder, scheduling, cubin writer and isolated
runner. It neither implements instruction semantics nor creates verification evidence.
"""
import argparse
import json
from pathlib import Path
import subprocess

from sass import launch, paths
from tests.harness import device_cases, device_setup
from tests.harness.case import Cases

ARCH = "SM89"
SENTINEL = 0xdeadbeef
MARKER = 0x75abcdef
UNWRITTEN = 0xcccccccc
MASKS = (0xffffffff, 0x25, 0x80000001, 1 << 5, 0xaaaaaaaa, 0)


def configurations(controls=False):
    def configuration(mask, base=10, destination=8, mode="predicate", offset=0, repetitions=1):
        return dict(operation="add" if controls else "popc", mask=mask, base=base,
                    destination=destination, mode=mode, address=256, offset=offset,
                    repetitions=repetitions)

    if controls:
        return [configuration(mask) for mask in MASKS[:4]]
    out = [configuration(mask) for mask in MASKS]
    out += [configuration(mask, mode="exit") for mask in MASKS[:-1]]
    out.append(configuration(0x25, destination="RZ"))
    for base in (0, 0xffffffff, 0xfffffff0, 0x7fffffff, 0x80000000):
        out += [configuration(mask, base=base) for mask in (0xffffffff, 0x25, 0x80000001, 1 << 31)]
    for mask in MASKS[:3]:
        out += [configuration(mask, offset=offset) for offset in (16, -16)]
        out += [configuration(mask, repetitions=count) for count in (2, 3)]
    return out


def make_probe(index, config):
    mask, base, address = config["mask"], config["base"], config["address"]
    destination, offset = config["destination"], config["offset"]
    mode, repetitions = config["mode"], config["repetitions"]
    guard = 0 if mode == "predicate" else "PT"
    fields = dict(Rd=destination, Ra="RZ", Ra_URc=2, Ra_offset=offset, Pg=guard)
    registers = {"UR2": address - offset, "R8": SENTINEL}
    cls = "atoms_arrive__popcinc"
    if config["operation"] == "add":
        cls = "atoms__RaNonRZ"
        fields = dict(Rd=destination, Ra=23, Rb=9, Ra_offset=0, Pg=guard, op="ADD", sz="32")
        registers["R9"] = 1
    cases = Cases(ARCH)
    case = cases.probe(cls, fields, registers, observe=["R8"],
                       memory=[dict(space="shared", address=address, hex=base.to_bytes(4, "little").hex())],
                       context={"warp": dict(active=mask, mode=mode, repetitions=repetitions)})
    scenario = device_cases.Scenario("popc_inc_discovery", case, "custom", arch=ARCH)
    code = device_cases.Code(ARCH)

    # Lane zero initializes shared memory; all 32 lanes synchronize before mask selection.
    code.emit("s2r_", Rd=16, SRa="SR_LANEID")
    code.mov("R23", address)
    code.mov("R24", base)
    code.emit("isetp__RsIR_RIR_noEX", Pu=6, Pv="PT", Ra=16, Sb=0, Pp="PT",
              icmp="EQ", fmt="U32", bop="AND")
    code.emit("sts__sImmOffset", Pg=6, Ra=23, Ra_offset=0, Rb=24, sz="32")
    code.emit("bar__SYNC_II_optionalCount_II", Sb=0, Sc=32)
    code.mov("UR2", address - offset)
    code.mov("R8", SENTINEL)
    code.mov("R9", 1)
    if mode == "predicate":
        code.mov("R17", mask)
        code.emit("shf__RRR_RRR", Rd=17, Ra=17, Rb=16, Rc="RZ", dir="R", fmt="U32", hilo="LO")
        code.emit("lop3_lut__RuIR_RIR", Rd=17, Ra=17, Sb=1, Rc="RZ", imm8=0xc0, Pp="PT")
        code.emit("isetp__RsIR_RIR_noEX", Pu=0, Pv="PT", Ra=17, Sb=0, Pp="PT",
                  icmp="NE", fmt="U32", bop="AND")
    else:
        device_setup.lane_guard(code, 32, mask, mask=True)
    for _ in range(repetitions):
        code.targets.append((len(code.words) * 16, case.request["word"]))
        code.raw(int.from_bytes(bytes.fromhex(case.request["word"]), "little"))

    # This fence is part of the capture, not a claim about POPC.INC's intrinsic ordering.
    code.emit("membar_", sem="ALL", sco="CTA")
    code.emit("lds__sImmOffset", Rd=24, Ra=23, Ra_offset=0, sz="32")
    code.parameter(20, 64)
    code.parameter(21, 68)
    code.emit("imad_wide__RsIR_RIR", Rd=20, Ra=16, Sb=16, Rc=20, fmt="U32")
    code.emit("uldc_const__RCR", URd=4, Sa_bank=0, Sa_addr=launch.GLOBAL_DESCRIPTOR[ARCH], sz="64")
    code.mov("R25", MARKER)
    for at, register in ((0, 8), (4, 24), (8, 16), (12, 25)):
        code.emit("stg__sImmOffset", Ra=20, Ra_offset=at, Rb=register, sz="32", e="E")
    code.emit("exit_")
    names = [f"lane{lane}_{field}" for lane in range(32) for field in ("Rd", "memory", "lane", "marker")]
    probe = device_cases.Probe(f"popc_inc_{index}", [scenario], code, (), [(0, names)], 512,
                              shared=address + 16, threads=32, raw=True)
    config = dict(config, instruction=device_cases.decoded(case.request["word"], ARCH).text,
                  word=case.request["word"])
    return probe, config


def validate(data):
    """Check the discovered candidate rule and capture integrity, without emulator semantics."""
    for row in data["observations"]:
        assert "error" not in row, row
        mask, base, repetitions = row["mask"], row["base"], row["repetitions"]
        count = mask.bit_count()
        final = (base + count * repetitions) & 0xffffffff
        old = (base + count * (repetitions - 1)) & 0xffffffff
        active_values = []
        for lane in row["lanes"]:
            active = bool(mask >> lane["lane"] & 1)
            if row["mode"] == "exit" and not active:
                assert lane["marker"] == UNWRITTEN, lane
                continue
            assert lane["marker"] == MARKER and lane["captured_lane"] == lane["lane"], lane
            assert lane["memory"] == final, (row, lane)
            if active and row["destination"] != "RZ":
                active_values.append(lane["Rd"])
                if row["operation"] == "popc":
                    assert lane["Rd"] == old, (row, lane)
            else:
                assert lane["Rd"] == SENTINEL, (row, lane)
        if row["operation"] == "add":
            # The control must capture distinct values; do not assume an ordering among lanes.
            assert sorted(active_values) == [(base + i) & 0xffffffff for i in range(count)], row
    return len(data["observations"])


def observe(configs, output):
    output.mkdir(parents=True, exist_ok=True)
    prepared = [make_probe(index, config) for index, config in enumerate(configs)]
    observations, device = [], None
    for start in range(0, len(prepared), 16):
        part = prepared[start:start + 16]
        probes = [probe for probe, _ in part]
        binary = device_cases.image(probes)
        path = output / f"probes_{start}.cubin"
        path.write_bytes(binary)
        path.with_suffix(".sass").write_text(subprocess.check_output([str(paths.NVDISASM), str(path)], text=True))
        result = device_cases.isolated(probes, timeout=30)
        device = result.get("device") or device
        for probe, config in part:
            raw = result["results"][probe.scenarios[0].id]
            if isinstance(raw, list):
                row = dict(config, error="\n".join(raw))
            else:
                values = raw["values"]
                lanes = [dict(lane=lane, Rd=values[4 * lane], memory=values[4 * lane + 1],
                              captured_lane=values[4 * lane + 2], marker=values[4 * lane + 3])
                         for lane in range(32)]
                row = dict(config, lanes=lanes)
            observations.append(row)
            print(config["instruction"], hex(config["mask"]), hex(config["base"]),
                  "repetitions", config["repetitions"], row.get("error", "captured"), flush=True)
    data = dict(arch=ARCH, device=device, observations=observations)
    (output / "observations.json").write_text(json.dumps(data, indent=1) + "\n")
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--output", type=Path, help="write GPU observations, cubins and disassembly here")
    action.add_argument("--check", type=Path, help="validate a saved capture without GPU access")
    parser.add_argument("--controls", action="store_true", help="probe ordinary ATOMS.ADD instead")
    args = parser.parse_args()
    data = json.loads(args.check.read_text()) if args.check else observe(configurations(args.controls), args.output)
    print(f"{data['device']}: validated {validate(data)} scenarios")


if __name__ == "__main__":
    main()
