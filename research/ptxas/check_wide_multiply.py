"""Check compiled SM100 signed/unsigned wide-multiply words in native p-code.

Usage: python3 research/ptxas/check_wide_multiply.py U32_CUBIN S32_CUBIN
Emulator consistency against PTX formulas; no SM100 hardware claim.
"""
import random
import sys
from pathlib import Path
from check_wide_chains import emulate, ROOT
from sass.cubin import kernel_image
from sass import pydecode


def main(unsigned_path, signed_path):
    edges = [0, 1, 2, 0x7ffffffe, 0x7fffffff, 0x80000000,
             0x80000001, 0xfffffffe, 0xffffffff]
    rng = random.Random(132)
    vectors = [(a, b) for a in edges for b in edges]
    vectors += [(rng.getrandbits(32), rng.getrandbits(32)) for _ in range(512)]
    requests, references = [], []
    for mode, path in (("u32", unsigned_path), ("s32", signed_path)):
        image = kernel_image(Path(path).read_bytes(), "mul_wide_" + mode)
        if image["arch"] != "SM100":
            raise ValueError("SM100 probe required")
        word = image["code"][0x40:0x50]
        if pydecode.decode("SM100", word).klass.mnemonic != "IMAD":
            raise AssertionError("expected IMAD.WIDE probe at 0x40")
        for a, b in vectors:
            requests.append(dict(word=word.hex(), registers=dict(R2=a, R3=b),
                                 observe=["R2", "R3"], inspect_pcode=True))
            # Sources overlap the pair destination in the actual compiler word.
            x, y = a, b
            if mode == "s32":
                x = a - (1 << 32) if a & (1 << 31) else a
                y = b - (1 << 32) if b & (1 << 31) else b
            references.append((mode, a, b, (x * y) & ((1 << 64) - 1)))
    for reference, result in zip(references, emulate(requests)):
        mode, a, b, expected = reference
        actual = result["registers"]["R2"] | result["registers"]["R3"] << 32
        if actual != expected:
            raise AssertionError((mode, hex(a), hex(b), hex(actual), hex(expected)))
    print(f"SM100 captured signed/unsigned IMAD.WIDE: {len(requests)} "
          "PTX-formula comparisons passed in native p-code")


if __name__ == "__main__":
    main(*sys.argv[1:])
