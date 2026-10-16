---
name: sass-opcode-semantics
description: Investigate a SASS opcode in nvdisasm-re (including CuLifter opcodes) by tracing PTX through ptxas, checking functional behavior against a GPU, and implementing or validating Ghidra P-code. Use for opcode semantics, modifier bits, opcode variants and PTX-to-SASS evidence.
---

# SASS opcode semantics

Produce reproducible evidence connecting a PTX probe, a specific SASS encoding,
and its value semantics. Add P-code when implementation is part of the user's
request; an explanation or compiler trace alone does not require changing the
semantics implementation.

Work from the user's `nvdisasm-re` checkout and read its `AGENTS.md`: its
"Writing new semantics" section is the authoritative implementation, test, build
and verification order, and this skill does not repeat it. For a completed example,
read [references/bmsk.md](references/bmsk.md).

Label every finding as either **semantics** (the decoded operation, implementation
or expected value is wrong or unresolved) or **testing/environment** (a probe was
not generated, did not execute the target, had a harness/build/device problem, or
lacked coverage). A test failure alone does not establish a semantics bug; classify
a mismatch only after checking probe construction, target execution, data layout
and reference independence.

## Establish the target and scope

- Select an opcode from `out/SMxx/md/OPCODE.txt` and the parsed md. Record the
  architecture, every matching non-alias class, operand order and roles, widths and
  register spans, encoded fields, enum values, defaults, selector tables, predicate
  outputs, carry inputs/outputs, and which forms appear in real cubins.
- Treat the mnemonic as a family of forms and build a form matrix: for each form,
  its disposition (native P-code, primitive, opaque, uninvestigated) and why. One
  form's evidence never stands in for another's.
- Pin the compiler path, version and SHA256 before using recorded addresses. The
  BMSK example uses CUDA 13.0.88 ptxas; the extracted nvdisasm tables come from
  another build. Keep those identities separate.
- Query the active Ghidra program/server (the user's URL, not a remembered port).
  Function lookups require exact entries. Confirm the image base: do not apply
  nvdisasm's address adjustment to ptxas.
- Check primary NVIDIA documentation (PTX ISA, CUDA-GDB, the Debugger API) before
  assuming a detail is undocumented, and state which detail is still uninvestigated.

## Obtain a controlled PTX-to-SASS pair

Write a small kernel that loads runtime inputs, performs the operation and stores
its result. Keep inputs live and the result observable so optimization cannot erase
the operation; use thread-dependent inputs to prevent a uniform opcode alias.

Change one semantic selector at a time. Compare the target 128-bit words and
decoded operands, accounting for register allocation and scheduling; claim that a
bit selects a mode only when the evidence isolates that bit. Record word bytes as
little-endian and bit numbers from `int.from_bytes(word, 'little')`.

Label compiler-generated, manually patched and synthetic samples separately: a
disassembler-accepted synthetic word does not prove ptxas emits it.

## Trace the live compiler path

Start with the ORI conversion dispatcher or an established target-class handler,
then follow operand construction and the final encoder (in the demonstrated build:
`dispatch_ori_opcode_conversion`, the operation lowerer, the target builder, the
finalizer directory, the native encoder).

- Use Ghidra to form hypotheses; confirm the path with live GDB breakpoints.
- Verify the requested architecture hits the handler: a shared-looking dispatcher
  can be bypassed on another architecture.
- Extend `research/ptxas/capture_finalizer.py` narrowly for the ORI opcode if
  needed, and use a fresh capture path (the script appends records).
- Validate the capture against the cubin from the same compiler run:

  ```sh
  python3 research/ptxas/validate_capture.py CAPTURE.jsonl OUTPUT.cubin
  ```

An encoder establishes bit placement and operand identity; functional semantics
need a specification, an informative lowering, or execution evidence. Preserve that
distinction. Rename functions only after their roles are established; keep
addresses and build identity in the research notes.

## Establish value semantics independently

Follow AGENTS.md step 1: the reference lives in `tests/refs/`, independent of the
code under test. For BMSK, enumerate selected output bits rather than duplicating
the shift formula. Include boundaries that distinguish plausible interpretations,
and cover count/width limits, signedness, zero registers, predicates, source/
destination aliases, discarded results, and each supported operand form. Record
unresolved forms individually.

For GPU fixtures (AGENTS.md step 5), verify the target instruction and selector are
present or document exactly which word or fields were patched, preserving the
kernel wrapper and its scheduling/control fields. Check the emulator visited the
target. Investigate scheduling hazards before calling a patched-word mismatch a
semantics failure. Report the architecture, device, compiler, form matrix, vector
count, seeds and any normalizations or exclusions.

Report hardware support only for the tested device and forms; an all-architecture
emulator pass does not establish all-architecture hardware behavior. If the GPU
cannot run (device, permissions, compile or harness failure), say why, finish the
emulator and static checks, and mark hardware validation pending. Never claim a
hardware pass from emulator coverage, or report a cancelled run as a pass.

## Implement P-code when requested

Semantics are families in `src/sass/semantics/families/` (Python over the
md-decoded instruction, `family.py`), lowered by `src/sass/sleigh/lower.py` and
lifted Ghidra-free by `lifter.py`; `operations.py` only routes classes to families.
Typed operand values come from `operands.py`.

- Ask every selector the semantics depend on (`i.select_fork`, `i.select_lookup`);
  an unasked selector models only its md default and the rest stays opaque.
- Call `unmodelled(reason)` for unresolved combinations and `primitive()` for
  runtime-executed ones. Give a specific reason: it becomes the coverage reason and
  is a limitation of the family, not evidence that hardware or ptxas lacks the form.
- Outputs are written simultaneously after all inputs are read. Model predicate
  outputs and carry chains individually (conditional, complementary, or preserving
  old state). The backends handle hardwired zeros, guards, register spans and md
  conditions.

## Build, verify and record

Build, test and refresh coverage as AGENTS.md describes, focused checks first.
Inspect what a command's arguments mean before launching an expansive suite, and
stop one that is unexpectedly broad or slow. Add tests rather than recording
verification results by hand.

Record the compiler trace in `research/ptxas/ptxas-finalizer-notes.md`, semantic
scope in `docs/semantics.md`, and hardware-established behavior in
`docs/hardware-facts.md`. Keep unrelated work intact.

Report the PTX-to-SASS mapping, the functional rule, implementation scope, the
tests with their scope, remaining fallbacks, any failures labelled semantics or
testing/environment, and the exact commands to reproduce. Do not claim an opcode
family is fully supported when only a subset of its selectors was verified.
