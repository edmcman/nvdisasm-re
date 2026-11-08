import argparse
import hashlib
import json
import math
import time
from collections import Counter
from pathlib import Path

from sass import launch
from sass.decode import cubin, pydecode
from sass.emulate import kernel
from research.additional_opcodes import OPS


def replay(root, captured, modules, max_threads, max_steps):
    name, ident = captured["name"], captured["id"]
    row = dict(id=ident, name=name, comparisons=[])
    if name not in modules: return row | dict(status="blocked", reason="captured cubin missing")
    image = cubin.kernel_image(modules[name], name)
    threads = math.prod(captured["grid"]) * math.prod(captured["block"])
    row.update(architecture=image["arch"], threads=threads)
    if threads > max_threads: return row | dict(status="blocked", reason=f"launch exceeds {max_threads} thread replay limit")
    if len(image["code"]) > 131072: return row | dict(status="blocked", reason="code exceeds 128 KiB replay limit")
    memory, comparisons = [], []
    for region in captured["regions"]:
        address = region["address"]
        before = root / f"{ident}-before-{address}.bin"
        after = root / f"{ident}-after-{address}.bin"
        if not before.exists() or not after.exists():
            return row | dict(status="blocked", reason="captured memory file missing")
        initial, expected = before.read_bytes(), after.read_bytes()
        memory.append(dict(space="ram", address=address, hex=initial.hex()))
        comparisons.append((address, expected))
    if not comparisons: return row | dict(status="uncompared", reason="capture has no memory region")
    start, visited = time.monotonic(), set()
    for address, expected in comparisons:
        stride = math.ceil(len(expected) / threads)
        request = launch.kernel_request(image, [bytes.fromhex(v) for v in captured["arguments"]], memory,
            block=captured["block"], grid=captured["grid"],
            output=dict(address=address, size=stride, stride=stride))
        request["kernel"]["max_steps"] = max_steps
        result = kernel.run_kernel(request, image["arch"])
        if "error" in result:
            return row | dict(status="blocked", reason=result["error"], seconds=time.monotonic() - start)
        actual = bytes.fromhex("".join(result["outputs"]))[:len(expected)]
        visited.update(result["visited"])
        row["comparisons"].append(dict(address=address, size=len(expected), exact=actual == expected,
            differing_bytes=sum(a != b for a, b in zip(actual, expected)),
            expected_sha256=hashlib.sha256(expected).hexdigest(), actual_sha256=hashlib.sha256(actual).hexdigest()))
    executed = Counter(pydecode.decode(image["arch"], image["code"][offset:offset + 16]).klass.mnemonic for offset in visited)
    return row | dict(status="pass" if all(c["exact"] for c in row["comparisons"]) else "mismatch",
                      seconds=time.monotonic() - start, executed=dict(executed),
                      executed_targets={op: executed[op] for op in OPS if executed[op]})


def main():
    p = argparse.ArgumentParser()
    p.add_argument("directory", type=Path)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--max-threads", type=int, default=262144)
    p.add_argument("--max-steps", type=int, default=10000)
    a = p.parse_args()
    modules = {}
    for file in sorted(a.directory.glob("module-*.cubin")):
        data = file.read_bytes()
        modules.update({name.removeprefix(".text."): data for name in cubin.text_sections(data)})
    rows = []
    for captured in map(json.loads, (a.directory / "launches.jsonl").read_text().splitlines()):
        if captured.get("error"): continue
        try: row = replay(a.directory, captured, modules, a.max_threads, a.max_steps)
        except (ValueError, KeyError) as error:
            row = dict(id=captured["id"], name=captured["name"], status="blocked", reason=str(error), comparisons=[])
        rows.append(row)
        a.out.write_text(json.dumps(dict(summary=dict(Counter(r["status"] for r in rows)), launches=rows), indent=1) + "\n")
        print(captured["id"], row["status"], row.get("reason", "")[:160], flush=True)


if __name__ == "__main__":
    main()
