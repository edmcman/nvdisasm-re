#!/usr/bin/env python3
"""Launch the LibAFL generator (ptx-sass-gen); arguments pass through, e.g. --output DIR --duration 120.
The AFL++ custom-mutator launcher it replaced is historical_run.py."""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
target = os.environ.get('CARGO_TARGET_DIR', str(HERE / 'tools' / 'libafl-target'))
os.execvp('cargo', ['cargo', 'run', '--release', '--quiet', '--manifest-path', str(HERE / 'libafl' / 'Cargo.toml'),
                    '--target-dir', target, '--', *sys.argv[1:]])
