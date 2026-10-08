"""Check captured finalizer words against a cubin produced by the same run.

python3 research/ptxas/validate_capture.py CAPTURE.jsonl OUTPUT.cubin
"""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sass.cubin import text_sections


def validate(capture, cubin):
    records = [json.loads(line) for line in Path(capture).read_text().splitlines()]
    dispatches = [r for r in records if r["stage"] == "native_dispatch"]
    encodings = [r for r in records if r["stage"] == "native_encoded"]
    if len(dispatches) != len(encodings):
        raise ValueError("Capture lacks a matching encoding for every dispatch")
    for dispatch, encoding in zip(dispatches, encodings):
        if dispatch["instruction"] != encoding["instruction"]:
            raise ValueError("Dispatch and encoded instruction identities differ")
        if len(bytes.fromhex(encoding["bytes"])) != 16:
            raise ValueError("Native instruction is not 16 bytes")
    captured = b"".join(bytes.fromhex(r["bytes"]) for r in encodings)
    actual = b"".join(text_sections(Path(cubin).read_bytes()).values())
    if not captured or captured != actual:
        raise ValueError("Captured native words differ from cubin text")
    print(f"Matched {len(encodings)} instructions / {len(captured)} bytes: {capture}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    validate(*sys.argv[1:])
