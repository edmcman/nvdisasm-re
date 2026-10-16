"""Compare one emulator result with a Case's expectations.

Plain keys are observed registers; `__` keys are flags: __native (p-code without CALLOTHER),
__max_ops (at most N ops, no branches or CALLOTHER), __counter (final PC), __memory (first
observed memory hex), __events (runtime event count), __fp8_nan (an FP8 format: packed
byte lanes match if equal or both NaN), __opaque (CALLOTHER p-code or an error starting with
the given text). `error` expects a failure message; `illegal` (substrings of an md condition's
message, all present) expects the word not to decode at all. __atomic (a LOAD count): the
p-code is one atomic region holding those loads and every store.
"""
from tests.refs.fpformat import same_fp8


def problems(expected, result):
    if "illegal" in expected:
        return [] if result.get("error") else ["decoded an md-illegal encoding"]
    if "error" in expected:
        return [] if expected["error"] in result.get("error", "") else [f"expected error {expected['error']!r}"]
    ops = result.get("pcode_ops", [])
    if "__opaque" in expected:
        return [] if "CALLOTHER" in ops or result.get("error", "").startswith(expected["__opaque"]) else ["opaque fallback"]
    registers = {n: v for n, v in expected.items() if not n.startswith("__")}
    checks = [
        (True, result.get("registers") == registers if "__fp8_nan" not in expected else
         set(result.get("registers", {})) == set(registers) and
         all(same_fp8(result["registers"][n], v, expected["__fp8_nan"]) for n, v in registers.items()), "registers"),
        ("__counter" in expected, result.get("counter") == expected.get("__counter"), "counter"),
        ("__memory" in expected, bool(result.get("memory")) and result["memory"][0]["hex"] == expected.get("__memory"), "memory"),
        ("__events" in expected, len(result.get("events", [])) == expected.get("__events"), "events"),
        ("__native" in expected, bool(ops) and "CALLOTHER" not in ops, "native p-code"),
        ("__max_ops" in expected, bool(ops) and len(ops) <= expected.get("__max_ops", 0)
         and not set(ops) & {"CBRANCH", "CALLOTHER"}, "p-code op count"),
        ("__atomic" in expected, atomic_region(result.get("pcode_sequence", []), expected.get("__atomic")), "atomic region")]
    return [name for wanted, ok, name in checks if wanted and not ok]


def atomic_region(sequence, loads):
    """The p-code opens one atomic region first, closes it once, and holds `loads` LOADs and every
    STORE inside it."""
    if not sequence or sequence[0] != "sass_atomic_begin" or sequence.count("sass_atomic_end") != 1 or sequence.count("sass_atomic_begin") != 1: return False
    end = sequence.index("sass_atomic_end")
    inside = sequence[:end]
    return inside.count("LOAD") == loads and "LOAD" not in sequence[end:] and "STORE" in inside and "STORE" not in sequence[end:]
