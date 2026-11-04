import argparse
import json
import statistics
import time

from sass.emulate import kernel
from tests.harness import synchronization


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out")
    a = p.parse_args()
    request = synchronization.request("""
MOV R8, 1000;
MOV R9, 0;
loop:
IADD3 R9, R9, 1, RZ;
IADD3 R8, R8, -1, RZ;
ISETP.NE.U32.AND P1, PT, R8, RZ, PT;
@P1 BRA loop;
MOV R8, R9;
""", threads=1024, block=1024)
    assert "error" not in kernel.run_kernel(request, "SM89")
    rows = []
    for _ in range(3):
        wall, cpu = time.monotonic(), time.process_time()
        result = kernel.run_kernel(request, "SM89")
        assert "error" not in result and result["outputs"] == ["e8030000"] * 1024, result
        rows.append(dict(wall=time.monotonic() - wall, cpu=time.process_time() - cpu, steps=result["steps"]))
    result = dict(rows=rows, median_cpu=statistics.median(row["cpu"] for row in rows),
                  median_wall=statistics.median(row["wall"] for row in rows))
    text = json.dumps(result, indent=1) + "\n"
    if a.out:
        from pathlib import Path
        Path(a.out).write_text(text)
    print(text, end="")


if __name__ == "__main__":
    main()
