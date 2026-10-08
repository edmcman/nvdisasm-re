"""Verify captured SM100 mad.hi and mad.wide lowering against PTX formulas.

Usage: python3 research/ptxas/check_mad.py HI_CUBIN WIDE_CUBIN
Emulator consistency only. Exercises native arithmetic, not memory/prologue.
"""
import random
import sys
from pathlib import Path
from check_wide_chains import emulate
from sass.cubin import kernel_image
from sass import pydecode


def image(path, mode):
    result = kernel_image(Path(path).read_bytes(), "mad_" + mode + "_u32")
    if result["arch"] != "SM100":
        raise ValueError("SM100 probe required")
    return result["code"]


def request(word, registers, observe):
    return dict(word=word.hex(), registers=registers, observe=observe, inspect_pcode=True)


def main(hi_path, wide_path):
    mask32, mask64 = (1 << 32) - 1, (1 << 64) - 1
    edges = [0, 1, 0x7fffffff, 0x80000000, mask32]
    rng = random.Random(170)
    vectors = [(a, b, c) for a in edges for b in edges for c in edges]
    vectors += [(rng.getrandbits(32), rng.getrandbits(32), rng.getrandbits(64))
                for _ in range(512)]
    hi, wide = image(hi_path, "hi"), image(wide_path, "wide")
    assert pydecode.decode("SM100", hi[0x60:0x70]).klass.mnemonic == "IMAD"
    assert pydecode.decode("SM100", wide[0x40:0x50]).klass.mnemonic == "UIMAD"
    assert all(pydecode.decode("SM100", wide[o:o+16]).klass.mnemonic == "UIADD3"
               for o in (0x50, 0x60))
    # PTX c resides in R5; compiler sets R4=0. IMAD.HI's addend pair is R4/R5.
    hi_requests = [request(hi[0x60:0x70], dict(R4=0, R5=c & mask32, R6=a, R7=b), ["R5"])
                   for a, b, c in vectors]
    hi_results = emulate(hi_requests)
    for (a, b, c), result in zip(vectors, hi_results):
        expected = ((a * b >> 32) + (c & mask32)) & mask32
        if result["registers"]["R5"] != expected:
            raise AssertionError(("mad.hi", a, b, c, expected, result))
    print(f"SM100 mad.hi lowering: {len(vectors)} PTX-formula comparisons passed", flush=True)
    # Additional SASS HI tests give the addend pair a nonzero low half. These
    # distinguish a full 64-bit sum then >>32 from high(product)+high(addend).
    extra = [(mask32, mask32, 0xffffffff), (mask32, mask32, mask64),
             (0x80000001, 0x80000001, mask64)]
    extra += [(rng.getrandbits(32), rng.getrandbits(32), rng.getrandbits(64)) for _ in range(512)]
    results = emulate([request(hi[0x60:0x70], dict(R4=c & mask32, R5=c >> 32, R6=a, R7=b), ["R5"])
                       for a, b, c in extra])
    for (a, b, c), result in zip(extra, results):
        if result["registers"]["R5"] != ((a * b + c) & mask64) >> 32:
            raise AssertionError(("SASS HI low-word carry", a, b, c, result))
    print(f"SM100 IMAD.HI full-addend: {len(extra)} comparisons passed", flush=True)
    regs = [dict(UR4=a, UR5=b, UR6=c & mask32, UR7=c >> 32) for a, b, c in vectors]
    for offset, observe in ((0x40, ["UR4", "UR5"]), (0x50, ["UR4", "UP0"]),
                            (0x60, ["UR5"])):
        results = emulate([request(wide[offset:offset+16], r, observe) for r in regs])
        for r, result in zip(regs, results):
            r.update(result["registers"])
    for (a, b, c), result in zip(vectors, regs):
        actual = result["UR4"] | result["UR5"] << 32
        expected = (a * b + c) & mask64
        if actual != expected:
            raise AssertionError(("mad.wide", a, b, c, expected, actual))
    print(f"SM100 mad lowering: {len(vectors)*2} PTX-formula comparisons and "
          f"{len(extra)} full-addend IMAD.HI comparisons passed in native p-code")


if __name__ == "__main__":
    main(*sys.argv[1:])
