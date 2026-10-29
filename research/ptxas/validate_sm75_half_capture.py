"""Validate partial legacy-half compiler captures against their own cubins.

This checks exact word membership, not a complete or ordered text capture.
Compiler sizing and emission passes can capture the same instruction twice.
No control-bit masking and no GPU execution are involved.

python research/ptxas/validate_sm75_half_capture.py CAPTURE.jsonl OUTPUT.cubin
"""
import collections
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from sass.decode import cubin, pydecode


def validate(capture, image):
    rows = [json.loads(line) for line in Path(capture).read_text().splitlines()]
    identities = [row for row in rows if row["stage"] == "identity"]
    expected = "daba837a68265cae38c832d13399b61dab811891de9b8914defddef143b849f2"
    if len(identities) != 1 or identities[0]["sha256"] != expected:
        raise ValueError("Missing pinned compiler identity")
    binary = Path(image).read_bytes()
    if cubin.elf_arch(binary) != "SM75":
        raise ValueError("Expected an SM75 cubin")
    starts = [row for row in rows if row["stage"] == "half_emission"]
    ends = [row for row in rows if row["stage"] == "half_encoded"]
    if not starts or len(starts) != len(ends):
        raise ValueError("Missing matching half emission/encoding records")
    actual = set(cubin.text_words(binary))
    opcodes = collections.Counter()
    for start, end in zip(starts, ends):
        if (start["instruction"], start["handler"]) != (end["instruction"], end["handler"]):
            raise ValueError("Emission and encoding identities differ")
        word = bytes.fromhex(end["word"])
        if len(word) != 16 or word not in actual:
            raise ValueError(f"Captured word is absent from cubin: {end['word']}")
        if len(end["final_modes"]) != 8:
            raise ValueError("Missing final operand modes")
        opcodes[pydecode.decode("SM75", word).klass.mnemonic] += 1
    return dict(kind="partial compiler capture; exact word membership",
                captured_words=len(ends), unique_words=len({row["word"] for row in ends}),
                opcodes=dict(sorted(opcodes.items())), control_bits_masked=False)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    print(json.dumps(validate(*sys.argv[1:]), indent=2))
