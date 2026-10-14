"""Test layers (see AGENTS.md): 0 helpers, 1 pydecode vs nvdisasm, 2 SLEIGH disassembly, 3 p-code
dataflow, 4 p-code semantics, 5 references vs GPU. Markers express requirements: `ghidra`
(compiled languages and pyghidra) and `gpu` (an SM89 device). Plain `pytest tests` runs neither."""


def pytest_addoption(parser):
    parser.addoption("--update-baseline", action="store_true", help="accept current results as the baseline")
    parser.addoption("--full", action="store_true", help="use the whole real corpus instead of a sample")
    parser.addoption("--arch", default="SM89", help="architecture(s) for Ghidra layers: SMxx[,SMyy] or all")
    parser.addoption("--fp", default="exact", help="fp lowering(s): exact, readable or all")
    parser.addoption("--family", action="append", help="limit semantics/GPU tests to these case families")
    parser.addoption("--require-gpu", action="store_true", help="fail instead of skipping without an SM89 device")


def pytest_configure(config):
    config.addinivalue_line("markers", "ghidra: needs compiled SLEIGH languages and Ghidra's pyghidra venv")
    config.addinivalue_line("markers", "gpu: needs an SM89 CUDA device")
    if not config.option.markexpr: config.option.markexpr = "not ghidra and not gpu"
