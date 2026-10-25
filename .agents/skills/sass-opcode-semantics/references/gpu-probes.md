# Generated SM89/SM90 GPU probes

Use these for single-instruction discovery without a known reference. The generated
runner shares register initialization, scheduling, constant/memory bindings, warp
setup and fault isolation with the regression tests. It returns raw bits without
NaN tolerances or expected-value comparisons. It needs an SM89 or SM90 GPU, not Ghidra or
nvcc; `Cases(arch)` (or `tests.probe --arch`) must match the device's architecture. Run hardware commands outside the sandbox, as required by `AGENTS.md`.
`tests/README.md` "Hardware evidence and probes" summarizes the same tools and how
passing shared-case runs become evidence.

## Python API

```python
import json
from tests.harness.case import Cases
from tests.harness import device_cases

c = Cases("SM89")
for width in (0, 1, 31, 32, 33):
    c.probe("bmsk__RRR_RRR", dict(Rd=8, Ra=2, Rb=4, cw="C"),
            dict(R2=1, R4=width), observe=["R8"])
print(json.dumps(device_cases.observe(c), indent=1))
```

`Cases.probe` accepts the same class, fields, register inputs and optional memory,
context and hardware setup as `Cases.add`; observations are explicit and no
reference is supplied. `observe` can also re-run existing cases without consulting
their expected values. Results include the device, architecture, exact input word,
disassembly, observed registers and memory hex. Worker faults return an error for
the affected probe; they are not values to fit a reference to.

## JSON CLI

Save a list of `Cases.probe` arguments:

```json
[
  {
    "cls": "bmsk__RRR_RRR",
    "fields": {"Rd": 8, "Ra": 2, "Rb": 4, "cw": "C"},
    "registers": {"R2": 1, "R4": 3},
    "observe": ["R8"]
  }
]
```

```sh
uv run python -m tests.probe /tmp/probes.json --out /tmp/observations.json
```

Without `--out`, stdout is JSON. A device fault is recorded in the output and makes
the CLI exit nonzero. Keep the input JSON and observations with the investigation
when they establish a new behavior; label the words as synthetic rather than
compiler-generated unless a PTX compilation independently establishes that mapping.

## Memory and warp setup

For memory, pass `hardware={"adapter": "memory"}` and the existing request fields:
`memory=[{"space": "ram", "address": 256, "hex": "..."}]` initializes logical
storage; `observe_memory=[{"space": "ram", "address": 256, "size": 16}]` captures
it. Shared and local storage use `shared` and `localmem`. The adapter binds logical
pointers to real allocations and runtime windows; preserve the register/offset
relationship in the instruction. Use `.E` for global pointers that must hold CUDA's
64-bit address. CX backing is recognized automatically from the encoded operand.

`hardware={"adapter": "warp"}` materializes `context={"warp": ...}` as real lanes,
using the same shape as `tests/cases/threads.py`: 32 source values or predicates,
observer lane, and active mask. Prefer existing family cases as setup examples.
Model-only state and unsupported bindings fail explicitly; do not change the
instruction's operand form merely to make an existing probe run.

## Promote a finding to a regression

Vary one selector at a time and choose inputs that distinguish plausible rules.
Observe aliased registers and all relevant predicates, not just the main result.
Write an independent reference from the evidence, then replace the draft
`c.probe(..., observe=[...])` with `c.add(..., expected=reference(...))` in the
appropriate family's `emulator_cases`. Draft probes are rejected in regression
suites, and raw observations never create GPU verification evidence. Run the
shared GPU tests (`nice uv run pytest -m gpu --require-gpu --family F`) to validate
the reference and record passing evidence; each word counts once all its scenarios pass. Then run the
Python/Ghidra checks and refresh coverage as `AGENTS.md` requires.

Keep whole-kernel `gpu_fixtures()` and `tests.observe` for PTX/compiler comparisons,
integration kernels, synchronization and scenarios that need more than one target
instruction. Generated probes establish only the tested forms and inputs, on the architecture that ran them.
