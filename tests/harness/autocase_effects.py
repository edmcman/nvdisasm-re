from itertools import product

from tests.harness import autocase_profiles
from tests.refs import asynchronous, barrier, cache

OPS = {"B2R", "MEMBAR", "LDGSTS", "LDGDEPBAR", "DEPBAR", "CCTL", "ERRBAR", "YIELD"}
FAMILIES = {op: "asynchronous" if op in ("LDGSTS", "LDGDEPBAR", "DEPBAR") else "control" for op in OPS}


def generate(c, k, form, profiles):
    from tests.harness.autocases import Inputs, Unsupported
    if profiles != ["effect_forms"]: raise Unsupported("incompatible effect profile")
    match k.mnemonic:
        case "B2R":
            for result in autocase_profiles.PROFILES["effect_forms"]["results"]:
                indices = range(16) if form["mode"] == "BAR" else (0,)
                for index, dest in product(indices, (8, 255)):
                    s, expected = Inputs(c, k, form), result
                    s.fields["Rd"] = dest
                    match form["mode"]:
                        case "BAR":
                            s.fields["barname"] = index
                            states = [value ^ result for value in range(16)]
                            states[index] = result
                            context = dict(barrier_states=states)
                        case "WARP":
                            expected, context = barrier.warp(result), dict(barrier_result=result)
                        case "RESULT":
                            s.fields["Pu"] = 1
                            context = dict(barrier_result=result)
                        case _: raise Unsupported("unknown barrier-state mode")
                    outputs = {f"R{dest}": expected if dest != 255 else 0}
                    if form["mode"] == "RESULT": outputs["P1"] = int(result != 0)
                    outputs["__userops"] = ["sass_ordered_block_barrier_" + ("state" if form["mode"] == "BAR" else "result")]
                    s.add(outputs, form, context=context,
                          hardware={"skip": "barrier state requires prior cooperative arrivals; covered by fixtures"})
        case "MEMBAR":
            if "sem" not in k.operand_types or form.get("mmio", "nomembar_mmio") != "nomembar_mmio":
                raise Unsupported("async/MMIO fence obligations unresolved")
            if form["sem"] not in ("SC", "ALL", "nomembar_sem") or form["sco"] not in ("CTA", "SM", "GPU", "SYS"):
                raise Unsupported("fence controls unresolved")
            s = Inputs(c, k, form)
            event = dict(op="MEMBAR", semantics="DEFAULT" if form["sem"] == "nomembar_sem" else form["sem"], scope=form["sco"])
            s.add(dict(__events=1, __event_values=[event], __max_ops=dict(count=4, userops=["sass_fence"])), form,
                  hardware={"skip": "SC fence events are internal model state; hardware ordering requires a cooperative fixture"})
        case "CCTL":
            if k.name == "cctl__IVALL_WBALL_D_U_noSrc" and form == dict(cache="D", cop="IVALL"):
                for predicate, invert in product((0, 1), (0, 1)):
                    s = Inputs(c, k, form)
                    s.fields.update(Pg=0, **{"Pg@not": invert})
                    s.registers["P0"] = predicate
                    events = [cache.invalidate()] if predicate ^ invert else []
                    s.add(dict(__events=len(events), __event_values=events,
                               __max_ops=dict(count=4, userops=["sass_cache_control"], branches=True)), form,
                          hardware={"skip": "cache invalidation requires a warmed dependent payload; covered by cooperative fixtures"})
                return c
            if k.name not in ("cctl__sImmOffset", "cctl__sImmOffset_pf2") or form["cache"] != "D":
                raise Unsupported("cache descriptor, predicate result or cache selector unresolved")
            if form["cop"] not in cache.OPERATIONS or c.arch == "SM75" and form["cop"] in ("PML2", "DML2"):
                raise Unsupported("cache operation unresolved")
            bits = 64 if form["e"] == "E" else 32
            for base, offset in ((0x180, -128), (0xffffff80, 128), (0x8000000000000180, -128),
                                 (0x180, 0x7fffff80), (0x180, -0x80000000)):
                s = Inputs(c, k, form)
                s.operand(("Ra",), base, 2, size=bits // 8)
                s.fields["Ra_offset"] = offset
                event = cache.event(form["cop"], base, offset, bits)
                payload = bytes((index * 37 + 11) % 256 for index in range(128))
                s.memory = [dict(space="ram", address=event["address"], hex=payload.hex())]
                s.add(dict(__events=1, __event_values=[event], __memory=payload.hex(),
                           __max_ops=dict(count=12, userops=["sass_cache_control"])), form,
                      observe_memory=[dict(space="ram", address=event["address"], size=128)],
                      hardware=dict(adapter="memory", expected=dict(__memory=payload.hex())))
        case "LDGSTS": return copies(c, k, form)
        case "LDGDEPBAR":
            for board in range(6):
                s = Inputs(c, k, form)
                s.fields["dst_wr_sb"] = board
                s.add(dict(__events=1, __event_values=[dict(op="async_commit", args=[board])],
                           __max_ops=dict(count=4, userops=["sass_async_commit"])), form,
                      context=dict(async_record=True), hardware={"skip": "commit behavior requires copy groups; covered by fixtures"})
        case "DEPBAR":
            for count in (0, 1, 63):
                s = Inputs(c, k, form)
                board = int(form.get("sbidx", "SB3").removeprefix("SB")) if "le" in form else 7
                if "le" in form:
                    s.fields["sbidx"] = f"SB{board}"
                    s.operand(("cnt", "URb"), count, 6)
                mask = 0x12 & ~(1 << board)
                s.fields["scoreboard_list"] = mask
                events = ([dict(op="async_wait_groups", args=[board, count])] if "le" in form else [])
                events.append(dict(op="async_wait_slots", args=[mask]))
                s.add(dict(__events=len(events), __event_values=events,
                           __max_ops=dict(count=8, userops=["sass_" + event["op"] for event in events])), form,
                      context=dict(async_record=True), hardware={"skip": "wait behavior requires prior dependencies; covered by fixtures"})
        case _: raise Unsupported(f"{k.mnemonic} distinguishing behavior unresolved")
    return c


def copies(c, k, form):
    from tests.harness.autocases import Inputs, Unsupported, operand_sizes
    from tests.harness.autocase_remaining import memory_event
    if form.get("sem") == "MMIO": raise Unsupported("async MMIO environment")
    width = int(form["sz"])
    if width not in (32, 64, 128): raise Unsupported("async size")
    profile = autocase_profiles.PROFILES["effect_forms"]
    descriptors = profile["descriptors"] if "Ra_URd" in k.operand_types else ((12, 0),)
    for enabled, value, (uniform, policy) in product((0, 1), profile["words"], descriptors):
        s = Inputs(c, k, form)
        size = int(operand_sizes(c.arch)[k.name].get("ILABEL_Ra_SIZE", 32)) // 8
        s.operand(("Ra",), 0x100, 4, size=size)
        s.fields.update(Ra_offset=16, Rb=8, Rb_offset=-16, Pnz=0)
        s.registers.update(R8=0x110, P0=enabled)
        for name, reg in (("Ra_URc", 6), ("Ra_URd", uniform + 1), ("Rb_URc", uniform)):
            if name not in k.operand_types: continue
            s.fields[name] = reg
            s.registers.update({f"UR{reg}": 0, f"UR{reg + 1}": 0})
        if "Ra_URd" in k.operand_types: s.registers[f"UR{uniform + 1}"] = policy
        value &= (1 << width) - 1
        s.memory = [dict(space="ram", address=0x110, hex=value.to_bytes(width // 8, "little").hex()),
                    dict(space="shared", address=0x100, hex="ff" * (width // 8))]
        s.fields["Pnz@not"] = form.get("Pnz@not") == "set"
        use = enabled ^ s.fields["Pnz@not"]
        result = asynchronous.copied(value, width, use)
        copied = int.from_bytes(result, "little")
        event = dict(op="async_issue", args=[0x100, copied & ((1 << 64) - 1), copied >> 64, width // 8, 1])
        if "Ra_URd" in k.operand_types and (error := asynchronous.descriptor_error(policy)):
            s.add(dict(error=error), form, context=dict(async_record=True), hardware=dict(adapter="async"))
            continue
        effects = ["sass_memory_order"] + (["sass_memory_descriptor"] if "memoryDescriptor" in k.operand_types else []) + [
            "sass_async_issue"]
        s.add(dict(P0=enabled, __memory="ff" * (width // 8),
                   __events=2, __event_values=memory_event(c, k, form) + [event],
                   __max_ops=dict(count=70, userops=effects, branches=True)),
              form, context=dict(async_record=True),
              observe_memory=[dict(space="shared", address=0x100, size=width // 8)],
              hardware={"adapter": "async", "expected": dict(P0=enabled, __memory=result.hex())})
    return c
