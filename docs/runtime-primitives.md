# Runtime primitives and effects

Reference for the calls used by instruction semantics, checked against the implementation
on 2026-11-05. A runtime primitive obtains a value from the execution environment.
An effect changes or validates environment state and returns no instruction value.
Pure numerical definitions, such as an atomic update calculation, are separate:
they expand into calculations in the intermediate representation (IR).

The call names below are the names emitted into Ghidra's p-code. Native code lowers
them to C++ helpers or checks. Python and Ghidra callbacks dispatch through
`src/sass/semantics/runtime.py`. The builders are in `src/sass/semantics/primitive.py`;
memory, atomic and asynchronous families also construct effects directly.

## Execution and context

A **warp** is a group of up to 32 GPU threads. A **lane** is a thread's position
within that group, numbered 0–31. A lane mask is a 32-bit integer with bit `n`
representing lane `n`. The active mask here identifies threads executing this
instruction, rather than all threads still alive.

Standalone evaluation supplies `Runtime(sm, context, handlers)`. A handler is an
override keyed by the full call name; it receives `(arguments, output_bytes, context)`.
Results are integer bit patterns; `output_bytes=0` denotes an effect.
Unknown calls and missing required context fail with `UnsupportedSemantics` or a
native `Fault`, rather than supplying invented values.

The shared kernel scheduler prepares per-instruction snapshots and pending-operation
state for all three engines. Providing a standalone context does not implement
scheduling, synchronization or memory ordering by itself.

The current instruction execution order is: check the instruction guard, execute
effects in their listed order, calculate register outputs/store addresses/store
values/control flow, then apply register writes, memory writes and control flow.
Effects can therefore change memory before those calculations. There is no general
sequence of interleaved reads and writes in the IR. Ordinary memory accesses use
the sequentially consistent execution model described in [emulation.md](emulation.md);
pending asynchronous copies follow their separate completion rules below.
`Store.atomic` is currently descriptive metadata: the execution loop prevents
another thread from interleaving within an instruction, not the flag itself.

## Warp value primitives

All names in this table have the prefix `sass_prim_`. `x` and `p` are the current
lane's unmodified input value and predicate. Other lanes' corresponding inputs
come from the snapshot. A predicate is a true/false input.

| Name and arguments | Result | Required context and failures |
| --- | --- | --- |
| `lane()` | 32-bit current lane index. | `context.warp`, with a valid executing lane. |
| `active()` | 32-bit executing-lane mask. | Same warp context. |
| `waiting()` | 32-bit mask of live lanes not executing this instruction. | Explicit `context.warp.waiting`; missing mask fails. |
| `read_lane(x, source)` | The selected lane's captured value; IR result width follows `x`. | 32 entries in `warp.values`, valid current lane and source 0–31. This primitive does not check whether the source is active; the SHFL instruction semantics handle inactive sources and bounds. |
| `operand_lane(group, source, word[, current])` | One captured 32-bit register word. If supplied, `current` replaces the returned word when `source` is the current lane. | `warp.operand_groups`, valid group/source/word, executing source. The snapshot is required even with `current`. Invalid or inactive sources fail. |
| `ballot(p)` | 32-bit mask of active lanes with nonzero predicates. | 32 entries in `warp.predicates` and valid warp context. |
| `match(x)` | 32-bit mask of active lanes whose values equal this lane's value. | 32 entries in `warp.values`; input is 32 or 64 bits. |
| `redux_and(x)`, `redux_or(x)`, `redux_xor(x)` | 32-bit bitwise reduction over active lanes. | 32 entries in `warp.values`; valid nonempty active set. |
| `redux_add(x)` | 32-bit sum over active lanes, wrapping on overflow. | Same reduction context. |
| `redux_umin(x)`, `redux_umax(x)` | 32-bit unsigned minimum/maximum over active lanes. | Same reduction context. |
| `redux_smin(x)`, `redux_smax(x)` | 32-bit signed minimum/maximum over active lanes. | Same reduction context; values interpreted as signed 32-bit integers. |

`context.warp` contains `lane`, `active`, and the inputs required by the particular
primitive. Standalone Python context defaults the lane to 0 and active mask to all
32 lanes when a warp context is present; consumers should supply both explicitly.
`read_lane`, `match` and reductions substitute `x` for the current lane's snapshot
entry; `ballot` substitutes `p`. They neither rendezvous threads nor order memory.
Source modifiers such as negation are applied by instruction semantics at the
appropriate point, not inferred by these primitives.

`operand_lane` group numbers follow `OPERAND_GROUPS`, in this order:
`Ra`, `Rb`, `Rc`, `Re`, `indexURc`, `Ra_URb`, `Ra_URc`, `Rb_URc`.
These are operand names, not absolute register numbers. The Python context maps
each name to 32 rows of register words; the native ABI requires equal row lengths
of 1–8 words. The scheduler captures participating inputs before any lane writes,
including overlapping source/destination registers. Having groups for indexed
operands does not establish support for indexed HMMA forms.

## Barrier value primitives

A **barrier** tracks arrivals by a specified set of threads. A completed barrier
may also provide a result, such as the number of arriving threads whose predicate
was true. A **generation** is one use of a barrier ID, from arrival to completion.

| Full name and arguments | Result | Required context and failures |
| --- | --- | --- |
| `sass_prim_bar_result()` | 32-bit last barrier result for this thread. | Standalone `context.barrier_result`; absent value fails. Cooperative execution starts at zero and updates it through the scheduler. |
| `sass_prim_bar_state(index)` | 32-bit packed pending state of barrier ID `index`. | `context.barrier_states` containing exactly 16 entries; index 0–15. Missing state or invalid index fails. |
| `sass_prim_bmov(state, inputs...)` | 32-bit environment-supplied convergence-barrier value. | Standalone `context.synchronization.bmov` mapping the first argument's state encoding to a result. Missing mapping/value fails. General cooperative BMOV behavior remains unresolved. |

The scheduler's packed BAR state contains the operation tag, expected warp count
(zero for an implicit count), accumulated predicates and arrived warp count.
Completed pending states clear. Blocking and deferred participants receive the
completed result; earlier nonblocking arrivals retain their prior result, while
the completing warp receives the new result. See [the barrier model](emulation.md#operand-groups-barrier-results-and-async-copies-2026-11-03)
and [hardware findings](hardware-facts.md) for the established forms and evidence.
Reading a result/state neither arrives at a barrier nor waits for it.

## Synchronization effects

The full names are `sass_prim_bar`, `sass_prim_bssy`, `sass_prim_bsync`,
`sass_prim_break`, `sass_prim_warpsync` and `sass_prim_bmov`.
Each effect has arguments `(enable, inputs...)` and no result. A false `enable`
suppresses the effect. The separate value-returning BMOV call is listed above.

The remaining argument schema is instruction-class dependent. The family emits
inputs in machine-description operand order: selector encodings for multi-valued
selectors, operand values, and resolved branch targets. The instruction guard is
excluded; the enabling predicate is passed first. BAR reductions keep their
reduction predicate among the inputs and use an unconditional enable.
`synchronization_inputs` in `families/runtime.py` defines this schema.

| Effect | Cooperative execution | Unsupported or limited behavior |
| --- | --- | --- |
| `bar` | Block-local arrival, waiting and reduction/result state for supported BAR forms. IDs can be reused after completion. | Packed ID/count producers and overlapping arrival generations remain unresolved. A deferred blocking arrival allows coupled instructions to continue, then gates a later decoupled instruction. |
| `bssy` | Captures enabled live participants under a warp-local convergence ID. | Participating-thread state comes from the scheduler, not a value-only primitive. |
| `bsync` | Holds arrivals until remaining live captured participants arrive; exited threads are removed. Resumes at the next instruction. | It does not jump to the earlier BSSY target. |
| `warpsync` | Holds named live lanes until matching masks arrive. | Exited lanes are excluded; mismatched masks or a lane omitted from its own mask fail. Only established selector/forms are supported. |
| `break`, `bmov` | No general cooperative implementation established. | Unresolved forms remain explicit errors. |

Standalone `context.synchronization` can acknowledge an enabled effect by a truthy
entry under its unprefixed name. Python records the acknowledged event; native
evaluation checks the acknowledgement. This is **not** a barrier/scheduler
implementation and does not cause other threads to arrive or memory to become
visible. Cooperative execution supplies the actual scheduler transitions.
An acknowledged BAR alone does not complete pending asynchronous copies.

## Hardware approximation primitives

**MUFU** is the SASS instruction family for hardware approximations to mathematical
functions. These primitives take and return floating-point bit patterns, not
host-language floating-point numbers. No warp snapshot or mutable state is needed.

Full names: `sass_prim_mufu_cos`, `sass_prim_mufu_sin`, `sass_prim_mufu_ex2`,
`sass_prim_mufu_lg2`, `sass_prim_mufu_rcp`, `sass_prim_mufu_rsq`,
`sass_prim_mufu_rcp64h`, `sass_prim_mufu_rsq64h`, `sass_prim_mufu_sqrt`,
`sass_prim_mufu_tanh`.

| Arguments/result | Supported calculations | Failures and evidence limits |
| --- | --- | --- |
| `(x)` → 32-bit result | F32 SIN/COS (sine/cosine), RCP (reciprocal), RSQ (reciprocal square root). | EX2, LG2, SQRT and TANH remain unresolved. Models are hardware-established on SM89; generation on another architecture is not hardware verification there. |
| `(x)` → 32-bit result for `rcp64h`/`rsq64h` | Accepts the high word of a 64-bit floating-point input, supplies a zero low word, returns the high word of the modeled result. | Precision/exception rules are those of the recovered approximation, not an assumed host-library calculation. |
| `(x, format)` → 16-bit result | `format=0` denotes F16; RCP/RSQ are implemented. | `format=1` denotes BF16 and remains unresolved; other 16-bit functions also fail. |

The instruction family applies source modifiers and selects/packs half-register
inputs and outputs. Merely having a named primitive does not mean every format is
implemented. See [semantics.md](semantics.md#11-runtime-primitives) and
[hardware-facts.md](hardware-facts.md) for numerical behavior and verification domains.

## Address-space and operand queries

| Full name and arguments | Result | Context, effects and failures |
| --- | --- | --- |
| `sass_prim_address_space(address)` | 32-bit tag: global `1`, shared `2`, local `4`, cluster shared `8`; shared plus cluster shared may return `10`. Input is 64 bits. | Requires `context.address_spaces`, mapping space names to lists of half-open `[start, end)` ranges. Missing/malformed ranges, unmapped addresses or ambiguous matches fail. No memory access or ordering action. |
| `sass_operand_value(class_ordinal, operand_index, children...)` | Environment-supplied operand value, with width set by its IR use. | Standalone Python/Ghidra resolves `context.operands["class.operand"]`; missing value fails. It does not infer stateful operand layouts. This is an operand-access hook, not a `sass_prim_` builder. |

Address-space range names are `global`, `shared`, `local`, `shared_cluster`.
The address-space primitive identifies mappings; the instruction family decides
whether the requested architectural form and destination are supported.

## Memory validation and fence effects

These effects have no result and do not themselves read or write memory.

| Full name and arguments | Behavior | Failures and ordering |
| --- | --- | --- |
| `sass_memory_order(class_ordinal, controls...)` | Validates encoded controls against the architecture's instruction class. Controls are present fields from `sem`, `sco`, `cop`, `cop2`, `private`, in that order. | `INVALID` control labels fail. Python records an event; native lowering validates constant controls during code generation. This call does not implement weak-memory behavior. |
| `sass_memory_descriptor(descriptor[, packed_async])` | Validates a 64-bit cache-policy descriptor; optional `packed_async` defaults to false. The family shifts a 32-bit packed descriptor into the high word first. | Bit 59 (range-form descriptor) is rejected because its full trap behavior is unknown. Zero packed-async descriptors also fail. Accepted descriptors do not change the value accessed. |
| `sass_fence(semantics, scope)` | Validates semantics `0=SC`, `1=ALL`, `2=DEFAULT`; scope `0=CTA`, `1=SM`, `2=GPU`, `3=SYS`. Python records a MEMBAR event. | Out-of-range controls fail. Supported obligations are already satisfied by the current ordinary-memory model. The family rejects unresolved async/MMIO forms; this effect does not drain async-copy queues. |

Scope denotes a thread block (CTA), a streaming multiprocessor (SM), the GPU, or
the system. MMIO means memory-mapped device input/output, whose environment behavior
is not provided by the ordinary-memory model. Numeric controls here are our effect
encodings, not universal SASS bit encodings.

## Asynchronous-copy effects

An asynchronous copy transfers global-memory data into block shared memory while
the issuing thread can continue. Each thread owns an open list of copies.
A **group** is that list after a commit closes it, including an empty list.
A **scoreboard** here is one of six numbered dependency queues, 0–5, used to track
committed groups; this does not model all hardware scoreboards.

| Full name and arguments | State change | Context and ordering |
| --- | --- | --- |
| `sass_async_issue(destination, low, high, size, enable)` | If enabled, appends captured bytes and their shared destination to the open list. `low`/`high` are 64-bit words; `size` is bytes. | Requires per-thread async state. The LDGSTS family supports sizes 4/8/16 and evaluates its global-memory read before calling this effect. No shared write occurs here. |
| `sass_async_commit()` | Moves the open list into a committed group, clears the open list and marks its scoreboard pending. | Uses the instruction's encoded write dependency through runtime context; it is not a call argument. A missing/invalid scoreboard (not 0–5) fails. Commit does not write shared memory. |
| `sass_async_wait(board, count, mask)` | For board 0–5, completes oldest groups until at most `count` newest groups remain. Then drains boards named by `mask`, and writes all ready copy bytes to shared memory. | Requires async state. Other board values skip the counted wait; the family uses `7` for mask-only waits. Only pending boards 0–5 participate in the mask. Completed bytes become visible before later output calculations. |

The low word supplies the first eight bytes, the high word the next eight,
least-significant byte first. Instruction predication can suppress the whole
instruction; LDGSTS's source predicate separately controls the supported zero-fill
behavior. These controls are resolved by the family before issue.

Encoded instruction wait masks are also handled by the scheduler, before the
instruction's guard/effects. Python and Ghidra callbacks use the same C++ queues
and transfer completed bytes into their engine's shared memory. A standalone
`context.async` supplies the corresponding `issue`, `commit`, `wait` methods.
`context.async_record` instead records calls for inspection; it neither queues
copies nor establishes completion.

Source bytes are captured at issue and visibility is delayed until required
completion. This is a deterministic emulator policy, not a claim about GPU transfer
latency or the outcome of racing accesses. BAR alone does not drain the queues.

## Cache-control effect

`sass_cache_control(operation, address)` has no result. A cache holds copies of
memory for faster reuse; this effect records the requested cache action while
ordinary reads still use the emulator's shared memory state.

| Operation | Meaning in the model | Validation |
| --- | --- | --- |
| `0=PF1`, `1=PF2` | Records a prefetch request (request to fetch ahead of use). | The instruction family restricts supported forms; no simulated cache contents. |
| `2=PML2`, `3=DML2` | Records a cache-priority request. | DML2 address must be aligned to 128 bytes. |
| `4=IVALL` | Records whole-data-cache invalidation and increments the cache state's generation counter. | Address must be zero. |

Unknown operation numbers fail. Native execution uses the supplied cache state or
its memory object's state; the shared scheduler supplies launch-wide state in
launch scope. Standalone Python records an event and validates controls even without
`context.cache`; providing cache state also tracks requests/generations.
Predication gates the effect. There is no simulation of cached bytes, stale reads,
eviction timing or cache performance. Other architectural cache forms remain
unresolved, even if a related request number exists here.

## Unsupported instruction calls

`sass_opaque_OPCODE(inputs...)` preserves instruction inputs for forms without
supported semantics. Default execution raises explicitly. A supplied handler can
return the encoded output bundle, but its behavior is external to our established
semantics. These calls are not implemented primitives; their register bundle
convention is documented in [the runtime ABI](semantics.md#13-runtime-abi).

## Maintenance

When adding or changing a runtime call, update its signature, result width, required
context, state transitions, ordering and failure behavior here. Cross-check
`primitive.py`, `runtime.py`, family-generated effects, `emulate/cxx.py` and
`emulate/include/sass/{machine,primitives,async,cache}.hpp`. Keep architecture support
and hardware evidence separate. A handler override or acknowledged standalone event
does not establish a cooperative implementation.
