"""Check actual ptxas SM100 add/sub carry words against PTX integer formulas.

Usage: python3 research/ptxas/check_wide_chains.py ADD_CUBIN SUB_CUBIN
Executes the low and high words using actual Ghidra p-code, transferring the
low-word result and predicate into the high-word state. This is an emulator
consistency check, not a claim of SM100 hardware verification.
"""
import json
import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from compare_ghidra import GHIDRA_PY, LDEFS
from sass.cubin import kernel_image
from sass import pydecode


def emulate(requests):
    process = subprocess.run(
        [GHIDRA_PY, str(ROOT / "tests/ghidra_emulate.py"), str(LDEFS),
         "SASS:LE:64:sm100"],
        input="".join(json.dumps(r) + "\n" for r in requests),
        capture_output=True, text=True, cwd=ROOT)
    if process.returncode:
        raise RuntimeError(process.stderr[-3000:])
    results = [json.loads(l) for l in process.stdout.splitlines() if l.startswith("{")]
    if len(results) != len(requests):
        raise RuntimeError(process.stderr[-2000:])
    for result in results:
        if "error" in result:
            raise RuntimeError(result["error"])
        if "CALLOTHER" in result["pcode_ops"]:
            raise AssertionError("carry arithmetic must execute native p-code")
    return results


def main(add_path, sub_path):
    mask32, mask64 = (1 << 32) - 1, (1 << 64) - 1
    edges = [0, 1, mask32 - 1, mask32, 1 << 32, (1 << 32) + 1,
             (1 << 63) - 1, 1 << 63, mask64 - 1, mask64]
    rng = random.Random(100)
    vectors = [(a, b) for a in edges for b in edges]
    vectors += [(rng.getrandbits(64), rng.getrandbits(64)) for _ in range(512)]
    entries, low_requests = [], []
    for operation, path in (("add", add_path), ("sub", sub_path)):
        image = kernel_image(Path(path).read_bytes(), operation + "_u64")
        if image["arch"] != "SM100":
            raise ValueError("SM100 probe required")
        words = [image["code"][offset:offset + 16] for offset in (0x40, 0x50)]
        for word in words:
            if pydecode.decode("SM100", word).klass.mnemonic != "UIADD3":
                raise AssertionError("expected captured UIADD3 chain at 0x40/0x50")
        for a, b in vectors:
            registers = dict(UR4=a & mask32, UR5=a >> 32,
                             UR6=b & mask32, UR7=b >> 32)
            entries.append((operation, a, b, words[1], registers))
            low_requests.append(dict(word=words[0].hex(), registers=registers,
                                     observe=["UR4", "UP0"], inspect_pcode=True))
    low_results = emulate(low_requests)
    high_requests = []
    for entry, low in zip(entries, low_results):
        registers = dict(entry[4], **low["registers"])
        high_requests.append(dict(word=entry[3].hex(), registers=registers,
                                  observe=["UR5"], inspect_pcode=True))
    high_results = emulate(high_requests)
    for entry, low, high in zip(entries, low_results, high_results):
        operation, a, b = entry[:3]
        actual = low["registers"]["UR4"] | high["registers"]["UR5"] << 32
        expected = (a + b if operation == "add" else a - b) & mask64
        if actual != expected:
            raise AssertionError((operation, hex(a), hex(b), hex(actual), hex(expected)))
        # Ordinary negation keeps the 33rd-bit +1, including b_low == 0.
        low_sum = (a & mask32) + ((b & mask32) if operation == "add"
                                  else ((b & mask32) ^ mask32) + 1)
        if low["registers"]["UP0"] != int(low_sum >= 1 << 32):
            raise AssertionError((operation, "low carry", a, b, low))
    print(f"SM100 captured add/sub chains: {len(entries)} PTX-formula comparisons passed "
          f"({len(entries) * 2} native-p-code instruction executions)")


if __name__ == "__main__":
    main(*sys.argv[1:])
