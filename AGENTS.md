# nvdisasm reverse engineering

Target: `nvdisasm` from CUDA 13.4 (V13.4.92, built 2026-09-01), stripped x86-64 PIE.
`uv sync` installs it from PyPI (`nvidia-cuda-nvdisasm==13.4.92`, dev group) into `.venv/lib/python3.13/site-packages/nvidia/cu13/bin/nvdisasm`
(`sass.paths.NVDISASM` also looks in uv's cache). All addresses in `docs/` are specific to that build.

## Layout
`uv sync` installs the `sass` package (`src/sass`, editable) and its console scripts.
- `src/sass/md/`: NVIDIA's machine descriptions as shipped: `extract` (decrypt from nvdisasm into `out/raw/`, gitignored), `layout` (split into the committed `out/SMxx/`), `parse` (classes/enums/tables), `fixes` (known md errors, applied by `parse.load_classes`), `legal` (CONDITIONS as legality constraints), `expression` (md expressions).
- `src/sass/decode/`: decode IR (`ir`), reference decoder (`pydecode`), encoder (`pyencode`), cubin ELF reader (`cubin`).
- `src/sass/semantics/`: our value semantics (the md has none): bitvector IR (`bitvec`), family framework (`family`), families (`families/`), `lifter`/`evaluate` (Ghidra-free), `operands`, `operations` (routing), `fp/` (`fpformat`, `softfloat`), `runtime` (test-only CPU primitives).
- `src/sass/sleigh/`: SLEIGH generator (`gen`), semantics lowering (`lower`), language build (`build`), coverage summary (`coverage`) and the coverage page it renders from `report/` (HTML, CSS and JS, inlined with the embedded data).
- `src/sass/ghidra_bridge/`: programs run under Ghidra's pyghidra venv (`ghidra_decode`, `ghidra_emulate`, `BlockEmulator.java`) whose package module is the only path to them.
- `src/sass/oracle.py`: the nvdisasm oracle (sqlite cache in `.cache/`); `corpus.py`, `synth.py`: word corpora.
- `src/sass/check/`: comparison/report CLIs (`pydecode_disasm`, `ghidra_disasm`, `census`).
- `src/sass/paths.py`: repository paths and external tool locations.
- `tests/`: tests only (`harness/`, `cases/`, `refs/`, kernels, baselines); see `tests/README.md`.
- `processor/SASS/`: the Ghidra module (hand-written `.sinc`/`.pspec`/`.cspec`, generated languages, coverage/evidence JSON and report).
- `research/`: one-off investigations (ptxas, CuLifter, SLEIGH conditions).

Modules are imported by their own names, never aliased (`from sass.md import parse`).

Console scripts: `sass-extract-md`, `sass-layout-md`, `sass-decode`, `sass-gen-sleigh`, `sass-build-languages`, `sass-coverage`, `sass-refresh-coverage`, `sass-compare-pydecode`, `sass-compare-ghidra`, `sass-census`. Tests: `uv run pytest` runs the Python tests; `tests/README.md` describes them and the Ghidra (`-m ghidra`) and GPU (`-m gpu`) tests.

Coverage: `processor/SASS/semantics/coverage.json`, `evidence.json` (per-class verification evidence, `tests/evidence.py`), `badge.json` (the README's opcode-coverage badge) and `coverage.html` (the visual report it links to) are committed (the report groups opcodes by `src/sass/sleigh/categories.json`, parsed from NVIDIA's SASS instruction set reference on 2026-10-15), and CI (`.github/workflows/ci.yml`) fails when they differ from what the generator produces. Refresh them after any semantics, case, fixture or PTX probe change, without Ghidra or a GPU: `uv run sass-refresh-coverage` (manifests, evidence, then the committed files; about 4 minutes).

## Findings (docs/)
- `docs/machine-descriptions.md`: embedded md/latencies/patterns blobs, `out/` layout and `semantics/*.json` format, re-extraction.
- `docs/nvdisasm-binary.md`: GhidraMCP setup, function addresses, cipher, md blobs and aliases.
- `docs/decoding.md`: md matching, oracle behavior, printing rules.
- `docs/sleigh.md`: SLEIGH generator constraints and Ghidra limits/bugs.
- `docs/semantics.md`: families, operand values, exact/readable floating point, generator reference.
- `docs/hardware-facts.md`: GPU oracle and SM89 hardware-established behavior.
- `docs/testing.md`: test-harness notes.
- `docs/open.md`: open questions and roadmap.

Record new findings in the matching `docs/` file, dated where it matters.

## Writing new semantics
Work from the reference outward, and run the cheapest test that can fail first (`tests/README.md` has the details):
1. **Reference.** Write the instruction's behavior as a function in `tests/refs/`, independent of `src/sass` (it may import only `sass.semantics.fp.softfloat`). Prefer the obvious, even naive, formulation (enumerate bits, exact rationals) over a clever one. Base it on documented hardware behavior (`docs/hardware-facts.md`), PTX semantics, or GPU observation, never on the implementation.
2. **Emulator cases.** In `tests/cases/<family>.py` `emulator_cases`, add cases whose expected values come from the reference: zeros of both signs, NaN/infinity/subnormal, saturation and overflow bounds, each modifier and operand form, aliased registers. Use `c.sweep(n)` for sweeps over every code or a long value list.
3. **Semantics.** Implement the family in `src/sass/semantics/families/` and iterate on the Python tests only, which take seconds and need neither Ghidra nor a GPU: `uv run pytest -k "lifted or family_lowering" --family F` and then `--arch all --fp all`.
4. **SLEIGH.** Build only the languages you need (`uv run sass-build-languages SM89 --fp exact`, outside the sandbox), then run `uv run pytest -m ghidra --family F --arch SM89`. Use `uv run python -m tests.semantics SM89 --family F` to see failing cases. Ghidra results are cached, so reruns only emulate what changed. Review op-count baseline changes before accepting them with `--update-baseline`.
5. **Hardware.** If the behavior is not already established on hardware, add a GPU fixture with a reference to `gpu_fixtures()` (draft with `expected=None` and `uv run python -m tests.observe F --diff`). Then run `uv run pytest -m gpu --require-gpu --family F`. GPU runs are needed only when fixtures or references change. Record what the GPU established in `docs/hardware-facts.md`.
6. **Before committing,** run the Python tests and the Ghidra tests on every architecture and lowering after a full language build.

When tests disagree, fix the side that is wrong at its root cause. A reference that disagrees with the GPU is wrong. Semantics that disagree with a hardware-confirmed reference are wrong. Never loosen `same`/`emulator_same`/`excused` or a reference to make a failure pass without hardware evidence.

## Rules
- Don't name a repo dir `ghidra/` (or a module directory on `sys.path` `ghidra`/`pyghidra`): Python treats it as a namespace package shadowing pyghidra's Java `ghidra` package (RecursionError).
- SLEIGH compiler memory: compiling the SM100+ languages took 10-12 GiB of Java heap each, and `sass-build-languages` once ran all 18 compilers at once (no `MAXMEM` in `support/sleigh`, so each JVM may take a quarter of RAM); eight together exhausted 93 GiB and systemd-oomd killed whole terminals (2026-10-13). `sass-build-languages` now runs `MemAvailable // 16 GiB` compilers with `-Xmx16g` (`COMPILER_HEAP`). The tests are small by comparison (one emulation JVM ~0.7 GB on SM90).
- Complete SLEIGH generation/compilation before parallel language loading. Ghidra auto-compiles stale specs, and concurrent compilers writing one `.sla` can corrupt it.
- Run language builds and GPU validation outside the sandbox. The user explicitly requested this; Ghidra's JDK/config setup and GPU/device access may fail inside the sandbox.
- Architecture support and hardware verification are separate. Lack of hardware evidence beyond SM89 limits verification claims, not code generation: implement semantics for other architectures whenever the machine description and available evidence make the behavior reasonable, using conservative selector guards and fallback paths for unresolved forms. Record hardware verification by architecture and tested forms rather than gating native generation to SM89.
- Treat IEEE rounding as shared floating-point machinery, not an F2FP-only feature: the exact-rounding strategy can apply to arithmetic operations such as FADD/FMUL/FFMA as well as conversions. Reuse or factor the rounding implementation where appropriate, while preserving each operation's rules for fusion, FTZ/FMZ, signed zero, saturation, and exceptional values.
