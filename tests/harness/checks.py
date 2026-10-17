"""Compare one emulator result with a Case's expectations.

Plain keys are observed registers; `__` keys are flags: __native (p-code without CALLOTHER),
__max_ops (at most N ops, no branches or CALLOTHER; the emulator reports {op: count}), __counter (final PC), __memory (first
observed memory hex), __events (runtime event count), __fp8_nan (an FP8 format: packed
byte lanes match if equal or both NaN), __opaque (CALLOTHER p-code or an error starting with
the given text). `error` expects a failure message.
"""
from tests.refs.fpformat import same_fp8


def problems(expected, result):
    if "error" in expected:
        return [] if expected["error"] in result.get("error", "") else [f"expected error {expected['error']!r}"]
    ops = result.get("pcode_ops", {})
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
        ("__max_ops" in expected, bool(ops) and sum(ops.values()) <= expected.get("__max_ops", 0)
         and not set(ops) & {"CBRANCH", "CALLOTHER"}, "p-code op count")]
    return [name for wanted, ok, name in checks if wanted and not ok]
