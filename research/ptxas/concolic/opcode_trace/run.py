"""Trace the existing SymQEMU runtime without changing its constraints or compiler.

python3 research/ptxas/concolic/opcode_trace/run.py /tmp/opcode-trace --scope instruction
"""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from corpus import instruction_region, tokens, heads

COMPILER_SHA256 = 'daba837a68265cae38c832d13399b61dab811891de9b8914defddef143b849f2'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--scope', choices=('opcode', 'instruction'), default='opcode')
    parser.add_argument('--opcode', choices=('add', 'sub', 'xor'), default='add')
    parser.add_argument('--ptxas', type=Path, default=Path('/usr/local/cuda-13.0/bin/ptxas'))
    parser.add_argument('--symqemu', type=Path, default=HERE.parent / 'tools/symqemu/build/qemu-x86_64')
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    generated = root / 'generated'
    generated.mkdir()
    compiler = args.ptxas.resolve()
    compiler_hash = hashlib.sha256(compiler.read_bytes()).hexdigest()
    if compiler_hash != COMPILER_SHA256:
        parser.error('This diagnostic is for the pinned CUDA 13.0.88 ptxas only')
    qualified = args.opcode.encode() + (b'.b32' if args.opcode == 'xor' else b'.u32')
    data = (HERE / 'add.ptx').read_bytes().replace(b'add.u32', qualified)
    source = root / 'input.ptx'
    source.write_bytes(data)
    region = instruction_region(data)
    opcode_start = data.index(qualified, region[0], region[1])
    preflight = subprocess.run([str(compiler), '-arch=sm_75', '-o', '/dev/null', str(source)], capture_output=True)
    if preflight.returncode:
        parser.error('Seed compilation failed: ' + preflight.stderr.decode(errors='replace'))
    begin, end = (opcode_start, opcode_start + len(args.opcode)) if args.scope == 'opcode' else region
    shim = root / 'runtime_trace.so'
    subprocess.run(['cc', '-shared', '-fPIC', '-O1', '-Wall', '-Wextra', '-o', str(shim),
                    str(HERE / 'runtime_trace.c'), '-ldl'], check=True)
    env = os.environ.copy()
    for key in ('PTX_OPCODE_DOMAIN', 'PTX_SYMBOLIC_SCOPE', 'SYMCC_NO_SYMBOLIC_INPUT', 'SYMCC_MEMORY_INPUT'):
        env.pop(key, None)
    # Merge runtime events and solver announcements to attribute candidates to
    # the constraint being negated; stderr logging avoids independent buffers.
    env.pop('PTX_TRACE_LOG', None)
    env.update(LD_PRELOAD=str(shim),
               SYMCC_INPUT_FILE=str(source), SYMCC_OUTPUT_DIR=str(generated),
               PTX_SYMBOLIC_BEGIN=str(begin), PTX_SYMBOLIC_END=str(end))
    command = [str(args.symqemu.resolve()), str(compiler), '-arch=sm_75', '-o',
               str(root / 'output.cubin'), str(source)]
    with (root / 'solver.log').open('wb') as log:
        try:
            result = subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=30)
            status = result.returncode
        except subprocess.TimeoutExpired:
            status = 'timeout'
    paths = Counter()
    sample = {}
    opcode_constraints = {}
    origins = {}
    current_path = None
    trace_lines = []
    for line in (root / 'solver.log').read_text(errors='replace').splitlines():
        if line.startswith(('PATH ', 'BYTE ', 'ADD ')):
            trace_lines.append(line)
        if line.startswith('PATH '):
            _, pc, taken, expression = line.split(' ', 3)
            current_path = dict(pc=pc, taken=int(taken), expression=expression)
            paths[pc] += 1
            if pc in ('720f36', '426e9c', '4277b9', '426eac'):
                sample.setdefault(pc, []).append(dict(taken=int(taken), expression=expression))
            if pc == '720f36' and taken == '1' and expression.startswith('Equal(0x20'):
                for offset in range(opcode_start, opcode_start + len(args.opcode)):
                    if f'Read(index={offset:x})' in expression:
                        opcode_constraints.setdefault(str(offset), expression)
        match = re.search(r'\[INFO\] New testcase: (.*)', line)
        if match:
            origins[Path(match[1]).name] = current_path
    (root / 'runtime.log').write_text('\n'.join(trace_lines) + '\n')
    candidates = []
    for path in sorted(generated.iterdir()):
        result = subprocess.run([str(compiler), '-arch=sm_75', '-o', '/dev/null', str(path)],
                                capture_output=True, timeout=10)
        candidate = path.read_bytes()
        candidate_region = candidate[region[0]:region[1]]
        candidates.append(dict(file=path.name, accepted=result.returncode == 0,
                               region_hex=candidate_region.hex(),
                               opcode_roots=[t.decode('ascii', errors='replace')
                                             for t in heads(tokens(candidate_region))],
                               origin=origins.get(path.name),
                               diagnostic=result.stderr.decode(errors='replace')))
    summary = dict(compiler_sha256=compiler_hash, scope=args.scope, opcode=args.opcode,
                   input_sha256=hashlib.sha256(data).hexdigest(), symbolic_range=[begin, end],
                   status=status, generated=len(candidates),
                   accepted=sum(c['accepted'] for c in candidates),
                   accepted_opcode_roots=sorted({op for c in candidates if c['accepted']
                                                  for op in c['opcode_roots']}),
                   path_counts=dict(paths), constraint_samples={pc: rows[:2] for pc, rows in sample.items()},
                   opcode_constraints=opcode_constraints,
                   candidates=candidates)
    (root / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps({k: v for k, v in summary.items()
                      if k not in ('candidates', 'constraint_samples', 'path_counts', 'opcode_constraints')}, indent=2))


if __name__ == '__main__':
    main()
