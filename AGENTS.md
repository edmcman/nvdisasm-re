# nvdisasm reverse engineering

Target: `nvdisasm` from CUDA 13.4 (V13.4.92, built 2026-09-01), stripped x86-64 PIE.
`uv sync` installs it from PyPI (`nvidia-cuda-nvdisasm==13.4.92`, dev group) into `.venv/lib/python3.13/site-packages/nvidia/cu13/bin/nvdisasm`
(`sass.paths.NVDISASM` honors `$NVDISASM` and also looks in uv's cache). All addresses in `docs/` are specific to that build.

## Layout
`uv sync` installs the `sass` package (`src/sass`, editable) and its console scripts; a regular wheel or git install also works (it carries `md/data/`; the README shows the library API).
- `src/sass/md/`: NVIDIA's machine descriptions as shipped: `extract` (decrypt from nvdisasm into the cache's `md-raw/`), `layout` (split into the committed `src/sass/md/data/SMxx/`, shipped in the wheel), `parse` (classes/enums/tables), `fixes` (known md errors, applied by `parse.load_classes`), `legal` (CONDITIONS as legality constraints), `expression` (md expressions).
- `src/sass/decode/`: decode IR (`ir`), reference decoder (`pydecode`), cubin ELF reader (`cubin`).
- `src/sass/semantics/`: our value semantics (the md has none): bitvector IR (`bitvec`), family framework (`family`), families (`families/`), `lifter`/`evaluate` (Ghidra-free), `operands`, `operations` (routing), `fp/` (`fpformat`, `softfloat`, `mufu`), `primitive` (named runtime primitives families use: MUFU, warp state, barriers, address spaces), `runtime` (the CPU runtime: context parsing around the C++ primitives).
- `src/sass/sleigh/`: SLEIGH generator (`gen`), semantics lowering (`lower`), language build (`build`), per-class modifier forms (`modifiers`, shared with `tests/evidence.py` and the report), coverage summary (`coverage`) and the coverage page it renders from `report/` (HTML, CSS and JS, inlined with the embedded data).
- `src/sass/emulate/`: whole-kernel emulation: the scheduler every engine shares (`schedule`: seeding, warp turns, BAR/WARPSYNC, collectives' warp context, limits, errors, response; importable from pyghidra, so no generator imports), the lifted-semantics engine (`reference`, the Python counterpart of `ghidra_emulate`), and the C++ runtime: header-only `include/sass/` (`softfloat`, `mufu`, warp-wide `primitives`, the one implementation of the runtime primitives), `primitives.cpp` (its C entry points), `build` (compiles C++ on first use into `CACHE/emulate` with `paths.CXX`, `$CXX` overriding), `native` (ctypes, used by `semantics.runtime`), and the native engine: `cxx` (an instruction's C++), `codegen` (fragment and kernel translation units), `machine` (the C++ machine from Python) and `kernel` (whole kernels, model 1 in `include/sass/single.hpp`).
- `src/sass/ghidra_bridge/`: programs run under Ghidra's pyghidra venv (`ghidra_decode`, `ghidra_emulate`, `BlockEmulator.java`, `ExactPrototypes.java`) whose package module is the only path to them.
- `src/sass/assemble/`: assembler (`sass-assemble`; cubin writer `elf`) and the one encoder (`encoding`: strict, or `permissive=True` for the test harness); `docs/assembling.md`, `examples/assembler/`.
- `src/sass/provenance.py`: which committed evidence (`evidence.json`; not in the wheel, `$SASS_EVIDENCE` outside a checkout) ran a word's class and form.
- `src/sass/oracle.py`: the nvdisasm oracle (sqlite in `sass.paths.CACHE`: `$SASS_CACHE`, else `.cache/` in a checkout, else `~/.cache/sass`); `corpus.py`, `synth.py`: word corpora.
- `src/sass/check/`: comparison/report CLIs (`pydecode_disasm`, `ghidra_disasm`, `census`).
- `src/sass/paths.py`: package data (`MD`), cache, repository paths and external tool locations; `launch.py`: kernel launch layout shared by the emulator and GPU harness; `workers.py`: pool sizes that fit in available memory; `sqlcache.py`: sqlite caches filled by parallel workers.
- `tests/`: tests only (`harness/`, `cases/`, `refs/`, kernels, baselines); see `tests/README.md`. `python -m tests.export_cases` exports a language's shared cases as JSON lines (#50).
- `processor/SASS/`: the Ghidra module (hand-written `.sinc`/`.pspec`/`.cspec`, generated languages, coverage/evidence JSON and report).
- `research/`: one-off investigations (ptxas, CuLifter, SLEIGH conditions).
- `examples/`: library API examples (`lift_cubin`, `assemble_and_lift`, `evaluate`; `emulator/`, the native emulator's `run_kernel`, `trace_kernel`, `equivalence` and `show_cpp` with their `times3.sass`, opt-in with `-m native`; run by `tests/test_examples.py`) and `assembler/` sources.

Modules are imported by their own names, never aliased (`from sass.md import parse`).

Console scripts: `sass-extract-md`, `sass-layout-md`, `sass-decode`, `sass-assemble`, `sass-gen-sleigh`, `sass-build-languages`, `sass-coverage`, `sass-refresh-coverage`, `sass-compare-pydecode`, `sass-compare-ghidra`, `sass-census`. Tests: `uv run pytest` runs the Python tests; `tests/README.md` describes them and the Ghidra (`-m ghidra`) and GPU (`-m gpu`) tests.

Coverage: `processor/SASS/semantics/` commits `coverage.json`, `evidence.json` (per-class verification evidence, `tests/evidence.py`), `badge.json` (the README's opcode-coverage badge), `gpu_verdicts.json` (per-family verdicts of the recorded GPU observations, `tests/baseline/gpu_observations/`) and `coverage.html` (the visual report it links to, grouping opcodes by `src/sass/sleigh/categories.json`, parsed from NVIDIA's SASS instruction set reference on 2026-10-15). CI (`.github/workflows/ci.yml`) fails when they differ from what the generator produces. Refresh and commit them (CI only checks them) after any semantics, case, fixture or PTX probe change with `nice uv run sass-refresh-coverage` in the background; it needs neither Ghidra nor a GPU and regenerates the manifests, the evidence, then the committed files. It takes about 19 minutes on 32 cores, 15 of them evidence, CPU-bound in sharded case generation, since fp32 reached about 680,000 cases per language (over an hour before evidence was sharded on 2026-10-20).

## Findings (docs/)
- `docs/machine-descriptions.md`: embedded md/latencies/patterns blobs, `md/data/` layout and `semantics/*.json` format, re-extraction.
- `docs/nvdisasm-binary.md`: GhidraMCP setup, function addresses, cipher, md blobs and aliases.
- `docs/decoding.md`: md matching, oracle behavior, printing rules.
- `docs/assembling.md`: assembler syntax, Python API, scheduling, cubin ABI profiles per architecture.
- `docs/sleigh.md`: SLEIGH generator constraints and Ghidra limits/bugs.
- `docs/semantics.md`: families, operand values, exact/readable floating point, generator reference.
- `docs/emulation.md`: the three emulation engines, the shared kernel scheduler, the native machine, models and performance.
- `docs/hardware-facts.md`: GPU oracle and hardware-established behavior (SM89, and SM90 since 2026-10-22).
- `docs/testing.md`: test-harness history and decisions (how-to is `tests/README.md`).
- `docs/open.md`: open questions and roadmap.
- `docs/coverage-2026-10-20.md`: a dated coverage-change report (10 opcodes' modifier-form evidence).

Record new findings in the matching `docs/` file, dated where it matters.

## Writing new semantics
To investigate an opcode's forms and compiler encoding first (form matrix, ptxas tracing, labelling findings), use the `sass-opcode-semantics` skill (`.agents/skills/`, linked into `.claude/skills/`); it defers to this section for the implementation and test steps. To drive one opcode's coverage and evidence across every legal modifier combination and architecture, use `sass-opcode-coverage`.

Work from the reference outward, and run the cheapest test that can fail first (`tests/README.md` has the details):
1. **Reference.** Write the instruction's behavior as a function in `tests/refs/`, independent of `src/sass` (it may import only `sass.semantics.fp.softfloat`). Prefer the obvious, even naive, formulation (enumerate bits, exact rationals) over a clever one. Base it on documented hardware behavior (`docs/hardware-facts.md`), PTX semantics, or GPU observation, never on the implementation.
2. **Emulator cases.** In `tests/cases/<family>.py` `emulator_cases`, add cases whose expected values come from the reference: zeros of both signs, NaN/infinity/subnormal, saturation and overflow bounds, each modifier and operand form, aliased registers. Use `c.sweep(n)` for sweeps over every code or a long value list.
3. **Semantics.** Implement the family in `src/sass/semantics/families/` and iterate on the Python tests only, which take seconds and need neither Ghidra nor a GPU: `uv run pytest -k "lifted or family_lowering" --family F` and then `--arch all --fp all`.
4. **SLEIGH.** Build only the languages you need (`uv run sass-build-languages SM89 --fp exact`, outside the sandbox), then run `uv run pytest -m ghidra --family F --arch SM89`. Use `uv run python -m tests.semantics SM89 --family F` to see failing cases. Ghidra results are cached, so reruns only emulate what changed. Review op-count baseline changes before accepting them with `--update-baseline`.
5. **Hardware.** Hardware-valid SM89/SM90 `emulator_cases` run on the architecture of GPU 0 automatically through generated native cubins: `uv run pytest -m gpu --require-gpu --family F` (sampled sweeps; add `--exhaustive` for every case). Run them when shared cases, fixtures, references or the GPU harness change. For discovery without a reference, use `Cases.probe` / `device_cases.observe` or `uv run python -m tests.probe INPUT.json --out OUTPUT.json`; raw observations do not create verification evidence. Promote a finding to an independent reference and `c.add` regression case. Measure single-instruction behavior with shared cases, not patched-kernel `gpu_fixtures()`: the same case then runs in the emulator, Ghidra and on the GPU, and creates evidence. Keep `gpu_fixtures()` and `tests.observe` for whole-kernel or compiler-generated probes. See the investigation skill's `references/gpu-probes.md`. Record what the GPU established in `docs/hardware-facts.md`.
6. **Before committing,** there is no need to run the Python or Ghidra tests on every architecture and lowering: CI (`.github/workflows/ci.yml`) runs both on every push and pull request. Run the GPU tests yourself when step 5 calls for them (CI has no GPU).

When tests disagree, fix the side that is wrong at its root cause. A reference that disagrees with the GPU is wrong. Semantics that disagree with a hardware-confirmed reference are wrong. Never loosen `same`/`emulator_same`/`excused` or a reference to make a failure pass without hardware evidence.

## Rules
- Run builds, test suites, plan regeneration, coverage refreshes and GPU runs under `nice` (`nice uv run pytest ...`, `nice uv run sass-build-languages ...`) so they don't starve the user's desktop.
- Whenever you start a long-running command (test suites, language builds, coverage refreshes, GPU runs, plan regeneration), tell the user approximately how long it will take, from `tests/README.md`'s resource table and the times recorded here, adjusted for what differs (a cold `.cache/` in a new worktree, `--arch all`, `--exhaustive`). If no time is recorded, say so and give your best guess. Flag the user whenever a run goes longer than the estimate, with the likely reason (load average, a competing job, cold caches) and a revised estimate.
- Don't name a repo dir `ghidra/` (or a module directory on `sys.path` `ghidra`/`pyghidra`): Python treats it as a namespace package shadowing pyghidra's Java `ghidra` package (RecursionError).
- SLEIGH compiler memory: compiling the SM100+ languages took 10-12 GiB of Java heap each, and `sass-build-languages` once ran all 18 compilers at once (no `MAXMEM` in `support/sleigh`, so each JVM may take a quarter of RAM); eight together exhausted 93 GiB and systemd-oomd killed whole terminals (2026-10-13). `sass-build-languages` caps each compiler's heap (`COMPILER_HEAP`, `-Xmx4g`) and runs `MemAvailable // 4 GiB` of them: capped, SM100 compiles in 7 s and 1.1-1.9 GiB, the same `.sla` even at `-Xmx2g` (2026-10-16). The tests are small by comparison (one emulation JVM ~0.7 GB on SM90).
- Complete SLEIGH generation/compilation before parallel language loading. Ghidra auto-compiles stale specs, and concurrent compilers writing one `.sla` can corrupt it.
- Run language builds and GPU validation outside the sandbox. The user explicitly requested this; Ghidra's JDK/config setup and GPU/device access may fail inside the sandbox.
- Architecture support and hardware verification are separate. Lack of hardware evidence beyond SM89/SM90 limits verification claims, not code generation: implement semantics for other architectures whenever the machine description and available evidence make the behavior reasonable, using conservative selector guards and fallback paths for unresolved forms. Record hardware verification by architecture and tested forms rather than gating native generation to the architectures with a GPU.
- Treat IEEE rounding as shared floating-point machinery, not an F2FP-only feature: the exact-rounding strategy can apply to arithmetic operations such as FADD/FMUL/FFMA as well as conversions. Reuse or factor the rounding implementation where appropriate, while preserving each operation's rules for fusion, FTZ/FMZ, signed zero, saturation, and exceptional values.
