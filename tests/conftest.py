"""Markers say what a test needs (see tests/README.md): `ghidra` (compiled languages and pyghidra)
and `gpu` (an SM89 device). Plain `pytest` runs the Python tests, which need neither."""


def pytest_addoption(parser):
    parser.addoption("--update-baseline", action="store_true", help="accept current results as the baseline")
    parser.addoption("--full", action="store_true", help="use the whole real corpus instead of a sample")
    parser.addoption("--arch", default="SM89", help="architecture(s): SMxx[,SMyy] or all")
    parser.addoption("--fp", default="exact", help="fp lowering(s): exact, readable or all")
    parser.addoption("--family", action="append", help="limit semantics/GPU tests to these case families")
    parser.addoption("--exhaustive", action="store_true", help="run complete GPU/Ghidra sweeps (Python lifted tests always run all)")
    parser.addoption("--require-gpu", action="store_true", help="fail instead of skipping without an SM89 device")


def pytest_configure(config):
    config.addinivalue_line("markers", "ghidra: needs compiled SLEIGH languages and Ghidra's pyghidra venv")
    config.addinivalue_line("markers", "gpu: needs an SM89 CUDA device")
    if not config.option.markexpr: config.option.markexpr = "not ghidra and not gpu"


def pytest_terminal_summary(terminalreporter, config):
    if not hasattr(config, '_device_case_summary'): return
    counts, reasons = config._device_case_summary
    terminalreporter.write_sep('-', 'shared GPU cases')
    terminalreporter.write_line(', '.join(f'{n} {kind}' for kind, n in counts.items()))
    for reason, n in sorted(reasons.items()): terminalreporter.write_line(f'  {n} emulator-only: {reason}')
