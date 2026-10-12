---
name: culifter-opcode
description: Incorporate a CuLifter SASS opcode into nvdisasm-re by identifying its machine-description forms, deriving value semantics, implementing selector-aware Ghidra P-code, and validating it with emulator and GPU probes when available. Use whenever adding, fixing, or investigating CuLifter opcode semantics or opcode variants in this repository.
---

# Incorporate a CuLifter opcode

Use this workflow to turn an opcode's decoded forms into tested P-code semantics. Work in the user's checkout and read the repository `AGENTS.md` first. Preserve unrelated changes and report progress during long builds, GPU runs, or investigations.

Always label findings as either **semantics** or **testing/environment**:

- A semantics issue means the decoded operation, implementation, or expected value is wrong or unresolved.
- A testing/environment issue means a probe was not generated, did not execute the target, had a harness/build/device problem, or lacked sufficient coverage. A test failure alone does not establish a semantics bug.

## 1. Establish the opcode's complete shape

Inspect the architecture's `out/SMxx/md/OPCODE.txt`, parsed md data, generated IR, and latency connectors. Record:

- Architecture and every matching non-alias class.
- Operand order, input/output roles, register widths and spans.
- Encoded fields, enum values, defaults, and selector tables.
- Predicate outputs, guards, carry inputs/outputs, and special-register effects.
- Which forms appear in real cubins, where available.

Treat the mnemonic as a family of forms. Enumerate relevant selector combinations explicitly, including modifiers such as signedness, rounding, saturation, flush behavior, lane selection, comparison mode, source type, and predicate combination. Consult the md and primary documentation before assuming that a selector is reserved or irrelevant.

Build a form matrix. For each form, record its current disposition: native P-code, primitive, opaque, or uninvestigated. State why unsupported forms remain unsupported. Do not let one form's evidence silently stand in for another.

## 2. Derive behavior with independent evidence

Use available sources in this order as appropriate: architecture/PTX documentation, compiler-generated PTX-to-SASS examples, controlled synthetic encodings, and execution results. Keep distinct whether an instruction was emitted by `ptxas`, manually patched into a kernel, or only decoded synthetically.

Write an independent reference calculation before relying on the implementation under test. Choose inputs that distinguish plausible interpretations, and cover boundaries, zero registers, aliases, predicate combinations, signed and unsigned values, and output preservation where applicable.

When using compiler probes, keep inputs runtime-dependent and outputs observable. Change one selector at a time, inspect the resulting SASS word and decoded operands, and avoid inferring a bit's meaning from samples where register allocation or another field also changed.

## 3. Implement with explicit selector coverage

Add or extend the family in `sass/operations.py`; obtain typed operand values through `sass/semantic.py`. Reuse existing generator and test helpers rather than inventing parallel frameworks.

- Constrain supported selector combinations at decode time.
- Keep unresolved encodings on an explicit opaque or primitive fallback.
- Preserve md-declared reads/writes, instruction guards, branch flow, register spans, and hardwired zero behavior.
- Capture all inputs before writing overlapping outputs.
- Model predicate outputs and carry chains individually; check whether each output is conditional, complementary, or preserves old state.
- Give rejected forms a specific coverage reason. An emitter limitation is not evidence that hardware or `ptxas` lacks the form.

Update semantic fixtures and dataflow expectations. Fixtures should assert values as well as relevant register and predicate effects. Use exact and readable floating-point modes when the operation has both lowerings.

## 4. Prefer GPU probes when hardware is available

Try a GPU oracle whenever a usable device and toolchain are available. GPU validation is encouraged, but a device or compiler limitation must be reported as an environment limitation rather than a semantic conclusion.

Add GPU fixtures to the family's `gpu_fixtures()` in `tests/cases/<family>.py`, reusing `tests/harness/scalar.py` (compiled scalar kernels), `tests/harness/templates.py` (patched CX wrapper and carry-kernel slots) and `tests/harness/gpu.py` (`Fixture`, `Driver`). While the behavior is unknown, draft the fixture with `expected=None` and run `python -m tests.observe <family>` to see raw GPU outputs; then write the reference in `tests/refs/` and the fixture becomes a regression test. A fixture:

1. Compiles a small kernel for the target architecture.
2. Verifies the expected opcode and selector are present, or documents the exact word/fields patched into the kernel.
3. Runs the same input vectors through the GPU, an independent reference, and Ghidra's P-code emulator.
4. Confirms that emulation visited the target instruction and that the GPU output came from the intended kernel.
5. Reports architecture, device, compiler, form matrix, vector count, and any normalizations or exclusions.

When patching an instruction, preserve the kernel wrapper and scheduling/control fields, document the changed word and fields, and investigate scheduling hazards if hardware results disagree. Classify a mismatch only after checking probe construction, target execution, data layout, and reference independence. A GPU mismatch may be a **testing/environment** issue or a **semantics** issue; explain the evidence for the classification.

If GPU execution cannot be performed, state why (device unavailable, unsupported architecture, permissions, compile failure, or harness issue), complete emulator and static checks that remain useful, and mark hardware validation as pending. Do not imply hardware verification from emulator coverage.

## 5. Build and validate in a safe order

Generate and compile languages before parallel Ghidra loading. Concurrent compiler writes to one `.sla` can corrupt it. Use `sass.build_languages`; select `--jobs` based on available CPU and memory, and keep build logs so failures can be classified.

Run focused checks first, then broaden to the architectures and exact/readable modes the change claims to support. Typical checks include:

```sh
python3 -m sass.build_languages SM89
python -m tests.semantics SM89 exact --family <family>
uv run --with pytest python -m pytest tests -m ghidra --arch SM89 --fp all
uv run --with pytest python -m pytest tests -m gpu --require-gpu --family <family>
python3 -m sass.coverage processor/SASS/data/languages --output processor/SASS/semantics/coverage.json
git diff --check
```

Adjust commands to the actual opcode and hardware scope. Do not run expansive suites blindly when their cost is unclear; inspect what each argument means, start with a focused case, and explain or stop a command that is unexpectedly broad or slow. Never report a cancelled or incomplete run as a pass.

Separate failures in reports:

- **Semantics failure:** incorrect P-code result/effects, incorrect independent expectation, or unresolved behavior.
- **Testing/environment failure:** language build/toolchain issue, malformed probe, target not reached, insufficient vectors, unavailable GPU, device permission, or unrelated harness failure.

Fix harness defects where practical before drawing conclusions about semantics.

## 6. Record and summarize the result

Update the relevant operation documentation and coverage manifest. Record hardware evidence by architecture and tested forms; other architectures may still have emulator support without hardware verification. Keep unresolved selectors visible as opaque, primitive, or uninvestigated.

Summarize:

- What semantics were added or corrected.
- Supported classes and selector combinations, plus remaining fallbacks.
- Emulator, dataflow, build, and GPU results, each with scope.
- Any failures, clearly identified as semantics or testing/environment.
- The exact commands needed to reproduce the checks.

Do not claim an opcode family is fully supported when only a subset of its selectors was verified.
