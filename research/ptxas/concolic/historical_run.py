#!/usr/bin/env python3
"""Run the AFL++/SymQEMU PTX experiment with all outputs outside the repo."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess

HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools", type=Path, default=HERE / "tools")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ptxas", type=Path, default=Path("/usr/local/cuda-13.0/bin/ptxas"))
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--worker-seconds", type=int, default=30)
    parser.add_argument("--seed", type=Path, default=HERE / "instruction.ptx")
    parser.add_argument("--mode", choices=("afl", "symqemu"), default="afl")
    parser.add_argument("--scope", choices=("instruction", "opcode", "file"), default="instruction")
    parser.add_argument("--restrict-add-sub", action="store_true",
                        help="reproduce the initial two-opcode experiment; requires --scope opcode")
    parser.add_argument("--concolic-only", action="store_true",
                        help="disable regular AFL mutations to isolate solver-generated cases")
    args = parser.parse_args()
    if args.restrict_add_sub and args.scope != "opcode":
        parser.error("--restrict-add-sub requires --scope opcode")
    tools = args.tools.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    seed = output / "seeds" / "add.ptx"
    seed.parent.mkdir()
    seed.write_bytes(args.seed.read_bytes())
    afl = tools / "AFLplusplus"
    symqemu = tools / "symqemu" / "build" / "symqemu-x86_64"
    env = os.environ.copy()
    env.update({
        "PATH": f"{HERE}:{afl}:{env.get('PATH', '')}",
        "AFL_PATH": str(afl), "AFL_DISABLE_TRIM": "1", "SYMQEMU_ALL": "1",
        "AFL_MAP_SIZE": "1048576",
        "AFL_QEMU_MAP_SIZE": "1048576",
        "AFL_NO_UI": "1", "AFL_NO_AFFINITY": "1", "AFL_SKIP_CPUFREQ": "1",
        "AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES": "1",
        "AFL_IGNORE_UNKNOWN_ENVS": "1",
        "AFL_CUSTOM_MUTATOR_LIBRARY": str(afl / "custom_mutators/symqemu/symqemu-mutator.so"),
        "PTX_SYMQEMU_BINARY": str(symqemu),
        "PTX_SYMQEMU_LOG_DIR": str(output / "workers"),
        "PTX_SYMQEMU_TIMEOUT": str(args.worker_seconds),
    })
    env.pop("AFL_CUSTOM_MUTATOR_ONLY", None)
    env.pop("AFL_EXIT_WHEN_DONE", None)
    if args.concolic_only:
        env["AFL_CUSTOM_MUTATOR_ONLY"] = "1"
    # Avoid inheriting an earlier experiment's symbolic restrictions.
    for key in ("PTX_SYMBOLIC_BEGIN", "PTX_SYMBOLIC_END", "PTX_OPCODE_DOMAIN"):
        env.pop(key, None)
    env["PTX_VALIDATE_GENERATED"] = "1"
    env["PTX_SYMBOLIC_SCOPE"] = args.scope
    if args.scope != "file":
        data = seed.read_bytes()
        start_marker = b"// BEGIN_INSTRUCTION\n"
        end_marker = b"// END_INSTRUCTION"
        marker = data.find(start_marker)
        if marker >= 0:
            begin = marker + len(start_marker)
            end = data.find(end_marker, begin)
            if end < 0:
                parser.error("missing END_INSTRUCTION marker")
        else:
            match = re.search(rb"(?:add|sub)\.u32\s+%r2[^;]*;", data)
            if not match:
                parser.error("seed needs BEGIN_INSTRUCTION/END_INSTRUCTION markers or --scope file")
            begin, end = match.span()
        if args.scope == "opcode":
            token = re.match(rb"[a-zA-Z0-9_]+", data[begin:end])
            if token is None:
                parser.error("instruction must begin with an opcode for --scope opcode")
            end = begin + len(token.group())
        env.update({"PTX_SYMBOLIC_BEGIN": str(begin), "PTX_SYMBOLIC_END": str(end)})
        if args.restrict_add_sub:
            env["PTX_OPCODE_DOMAIN"] = "add-sub"
    target = [str(args.ptxas.resolve()), "-arch=sm_100", "-o", "/dev/null"]
    if args.mode == "afl":
        argv = [str(afl / "afl-fuzz"), "-Q", "-z", "-m", "none", "-t", "2000",
                "-V", str(args.seconds), "-i", str(seed.parent),
                "-o", str(output / "afl"), "-x", str(HERE / "ptx.dict"), "--", *target, "@@"]
    else:
        generated = output / "generated"
        generated.mkdir()
        env.update({"SYMCC_INPUT_FILE": str(seed), "SYMCC_OUTPUT_DIR": str(generated)})
        argv = [str(HERE / "symqemu-x86_64"), *target, str(seed)]
    (output / "command.json").write_text(json.dumps({"argv": argv, "mode": args.mode,
        "worker_seconds": args.worker_seconds,
        "scope": args.scope, "restrict_add_sub": args.restrict_add_sub,
        "regular_afl_mutations": not args.concolic_only,
        "symbolic_range": None if args.scope == "file" else [begin, end]}, indent=2) + "\n")
    with (output / "run.log").open("wb") as log:
        child = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        (output / "process.json").write_text(json.dumps({"driver_pid": os.getpid(),
                                                       "afl_pid": child.pid}) + "\n")
        for line in child.stdout:
            if b"Fuzzing test case #" in line or b"Entering queue cycle" in line:
                continue
            if line.strip() in (b"", b"\x1b[0m"):
                continue
            log.write(line)
            log.flush()
        rc = child.wait()
    (output / "exit.json").write_text(json.dumps({"returncode": rc}) + "\n")
    print(f"returncode={rc}; logs={output}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
