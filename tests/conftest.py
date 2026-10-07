import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).parent.parent)]

def pytest_addoption(parser):
    parser.addoption("--update-baseline", action="store_true", help="accept current per-class results as the baseline")
    parser.addoption("--full", action="store_true", help="use the whole real corpus instead of a sample")
