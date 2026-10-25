"""SM89 GPU probe of BRA.DIV/BRA.CONV URb: which (executing lanes, URb mask) pairs take the branch,
with the inactive lanes exited or alive at a convergence barrier (`waiting`). Raw observations, no
reference; tests/refs/control.divergence_branch is derived from them.
usage: uv run python -m research.bra_div.probe OUT.json"""
import itertools, json, sys
from tests.harness.case import Cases
from tests.harness import device_cases

FULL, LOW, BYTE, HIGH, MID = 0xffffffff, 0x0000ffff, 0x000000ff, 0x00ff0000, 0x00ffff00
# (executing lanes, URb mask, observer lane)
SCENARIOS = [(FULL, FULL, 0), (FULL, LOW, 0), (FULL, 0, 0), (LOW, FULL, 0), (LOW, LOW, 0), (LOW, BYTE, 0),
             (LOW, HIGH, 0), (LOW, MID, 0), (LOW, 0, 0), (LOW, 0xff00, 0), (0x1, 0x1, 0), (0x1, 0x3, 0)]


def probes():
    c = Cases("SM89")
    for (active, mask, lane), wait, cond, invert in itertools.product(SCENARIOS, (0, 1), ("DIV", "CONV"), (0, 1)):
        if active == FULL and wait: continue
        c.probe("bra_uniform_", {"cond": cond, "URb": 6, "URb@invert": invert, "sImm": 32}, {"UR6": mask},
                observe=["__counter"], context={"warp": dict(lane=lane, active=active, waiting=~active & FULL if wait else 0)}, hardware={"adapter": "warp"})
    for cond, pp in itertools.product(("DIV", "CONV"), (0, 1)):   # Pp false (P0 = 0, maybe inverted)
        c.probe("bra_uniform_", {"cond": cond, "URb": 6, "Pp": 0, "Pp@not": pp, "sImm": 32}, {"UR6": FULL, "P0": 0},
                observe=["__counter"], context={"warp": dict(lane=0, active=LOW, waiting=0)}, hardware={"adapter": "warp"})
    return c


if __name__ == "__main__":
    c = probes()
    out = device_cases.observe(c)
    for case, o in zip(c, out["observations"]): o["context"] = case.request["context"]
    json.dump(out, open(sys.argv[1], "w"), indent=1)
