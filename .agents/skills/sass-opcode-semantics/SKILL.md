---
name: sass-opcode-semantics
description: Investigate a SASS opcode in nvdisasm-re by tracing PTX through ptxas, checking functional behavior against a GPU, and implementing or validating Ghidra P-code. Use for opcode semantics, modifier bits, and PTX-to-SASS evidence.
---

# SASS opcode semantics

Produce reproducible evidence connecting a PTX probe, a specific SASS encoding,
and its value semantics. Add P-code when implementation is part of the user's
request. An explanation or compiler trace alone does not require changing the
repository's semantics implementation.

Work from the user's `nvdisasm-re` checkout and read its `AGENTS.md`. The files
and commands below are relative to that checkout. For a completed example,
read [references/bmsk.md](references/bmsk.md).

## Establish the target and scope

- Select an opcode from `out/SMxx/md/OPCODE.txt`. Record the architecture,
  class names, input/output roles, widths, selectors, and encoded fields.
  Distinguish the mnemonic from its operand variants and modifier combinations.
- Pin the compiler path, version and SHA256 before using recorded addresses.
  The BMSK example uses CUDA 13.0.88 ptxas; the repository's extracted nvdisasm
  tables come from another build. Keep those identities separate.
- Query the active Ghidra program/server. Use the user's server URL, not a port
  remembered from a previous session. Function lookups require exact entries.
  Confirm the image base: do not apply nvdisasm's address adjustment to ptxas.
- Check primary NVIDIA documentation before assuming a semantic detail is
  undocumented. PTX ISA, CUDA-GDB and the CUDA Debugger API can each supply
  relevant evidence. State precisely which detail is still uninvestigated.

## Obtain a controlled PTX-to-SASS pair

Write a small kernel that loads runtime inputs, performs the operation and
stores its result. Compile and inspect the actual kernel text. Keep inputs live
and the result observable so optimization cannot erase the target operation.
Use thread-dependent inputs when necessary to prevent a uniform opcode alias.

Change one semantic selector at a time. Compare the target 128-bit words and
decoded operands. Account for register allocation and scheduling differences;
only claim that one bit selects a mode when the evidence isolates that bit.
Record word bytes as little-endian bytes, and bit numbers from the integer
formed with `int.from_bytes(word, 'little')`.

Do not treat a disassembler-accepted synthetic word as proof that ptxas emits
it. Label compiler-generated, manually patched and synthetic samples separately.

## Trace the live compiler path

Start with the ORI conversion dispatcher or an established target-class
handler, then follow operand construction and the final encoder. Useful stages
in the demonstrated build are `dispatch_ori_opcode_conversion`, the operation
lowerer, the target builder, the finalizer directory, and the native encoder.

- Use Ghidra to form hypotheses about dispatch cases, builder slots, operand
  roles and selectors. Confirm the relevant path with live GDB breakpoints.
- Verify the requested architecture hits the handler. A shared-looking
  dispatcher can be active on one architecture and bypassed on another.
- Extend `research/ptxas/capture_finalizer.py` narrowly for the selected ORI
  opcode when needed. Inspect its existing breakpoint/address assumptions.
- Capture lowering, operand materialization, native dispatch and encoded words.
  Use a fresh capture path: the script appends records.
- Validate the capture against the cubin produced by that same compiler run:

  ```sh
  python3 research/ptxas/validate_capture.py CAPTURE.jsonl OUTPUT.cubin
  ```

An encoder establishes bit placement and operand identity. Functional semantics
need a specification, an informative lowering, or execution evidence. Preserve
that distinction in comments and conclusions. Rename functions only after their
roles are established; keep addresses and build identity in the research notes.

## Establish value semantics independently

Write a reference calculation independently of the proposed P-code expression.
For BMSK, enumerate selected output bits rather than duplicating its shift
formula. Include boundaries that distinguish plausible interpretations, not
just random ordinary inputs.

Cover applicable count/width limits, signedness, zero registers, predicates,
source/destination aliases, discarded results, and supported operand forms.
Record unresolved forms individually rather than inferring them from another
form's success.

Add GPU fixtures to the family's `gpu_fixtures()` in `tests/cases/<family>.py`
(`tests/harness/gpu.py` `Fixture`/`Driver`, with `tests/harness/scalar.py` and
`tests/harness/templates.py` for compiled and patched kernels). Verify the target
instruction and selector, then compare GPU, reference and P-code outputs using the
same input vectors; `python -m tests.observe <family>` shows raw GPU outputs for a
draft fixture (`expected=None`).
Check that the emulator actually visited the target instruction. When testing
a patched instruction, preserve the kernel wrapper and document exactly which
word or fields were changed.

Report hardware support only for the tested device architecture and forms.
An all-architecture emulator pass does not establish all-architecture hardware
behavior. Keep seeds, vector counts and executable fixtures with the evidence.

## Implement P-code when requested

The repo's generator is `sass/gen_sleigh.py`; semantics are families in
`sass/families/` (Python over the md-decoded instruction, `sass/sem_family.py`),
lowered to SLEIGH by `sass/sem_sleigh.py` and lifted without Ghidra by
`sass/sem_lift.py`. Explain these names if switching from discussing NVIDIA's
compiler to the repo.

Write or extend the opcode's `@family`. Ask every selector the semantics depend
on (`i.select_fork`, `i.select_lookup`); an unasked selector models only its md
default, the rest stays opaque. Call `unmodelled(reason)` for unresolved
combinations and `primitive()` for runtime-executed ones. Outputs are written
simultaneously after all inputs are read; the backends handle hardwired zeros,
guards, register spans and md conditions.

Give rejected forms specific reasons (they become coverage reasons). A reason
is a limitation of the family, not evidence that the GPU or ptxas lacks the
instruction.

Add value cases to the family's `emulator_cases()` in `tests/cases/<family>.py`
and independent references to `tests/refs/` (which must not import the generated
semantics). Prefer `tests/harness/case.py` helpers (`Cases.add`, `put`, `cx_source`)
over a second decoding or execution framework.

## Build, verify and record

Build the affected language first, then the architectures and exact/readable
variants supported by the change. Use `sass.build_languages` so unused
temporaries and Ghidra's decompressed SLA size limit are checked.

Complete generation and compilation before parallel language loading. Ghidra
may automatically compile stale specifications; concurrent writers can corrupt
a `.sla`. For isolated output, use `SASS_SLEIGH_OUT` and copy the shared
`.sinc`, `.pspec` and `.cspec` as described in `AGENTS.md`.

Typical checks, adjusted to the actual scope:

```sh
python3 -m sass.build_languages SM89
python -m tests.semantics SM89 exact --family <family>
uv run --with pytest python -m pytest tests -m ghidra --arch SM89 --fp all
uv run --with pytest python -m pytest tests -m gpu --require-gpu --family <family>
python3 -m sass.coverage processor/SASS/data/languages --output processor/SASS/semantics/coverage.json
git diff --check
```

Run focused value tests across every architecture whose support is claimed.
Run the relevant hardware oracle when hardware evidence is available. Use the
permission mechanism for device access or ptrace if the sandbox requires it;
do not convert an unavailable GPU into a claimed hardware pass.

Record the trace in `research/ptxas/ptxas-finalizer-notes.md`, semantic scope in
`processor/SASS/semantics/README.md`, and measured results in
`processor/SASS/semantics/verification.json`. Refresh coverage from generated
manifests. Preserve earlier results with their dates and scopes; do not relabel
old tests as newly run. Keep unrelated work intact.

Report the verified PTX-to-SASS mapping, functional rule, implementation scope,
tests and remaining uncertainty. Answer questions about the currently discussed
bit or function directly before introducing another operand variant.
