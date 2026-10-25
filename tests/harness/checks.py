"""Compare one emulator result with a Case's expectations.

Plain keys are observed registers; `__` keys are flags: __native (nonempty p-code without CALLOTHER),
__max_ops (integer: at most N ops, no branches or CALLOTHER; dict: count bound and exact userop list, with explicit optional branch permission),
__counter (final PC), __memory (first observed memory hex), __events (runtime event count),
__event_values (exact runtime event records),
__fp8_nan (an FP8 format: packed byte lanes match if equal or both NaN),
__opaque (CALLOTHER p-code or an error starting with
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
        ("__event_values" in expected, result.get("events") == expected.get("__event_values"), "event values"),
        ("__native" in expected, bool(ops) and "CALLOTHER" not in ops, "native p-code"),
        ("__max_ops" in expected, within_pcode_limit(expected.get("__max_ops"),result), "p-code op count")]
    return [name for wanted, ok, name in checks if wanted and not ok]


def within_pcode_limit(limit,result):
    """Keep integer limits strict; structured limits require every named effect and explicit branch permission."""
    ops=result.get('pcode_ops')
    if isinstance(limit,dict):
        if set(limit) not in ({'count','userops'},{'count','userops','branches'}):return False
        if 'branches' in limit and limit['branches'] is not True:return False
        effects=result.get('pcode_userops')
        return (isinstance(ops,dict) and effects is not None
                and sum(ops.values())<=limit['count'] and (limit.get('branches') is True or 'CBRANCH' not in ops)
                and ops.get('CALLOTHER',0)==len(limit['userops'])
                and sorted(effects)==sorted(limit['userops']))
    return bool(ops) and limit is not None and sum(ops.values())<=limit and not set(ops)&{'CBRANCH','CALLOTHER'}
