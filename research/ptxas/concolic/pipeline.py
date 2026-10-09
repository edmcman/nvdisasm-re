#!/usr/bin/env python3
"""Persistent local PTX compilation and lowering catalogue service for LibAFL."""
import argparse
import fcntl
import hashlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import queue
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))
from corpus import instruction_region, name_key, sass_key
from forms import ARCHITECTURES, TARGETS, canonical, decode_cubin, digest
VERIFIED_COMPILER = 'daba837a68265cae38c832d13399b61dab811891de9b8914defddef143b849f2'
# Deferred forkserver: first instruction of the input-independent driver call that later
# fopen()s the PTX file (runs once, single-threaded, nothing touches input/output earlier).
# Skips ~60% of each native run; 6.5x mutation throughput under QEMU.
FORKSERVER_ENTRY = '0x4428e0'

SCHEMA = '''
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS candidates(key TEXT PRIMARY KEY, source TEXT, first_seen REAL);
CREATE TABLE IF NOT EXISTS observations(id INTEGER PRIMARY KEY, key TEXT, origin TEXT, bytes_hash TEXT, seen REAL);
CREATE TABLE IF NOT EXISTS compilations(cache_key TEXT PRIMARY KEY, key TEXT, arch TEXT, target TEXT,
 compiler TEXT, options TEXT, state TEXT DEFAULT 'pending', owner TEXT, retries INTEGER DEFAULT 0,
 directory TEXT, rc INTEGER, diagnostics TEXT, encoding TEXT, decoder_arch TEXT);
CREATE TABLE IF NOT EXISTS forms(arch TEXT, hash TEXT, signature TEXT, witness TEXT, PRIMARY KEY(arch,hash));
CREATE TABLE IF NOT EXISTS sequences(arch TEXT, hash TEXT, signature TEXT, witness TEXT, PRIMARY KEY(arch,hash));
CREATE TABLE IF NOT EXISTS replays(cache_key TEXT PRIMARY KEY, state TEXT DEFAULT 'pending',
 owner TEXT, retries INTEGER DEFAULT 0, diagnostics TEXT);
CREATE TABLE IF NOT EXISTS concolic(cache_key TEXT PRIMARY KEY, key TEXT, state TEXT DEFAULT 'pending',
 owner TEXT, retries INTEGER DEFAULT 0, directory TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT);
'''

def write_json(path, obj):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(obj, indent=2) + '\n'); temporary.replace(path)

def kill_group(pid, sig=signal.SIGKILL):
    try: os.killpg(pid, sig)
    except ProcessLookupError: pass

def bounded(argv, log, seconds, stop, events, env=None):
    with Path(log).open('wb') as output:
        child = subprocess.Popen(argv, stdout=output, stderr=subprocess.STDOUT,
                                 env=env, start_new_session=True)
        events.put(dict(kind='child', pid=child.pid, active=True))
        deadline = time.monotonic() + seconds
        interrupted = timed_out = False
        try:
            while child.poll() is None:
                interrupted = stop.is_set(); timed_out = time.monotonic() >= deadline
                if interrupted or timed_out:
                    kill_group(child.pid); break
                try: child.wait(.05)
                except subprocess.TimeoutExpired: pass
            rc = child.wait()
        finally:
            kill_group(child.pid)  # also any descendants remaining after exit
            events.put(dict(kind='child', pid=child.pid, active=False))
    return dict(rc=rc, interrupted=interrupted, timed_out=timed_out)

def analysis_worker(jobs, events, config, stop):
    while not stop.is_set():
        try: job = jobs.get(timeout=.2)
        except queue.Empty: continue
        directory = Path(job['directory']); directory.mkdir(parents=True, exist_ok=True)
        source = Path(job['source']); output = directory / 'kernel.cubin'
        result = bounded([config['ptxas'], '-arch=' + job['target'], '-o', str(output), str(source)],
                         directory / 'compile.log', 3, stop, events)
        result.update(kind='compiled', cache_key=job['cache_key'], arch=job['arch'])
        result['diagnostics'] = (directory / 'compile.log').read_text(errors='replace')
        if result['interrupted']: result['state'] = 'running'
        elif result['rc'] != 0:
            result['state'] = ('incompatible' if 'higher than default SM version' in result['diagnostics'] else 'rejected')
        else:
            try:
                data = output.read_bytes(); result['encoding'] = sass_key(data)
                decoded = decode_cubin(data, job['arch'])
                result.update(decoded); result['state'] = decoded['status']
                result['diagnostics'] = canonical(decoded['diagnostics'])
                (directory / 'sass.txt').write_text('\n'.join(decoded['listing']) + '\n')
                write_json(directory / 'normalized.json', {k:decoded[k] for k in ('forms','sequences','diagnostics')})
            except Exception as e:
                result.update(state='undecodable', diagnostics=f'{type(e).__name__}: {e}')
        write_json(directory / 'compile-result.json', {k:v for k,v in result.items() if k not in ('forms','sequences','listing')})
        events.put(result)

def concolic_worker(jobs, events, config, stop, index):
    # Independently bounded symbolic compiler execution; only the instruction region is symbolic.
    while not stop.is_set():
        try: job = jobs.get(timeout=.2)
        except queue.Empty: continue
        directory = Path(job['directory']); directory.mkdir(parents=True, exist_ok=True)
        source = directory / 'input.ptx'; source.write_bytes(Path(job['source']).read_bytes())
        generated = directory / 'generated'; generated.mkdir(exist_ok=True)
        env = os.environ.copy()
        for name in ('PTX_OPCODE_DOMAIN','PTX_SYMBOLIC_SCOPE',
                     'SYMCC_NO_SYMBOLIC_INPUT','SYMCC_MEMORY_INPUT','AFL_CUSTOM_MUTATOR_LIBRARY',
                     'AFL_CUSTOM_MUTATOR_ONLY'):
            env.pop(name, None)
        begin, end = instruction_region(source.read_bytes())
        env.update(SYMCC_INPUT_FILE=str(source), SYMCC_OUTPUT_DIR=str(generated),
                   PTX_SYMBOLIC_BEGIN=str(begin), PTX_SYMBOLIC_END=str(end))
        result = bounded([config['symqemu'], config['ptxas'], '-arch=' + job['target'],
                          '-o', '/dev/null', str(source)], directory / 'solver.log', 30, stop, events, env)
        # Enumerate completed solver output once per execution; never poll directories to
        # communicate between generators. Most outputs take a lexer error branch, so only those
        # ptxas accepts for the job's target are submitted.
        outputs = [path for path in sorted(generated.iterdir()) if path.is_file()]
        accepted = 0
        for path in outputs:
            if stop.is_set(): break
            check = bounded([config['ptxas'], '-arch=' + job['target'], '-o', '/dev/null', str(path)],
                            directory / 'check.log', 3, stop, events)
            if check['rc'] == 0:
                events.put(dict(kind='candidate', origin=f'concolic:{index}:{job["arch"]}:{job["key"]}', data=path.read_bytes()))
                accepted += 1
        result.update(kind='concolic_done', cache_key=job['cache_key'], generated=len(outputs), candidates=accepted)
        write_json(directory / 'result.json', result); events.put(result)

def replay_worker(jobs, events, config, stop):
    while not stop.is_set():
        try: job = jobs.get(timeout=.2)
        except queue.Empty: continue
        directory = Path(job['directory']) / 'ir'; directory.mkdir(exist_ok=True)
        env = os.environ.copy()
        env.update(PTX_IR_OUT=str(directory / 'ptx-ir.jsonl'), DAG_OUT=str(directory / 'cop-dag.txt'),
                   ORI_OUT=str(directory / 'ori.txt'), ORI_PHASES='all', ORI_WHEN='both')
        results = {}; interrupted = False
        for name, script, artifact in [('ptx',HERE/'ptx_ir_print.py','ptx-ir.jsonl'),
                                      ('cop',HERE.parent/'dag_print.py','cop-dag.txt'),
                                      ('ori',HERE.parent/'ori_print.py','ori.txt')]:
            if stop.is_set(): interrupted = True; break
            artifact_path = directory / artifact
            if artifact_path.exists(): artifact_path.unlink()  # stale retry output is not evidence
            result = bounded(['gdb','-q','-batch','-x',str(script),'--args',config['ptxas'],
                              '-arch=' + job['target'],'-o',str(directory/'kernel.cubin'),job['source']],
                             directory/f'gdb-{name}.log',30,stop,events,env)
            interrupted |= result['interrupted']
            results[name] = dict(**result, nonempty=artifact_path.exists() and artifact_path.stat().st_size > 0)
        state = 'running' if interrupted else ('captured' if len(results)==3 and
                 all(r['rc']==0 and r['nonempty'] for r in results.values()) else 'failed')
        write_json(directory/'result.json',dict(state=state, results=results))
        events.put(dict(kind='replayed',cache_key=job['cache_key'],state=state,diagnostics=canonical(results)))

def recover(db):
    """An interrupted lease is retried once; a second interruption is explicit."""
    for table in ('compilations','replays','concolic'):
        db.execute(f"UPDATE {table} SET state=CASE WHEN retries=0 THEN 'pending' ELSE 'failed' END, "
                   "retries=retries+1, owner=NULL WHERE state='running'")
    db.commit()

class Coordinator:
    def __init__(self, root, db, config):
        self.root,self.db,self.config = root,db,config
        self.duplicates = 0; self.received = 0; self.cache_hits = 0; self.concolic_turn = 0
        # Newest target: it accepts the most instructions, so its rejections are mostly target-independent.
        self.probe = config['architectures'][-1]

    def candidate(self, data, origin):
        data = bytes(data)
        if not data or len(data) > 1048576: return
        key = name_key(data); self.received += 1
        self.db.execute('INSERT INTO observations(key,origin,bytes_hash,seen) VALUES(?,?,?,?)',
                        (key,origin,hashlib.sha256(data).hexdigest(),time.time()))
        exists = self.db.execute('SELECT 1 FROM candidates WHERE key=?',(key,)).fetchone()
        if exists: self.duplicates += 1
        else:
            path = self.root/'sources'/f'{key}.ptx'; path.write_bytes(data)
            self.db.execute('INSERT INTO candidates VALUES(?,?,?)',(key,str(path),time.time()))
        # Only the probe target is compiled at first; see compiled() for the others.
        for arch in self.config['architectures']:
            ck = digest([self.config['compiler'],['-arch='+TARGETS[arch]],arch,key])
            directory = self.root/'cases'/ck
            changed = self.db.execute('INSERT OR IGNORE INTO compilations '
                '(cache_key,key,arch,target,compiler,options,directory,state) VALUES(?,?,?,?,?,?,?,?)',
                (ck,key,arch,TARGETS[arch],self.config['compiler'],canonical(['-arch='+TARGETS[arch]]),str(directory),
                 'pending' if arch == self.probe else 'waiting')).rowcount
            self.cache_hits += not changed
        self.db.commit()

    def job(self, table, owner):
        if table == 'compilations':
            row = self.db.execute("SELECT c.cache_key,c.key,c.arch,c.target,c.directory,s.source "
               "FROM compilations c JOIN candidates s USING(key) WHERE c.state='pending' ORDER BY s.first_seen,c.arch LIMIT 1").fetchone()
        elif table == 'replays':
            row = self.db.execute("SELECT c.cache_key,c.key,c.arch,c.target,c.directory,s.source "
               "FROM replays r JOIN compilations c USING(cache_key) JOIN candidates s USING(key) "
               "WHERE r.state='pending' ORDER BY s.first_seen,c.arch LIMIT 1").fetchone()
        else:
            # Concolic jobs rotate through the architectures, oldest kernel first within each.
            arches = self.config['architectures']; row = None
            for i in range(len(arches)):
                arch = arches[(self.concolic_turn + i) % len(arches)]
                row = self.db.execute("SELECT c.cache_key,c.key,c.arch,c.target,l.directory,s.source "
                   "FROM concolic l JOIN compilations c USING(cache_key) JOIN candidates s ON s.key=c.key "
                   "WHERE l.state='pending' AND c.arch=? ORDER BY s.first_seen LIMIT 1",(arch,)).fetchone()
                if row: self.concolic_turn += i + 1; break
        if not row: return None
        self.db.execute(f"UPDATE {table} SET state='running',owner=? WHERE cache_key=? AND state='pending'",(owner,row[0]))
        self.db.commit()
        return dict(zip(('cache_key','key','arch','target','directory','source'),row))

    def compiled(self, msg, publisher):
        ck = msg['cache_key']; state = msg['state']
        self.db.execute('UPDATE compilations SET state=?,rc=?,diagnostics=?,encoding=?,decoder_arch=?,owner=NULL WHERE cache_key=?',
             (state,msg['rc'],msg['diagnostics'],msg.get('encoding'),msg.get('elf_arch'),ck))
        if msg['arch'] == self.probe and state != 'running':
            # A syntax or semantic rejection that names no target fails everywhere: record it for
            # the other targets without compiling. Anything else releases them.
            key = self.db.execute('SELECT key FROM compilations WHERE cache_key=?',(ck,)).fetchone()[0]
            if state == 'rejected' and not re.search(r'sm_\d|\.target|compute_\d', msg['diagnostics']):
                self.db.execute("UPDATE compilations SET state='rejected',diagnostics=? WHERE key=? AND state='waiting'",
                                (f'inferred from {self.probe}: '+msg['diagnostics'],key))
            else:
                self.db.execute("UPDATE compilations SET state='pending' WHERE key=? AND state='waiting'",(key,))
        if state == 'decoded':
            new = 0
            for h, form in msg['forms'].items():
                new += self.db.execute('INSERT OR IGNORE INTO forms VALUES(?,?,?,?)',
                          (msg['arch'],h,canonical(form),ck)).rowcount
            for sequence in msg['sequences']:
                new += self.db.execute('INSERT OR IGNORE INTO sequences VALUES(?,?,?,?)',
                          (msg['arch'],digest(sequence),canonical(sequence),ck)).rowcount
            if new: self.db.execute('INSERT OR IGNORE INTO replays(cache_key) VALUES(?)',(ck,))
            row = self.db.execute('SELECT key,target FROM compilations WHERE cache_key=?',(ck,)).fetchone()
            key,target = row; source = self.db.execute('SELECT source FROM candidates WHERE key=?',(key,)).fetchone()[0]
            publisher.stdin.write(canonical([target,source])+'\n'); publisher.stdin.flush()
            if instruction_region(Path(source).read_bytes()):
                self.db.execute('INSERT OR IGNORE INTO concolic(cache_key,key,directory) VALUES(?,?,?)',
                                (ck,key,str(self.root/'concolic'/ck)))
        self.db.commit()

    def status(self, activity, processes, start, phase):
        elapsed = time.monotonic()-start
        result = dict(phase=phase,elapsed_seconds=round(elapsed,2),received=self.received,
           token_duplicates=self.duplicates,cache_hits=self.cache_hits,activity=activity,
           mutations=sum(a['executions'] for a in activity.values()),
           worker_exit_codes={name:p.poll() if isinstance(p,subprocess.Popen) else p.exitcode for name,p in processes.items()},
           accepted_by_arch=dict(self.db.execute("SELECT arch,count(*) FROM compilations WHERE state='decoded' GROUP BY arch")),
           forms_by_arch=dict(self.db.execute('SELECT arch,count(*) FROM forms GROUP BY arch')),
           sequences_by_arch=dict(self.db.execute('SELECT arch,count(*) FROM sequences GROUP BY arch')),
           compilation_states=dict(self.db.execute('SELECT state,count(*) FROM compilations GROUP BY state')),
           replay_states=dict(self.db.execute('SELECT state,count(*) FROM replays GROUP BY state')),
           concolic_states=dict(self.db.execute('SELECT state,count(*) FROM concolic GROUP BY state')))
        result['mutations_per_second'] = round(result['mutations']/max(elapsed,.01),2)
        result['candidates_per_second'] = round(self.received/max(elapsed,.01),2)
        write_json(self.root/'status.json',result)
        return result

def socket_reader(server, events, stop):
    def read(connection):
        with connection, connection.makefile('rb') as stream:
            for line in stream:
                try: events.put(json.loads(line))
                except (ValueError, UnicodeError): pass
    server.settimeout(.2)
    while not stop.is_set():
        try: connection,_ = server.accept()
        except socket.timeout: continue
        threading.Thread(target=read,args=(connection,),daemon=True).start()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--application',required=True,type=Path)
    p.add_argument('--output',required=True,type=Path)
    p.add_argument('--n',type=int,default=8); p.add_argument('--m',type=int,default=1)
    p.add_argument('--duration',type=int,default=3600)
    p.add_argument('--seed',type=Path,action='append')
    p.add_argument('--architectures',default=','.join(ARCHITECTURES))
    p.add_argument('--dictionary',type=Path,action='append')
    p.add_argument('--resume',action='store_true')
    p.add_argument('--tools',type=Path,default=Path('/tmp/ptx-concolic'))
    p.add_argument('--ptxas',type=Path,default=Path('/usr/local/cuda-13.0/bin/ptxas'))
    args=p.parse_args()
    arches=[a.upper() for a in args.architectures.split(',')]
    if args.n<1 or args.m<0 or args.duration<1 or not arches or any(a not in TARGETS for a in arches): p.error('invalid workers, duration or architectures')
    root=args.output.resolve(); tools=args.tools.resolve(); application=str(args.application.resolve())
    compiler=args.ptxas.resolve()
    config=dict(candidate_key='name_key-2',mutation_input='ptx-region-1',concolic_jobs='per-arch-1',compile_probe='newest-1',ptxas=str(compiler),compiler=hashlib.sha256(compiler.read_bytes()).hexdigest(),architectures=arches,
                qemu=str(tools/'AFLplusplus/afl-qemu-trace'),symqemu=str(tools/'symqemu/build/symqemu-x86_64'))
    version=subprocess.check_output([str(compiler),'--version']).decode()
    if config['compiler'] != VERIFIED_COMPILER: p.error('IR logger addresses require verified CUDA 13.0.88 compiler hash')
    for executable in [application,config['ptxas'],config['qemu'],*([config['symqemu']] if args.m else [])]:
        if not os.access(executable,os.X_OK): p.error('missing executable: '+executable)
    if root.exists() and not args.resume: p.error('output exists; use --resume')
    if args.resume and not (root/'catalogue.sqlite').exists(): p.error('resume requires an existing catalogue')
    root.mkdir(parents=True,exist_ok=True)
    lock=(root/'campaign.lock').open('w')
    try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError: p.error('campaign already active')
    for name in ('sources','seeds','cases','concolic','mutation','logs'): (root/name).mkdir(exist_ok=True)
    db=sqlite3.connect(root/'catalogue.sqlite'); db.executescript(SCHEMA)
    prior=db.execute("SELECT value FROM metadata WHERE key='config'").fetchone()
    if prior and json.loads(prior[0]) != config: p.error('resume compiler/tool/architecture configuration differs')
    db.execute("INSERT OR REPLACE INTO metadata VALUES('config',?)",(canonical(config),))
    db.execute("INSERT OR REPLACE INTO metadata VALUES('compiler_version',?)",(version,)); db.commit(); recover(db)
    coordinator=Coordinator(root,db,config)
    dictionaries=args.dictionary or [HERE/'ptx.dict']
    seeds=args.seed or [HERE/'generic_sm75.ptx',*sorted(HERE.parent.glob('*.ptx'))]
    for i,seed in enumerate(seeds):
        data=seed.read_bytes(); (root/'seeds'/f'{i:03d}.ptx').write_bytes(data); coordinator.candidate(data,'seed:'+str(seed))
    ctx=mp.get_context('spawn'); events=ctx.Queue(); stop=ctx.Event()
    def stopping(*_): stop.set()
    signal.signal(signal.SIGINT,stopping); signal.signal(signal.SIGTERM,stopping)
    socket_path=root/'candidates.sock'
    if socket_path.exists(): socket_path.unlink()
    server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); server.bind(str(socket_path)); server.listen()
    threading.Thread(target=socket_reader,args=(server,events,stop),daemon=True).start()
    # Reserve a local port for the LibAFL LLMP broker, without hard-coded collisions.
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1',0)); port=reservation.getsockname()[1]
    processes={}; child_groups=set(); activity={}; queues={}; busy={}; start=time.monotonic()
    def launch(name,argv,**kwargs):
        log=(root/'logs'/f'{name}.log').open('wb')
        child=subprocess.Popen(argv,start_new_session=True,stderr=log,**kwargs)
        processes[name]=child; return child
    broker=launch('broker',[application,'--broker',str(port)],stdout=subprocess.PIPE)
    if broker.stdout.readline().strip()!=b'ready': raise RuntimeError('LLMP broker failed')
    publisher=launch('publisher',[application,'--publish',str(port)],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,text=True)
    for i in range(2):
        name=f'analysis{i}'; jobs=ctx.Queue(); queues[name]=jobs
        child=ctx.Process(target=analysis_worker,args=(jobs,events,config,stop)); child.start(); processes[name]=child
    name='replay'; jobs=ctx.Queue(); queues[name]=jobs
    child=ctx.Process(target=replay_worker,args=(jobs,events,config,stop)); child.start(); processes[name]=child
    for i in range(args.m):
        name=f'concolic{i}'; jobs=ctx.Queue(); queues[name]=jobs
        child=ctx.Process(target=concolic_worker,args=(jobs,events,config,stop,i)); child.start(); processes[name]=child
    env=dict(os.environ,AFL_ENTRYPOINT=FORKSERVER_ENTRY)
    for key in ('AFL_CUSTOM_MUTATOR_LIBRARY','AFL_CUSTOM_MUTATOR_ONLY','PTX_OPCODE_DOMAIN','PTX_SYMBOLIC_BEGIN','PTX_SYMBOLIC_END'):
        env.pop(key,None)
    for i in range(args.n):
        launch(f'mutation{i}',[application,'--worker',str(root),str(i),str(port),config['qemu'],config['ptxas'],
               ','.join(str(d.resolve()) for d in dictionaries),','.join(TARGETS[a] for a in arches)],stdout=subprocess.DEVNULL,env=env)
    # Replay previously accepted cases through LLMP on resume; no directory polling.
    for target,source in db.execute("SELECT DISTINCT c.target,s.source FROM compilations c JOIN candidates s USING(key) WHERE c.state='decoded'"):
        publisher.stdin.write(canonical([target,source])+'\n')
    publisher.stdin.flush()
    reported=start
    def handle(msg):
        kind=msg['kind']
        if kind=='candidate': coordinator.candidate(msg['data'],msg['origin'])
        elif kind=='activity': activity[msg['worker']]=msg
        elif kind=='child':
            if msg['active']: child_groups.add(msg['pid'])
            else: child_groups.discard(msg['pid'])
        elif kind=='compiled':
            coordinator.compiled(msg,publisher)
            for name,job in list(busy.items()):
                if name.startswith('analysis') and job['cache_key']==msg['cache_key']: del busy[name]
        elif kind=='replayed':
            db.execute('UPDATE replays SET state=?,owner=NULL,diagnostics=? WHERE cache_key=?',
                       (msg['state'],msg['diagnostics'],msg['cache_key'])); db.commit(); busy.pop('replay',None)
        elif kind=='concolic_done':
            state='running' if msg['interrupted'] else 'completed'
            db.execute('UPDATE concolic SET state=?,result=? WHERE cache_key=?',(state,canonical(msg),msg['cache_key'])); db.commit()
            for name,job in list(busy.items()):
                if name.startswith('concolic') and job['cache_key']==msg['cache_key']: del busy[name]
    try:
        while not stop.is_set() and time.monotonic()-start<args.duration:
            for name,jobs in queues.items():
                if name not in busy and processes[name].is_alive():
                    table='compilations' if name.startswith('analysis') else 'concolic' if name.startswith('concolic') else 'replays'
                    job=coordinator.job(table,name)
                    if job: busy[name]=job; jobs.put(job)
            try: handle(events.get(timeout=.1))
            except queue.Empty: pass
            if time.monotonic()-reported>5:
                status=coordinator.status(activity,processes,start,'running')
                print(canonical({k:status[k] for k in ('elapsed_seconds','mutations','accepted_by_arch','forms_by_arch','replay_states')}),flush=True)
                reported=time.monotonic()
            dead=[name for name,child in processes.items() if (child.poll() is not None if isinstance(child,subprocess.Popen) else not child.is_alive())]
            if dead: raise RuntimeError('campaign worker exited unexpectedly: '+','.join(dead))
    finally:
        stop.set()
        # Ask Rust clients to checkpoint before terminating their process groups.
        for name,child in processes.items():
            if name.startswith('mutation') and child.poll() is None:
                try: os.kill(child.pid,signal.SIGUSR1)
                except ProcessLookupError: pass
        drain_deadline=time.monotonic()+4
        while time.monotonic()<drain_deadline:
            try: handle(events.get(timeout=.1))
            except queue.Empty:
                if all(not child.is_alive() for child in processes.values() if not isinstance(child,subprocess.Popen)): break
        for pid in child_groups: kill_group(pid)
        for name,child in processes.items():
            if isinstance(child,subprocess.Popen):
                if name.startswith('mutation'):
                    try: child.wait(timeout=2)
                    except subprocess.TimeoutExpired: pass
                kill_group(child.pid); child.wait()
            else:
                child.join(timeout=1)
                if child.is_alive(): child.terminate(); child.join()
        # Running leases remain running and are recovered once on next invocation.
        for name,job in busy.items():
            table='compilations' if name.startswith('analysis') else 'concolic' if name.startswith('concolic') else 'replays'
            db.execute(f"UPDATE {table} SET state='running' WHERE cache_key=? AND state='pending'",(job['cache_key'],))
        db.commit()
        status=coordinator.status(activity,processes,start,'stopped')
        print(canonical(status),flush=True)
        # Export only fully decoded, validated lowering witnesses.
        with (root/'catalogue.jsonl').open('w') as output:
            for arch,h,form,ck in db.execute('SELECT * FROM forms ORDER BY arch,hash'):
                witness=db.execute('SELECT c.directory,s.source FROM compilations c JOIN candidates s USING(key) WHERE cache_key=?',(ck,)).fetchone()
                output.write(canonical(dict(architecture=arch,hash=h,signature=json.loads(form),cubin=str(Path(witness[0])/'kernel.cubin'),ptx=witness[1],compiler=config['compiler']))+'\n')
        server.close(); socket_path.unlink(missing_ok=True); db.close()

if __name__=='__main__': main()
