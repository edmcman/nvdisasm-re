#!/usr/bin/env python3
"""Stock AFL++ (QEMU mode) campaign on ptxas, with CmpLog (RedQueen) and SymQEMU concolic instances.

python3 afl_campaign.py start OUT [--arch sm_100]   # starts every instance in the background
python3 afl_campaign.py report OUT                  # compiles the queues, counts accepted files and opcodes
python3 afl_campaign.py monitor OUT [--interval 180]  # report every few minutes, appending to OUT/monitor.log
python3 afl_campaign.py stop OUT
"""
import argparse, json, os, re, signal, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from corpus import heads, instruction_region, tokens

HERE = Path(__file__).resolve().parent
AFL = HERE/'tools'/'AFLplusplus'
PTXAS = '/usr/local/cuda-13.0/bin/ptxas'
SCHEDULES = ['explore', 'fast', 'coe', 'lin', 'quad', 'exploit', 'rare', 'mmopt']
CMPLOG, CONCOLIC, PLAIN = 2, 5, 20  # besides the main; the SymQEMU workers use the remaining cores

def start(out, arch):
    out.mkdir(parents=True, exist_ok=False)
    seed = out/'seeds'/'seed.ptx'; seed.parent.mkdir()
    scaffold = re.sub(rb'(?m)^\.target sm_\d+', f'.target {arch}'.encode(), (HERE/'generic_sm75.ptx').read_bytes())
    begin, end = instruction_region(scaffold)
    # The queue holds only the instruction text; ptx_scaffold.so wraps it in the rest of the kernel.
    (out/'scaffold.pre').write_bytes(scaffold[:begin]); (out/'scaffold.post').write_bytes(scaffold[end:]); seed.write_bytes(scaffold[begin:end])
    env = dict(os.environ, AFL_PATH=str(AFL), AFL_ENTRYPOINT='0x4428e0', AFL_QEMU_OLD_COVERAGE='1', AFL_NO_UI='1',
               AFL_NO_AFFINITY='1', AFL_SKIP_CPUFREQ='1', AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES='1', AFL_IGNORE_UNKNOWN_ENVS='1', AFL_CUSTOM_MUTATOR_LIBRARY=str(HERE/'tools'/'ptx_scaffold.so'), AFL_POST_PROCESS_KEEP_ORIGINAL='1',
               PTX_PREFIX=str(out/'scaffold.pre'), PTX_SUFFIX=str(out/'scaffold.post'))
    concolic = dict(AFL_CUSTOM_MUTATOR_LIBRARY=f"{HERE/'tools'/'ptx_scaffold.so'}:{AFL/'custom_mutators/symqemu/symqemu-mutator.so'}", AFL_DISABLE_TRIM='1',
                    SYMQEMU_ALL='1', PTX_SYMQEMU_BINARY=str(HERE/'tools/symqemu/build/symqemu-x86_64'),
                    PTX_SYMQEMU_TIMEOUT='30', PATH=f"{HERE}:{env['PATH']}")
    instances = [('main', ['-M', 'main', '-c', '0'], {})]
    # Deterministic stages run on the main only (AFL++ runs them on every instance unless -z).
    instances += [(f'cmplog{i}', ['-S', f'cmplog{i}', '-z', '-c', '0', '-p', SCHEDULES[i + 1]], {}) for i in range(CMPLOG)]
    instances += [(f'concolic{i}', ['-S', f'concolic{i}', '-z', '-p', SCHEDULES[i % len(SCHEDULES)]], {**concolic, 'PTX_SYMQEMU_LOG_DIR': str(out/f'symqemu{i}')}) for i in range(CONCOLIC)]
    instances += [(f'plain{i}', ['-S', f'plain{i}', '-z', '-p', SCHEDULES[i % len(SCHEDULES)]], {}) for i in range(PLAIN)]
    pids = {}
    for name, flags, extra in instances:
        log = (out/f'{name}.log').open('wb')
        argv = [str(AFL/'afl-fuzz'), '-Q', '-m', 'none', '-t', '2000', '-i', str(seed.parent), '-o', str(out/'afl'),
                '-x', str(HERE/'dict'/'ptx-lexer.dict'), *flags, '--', PTXAS, f'-arch={arch}', '-o', '/dev/null', '@@']
        pids[name] = subprocess.Popen(argv, env={**env, **extra}, stdout=log, stderr=subprocess.STDOUT, start_new_session=True).pid
    (out/'pids.json').write_text(json.dumps(pids, indent=1))
    print(f'started {len(pids)} afl-fuzz instances in {out}')

def stop(out):
    for name, pid in json.loads((out/'pids.json').read_text()).items():
        try: os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError: pass
    subprocess.run(['pkill', '-x', 'symqemu-x86_64'])

WRAP = {}  # (prefix, suffix) of the campaign being reported; empty for whole-file campaigns

def accepted(path):
    import tempfile
    prefix, suffix = WRAP.get('scaffold', (b'', b''))
    with tempfile.NamedTemporaryFile(suffix='.ptx') as wrapped:
        wrapped.write(prefix + Path(path).read_bytes() + suffix); wrapped.flush()
        return subprocess.run([PTXAS, '-arch=sm_100', '-o', '/dev/null', wrapped.name], capture_output=True).returncode == 0

def report(out):
    registry = json.loads((HERE/'opcode_registry.json').read_text()); roots = set(registry['roots'])
    if (out/'scaffold.pre').exists(): WRAP['scaffold'] = ((out/'scaffold.pre').read_bytes(), (out/'scaffold.post').read_bytes())
    prefix, suffix = WRAP.get('scaffold', (b'', b''))
    scaffold = {h.decode() for h in heads(tokens(prefix + (out/'seeds'/'seed.ptx').read_bytes() + suffix))} & roots
    cache_path = out/'report-cache.json'
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    files = [p for p in (out/'afl').glob('*/queue/id:*') if p.is_file()]
    todo = [p for p in files if str(p) not in cache]
    with ThreadPoolExecutor(8) as pool: results = list(pool.map(accepted, todo))
    cache.update({str(p): ok for p, ok in zip(todo, results)}); cache_path.write_text(json.dumps(cache))
    found = {}
    for p in files:
        if not cache[str(p)]: continue
        for h in heads(tokens(p.read_bytes())):
            if h.decode() in roots: found.setdefault(h.decode(), p.name)
    stat_paths = sorted((out/'afl').glob('*/fuzzer_stats'))
    stats = [{k.strip(): v for k, v in (l.split(':', 1) for l in s.read_text().splitlines() if ':' in l)} for s in stat_paths]
    total = lambda k: sum(float(s[k].strip().rstrip('%')) for s in stats if k in s)
    by_role = {}
    for path, s in zip(stat_paths, stats):
        role = re.sub(r'\d+$', '', path.parent.name)
        c, pend, fav = by_role.setdefault(role, [0, 0, 0]), int(s['pending_total']), int(s['pending_favs'])
        c[0] += int(s['corpus_count']); c[1] += pend; c[2] += fav
    pending = ' '.join(f'{r} {c[1] / max(c[0], 1):.0%} ({c[2]} favored)' for r, c in by_role.items())
    print(f'{len(stats)} instances, {int(total("execs_done"))} execs ({total("execs_per_sec"):.0f}/s), queue files {len(files)}, accepted by ptxas {sum(cache[str(p)] for p in files)}, never fuzzed: {pending}')
    new = sorted(set(found) - scaffold)
    print(f'opcode roots in accepted files: {len(found)} (scaffold has {sorted(scaffold)}): {new}')
    return new

def monitor(out, interval):
    import time
    seen, start = set(), time.time()
    while True:
        print(f'--- {time.time() - start:.0f}s'); sys.stdout.flush()
        new = report(out); print(f'new since last report: {sorted(set(new) - seen)}', flush=True); seen |= set(new)
        time.sleep(interval)

if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('command', choices=['start', 'report', 'monitor', 'stop']); p.add_argument('out', type=Path)
    p.add_argument('--arch', default='sm_100'); p.add_argument('--interval', type=int, default=180); a = p.parse_args()
    {'start': lambda: start(a.out.resolve(), a.arch), 'report': lambda: report(a.out.resolve()), 'monitor': lambda: monitor(a.out.resolve(), a.interval), 'stop': lambda: stop(a.out.resolve())}[a.command]()
