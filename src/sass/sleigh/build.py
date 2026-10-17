"""Generate and compile SASS languages, enforcing Ghidra's packed-data limit."""
import argparse,json,os,shutil,subprocess,tempfile,zlib
from concurrent.futures import ProcessPoolExecutor,ThreadPoolExecutor,as_completed
from itertools import product
from pathlib import Path
from sass.sleigh import gen
from sass.paths import ROOT

ARCHES=['SM75','SM80','SM86','SM89','SM90','SM100','SM101','SM103','SM107','SM120']
LIMIT=16*1024*1024
COMPILER_HEAP=int(os.environ.get('SASS_COMPILER_HEAP',4))  # GiB per SLEIGH compiler: SM100 compiled in 1.1-1.9 GiB, unchanged even at -Xmx2g (2026-10-16); without a cap each JVM grew to 11.7 GiB and 18 at once exhausted 93 GiB (2026-10-13)


def compile_workers(jobs,tasks):
    """Parallel SLEIGH compilers that fit in the memory available now, each allowed COMPILER_HEAP."""
    available=next(int(l.split()[1]) for l in open('/proc/meminfo') if l.startswith('MemAvailable:'))>>20
    return max(1,min(jobs,tasks,available//COMPILER_HEAP))


def generate_one(arch,fp,output,coverage=False):
    """Generate one spec and its manifests in a worker process."""
    output=Path(output)
    gen.OUT=output
    generator=gen.Gen(arch,fp,coverage=coverage)
    text=generator.slaspec()
    (output/f'sass_{gen.variant(arch,fp)}.slaspec').write_text(text)
    generator.write_manifests(output)
    return dict(arch=arch,fp=fp,lines=text.count('\n'),subtables=len(generator.tables),
                skipped=generator.skipped)


def compile_all(sleigh,output,tasks,jobs):
    """Compile the generated specs, as many at once as memory allows."""
    def compile_one(arch,fp):
        name=gen.variant(arch,fp);spec=output/f'sass_{name}.slaspec'
        fd,tmp=tempfile.mkstemp(prefix=spec.stem+'-',suffix='.sla',dir=output);os.close(fd)
        try:
            process=subprocess.run([str(sleigh),'-t','-e',str(spec),tmp],capture_output=True,text=True,env=heap)
            if process.returncode:raise RuntimeError(process.stdout+process.stderr)
            diagnostics=process.stdout+process.stderr
            if any(message in diagnostics for message in ('Temporary is written but not read',
                       'operations wrote to temporaries that were not read','read before written')):
                raise RuntimeError(diagnostics)
            size=len(zlib.decompress(Path(tmp).read_bytes()[4:]))
            if size>=LIMIT:raise RuntimeError(f'{name}: decompressed SLA is {size}, limit {LIMIT}')
            os.replace(tmp,spec.with_suffix('.sla'))
            return dict(arch=arch,fp=fp,decompressed_bytes=size,dead_temporaries=0)
        finally:
            if Path(tmp).exists():Path(tmp).unlink()
    completed={}
    heap={**os.environ,'JAVA_TOOL_OPTIONS':f"{os.environ.get('JAVA_TOOL_OPTIONS','')} -Xmx{COMPILER_HEAP}g".strip()}
    with ThreadPoolExecutor(max_workers=compile_workers(jobs,len(tasks))) as pool:
        futures={pool.submit(compile_one,arch,fp):(arch,fp) for arch,fp in tasks}
        for future in as_completed(futures):
            arch,fp=futures[future];result=future.result();completed[(arch,fp)]=result
            name=gen.variant(arch,fp)
            print(f'{name}: compiled, {result["decompressed_bytes"]} decompressed bytes',flush=True)
    return [completed[task] for task in tasks]


def main():
    p=argparse.ArgumentParser()
    p.add_argument('architectures',nargs='*',metavar='SMxx')
    p.add_argument('--output',type=Path,default=gen.OUT)
    p.add_argument('--fp',choices=gen.MODES,action='append',help='floating-point lowering (default: all)')
    p.add_argument('--jobs',type=int,default=os.cpu_count() or 1,
                   help='parallel SLEIGH compiler processes (default: available CPU count)')
    p.add_argument('--coverage',action='store_true',help='collect detailed semantic coverage (slower)')
    p.add_argument('--generate-only',action='store_true',help='write specs and manifests without compiling (no Ghidra needed)')
    install=Path(os.environ.get('GHIDRA_INSTALL_DIR',str(Path.home()/'Ghidra/ghidra_12.1.4_PUBLIC')))
    launcher='sleigh.bat' if os.name=='nt' else 'sleigh'
    p.add_argument('--sleigh',type=Path,default=install/'support'/launcher)
    a=p.parse_args();architectures=list(dict.fromkeys(a.architectures or ARCHES))
    if any(arch not in ARCHES for arch in architectures):p.error('supported architectures: '+', '.join(ARCHES))
    if a.jobs<1:p.error('--jobs must be at least 1')
    if not a.generate_only and not a.sleigh.is_file():p.error('SLEIGH compiler launcher not found; set --sleigh')
    a.output.mkdir(parents=True,exist_ok=True)
    source=ROOT/'processor/SASS/data/languages'
    for name in ('sass_common.sinc','sass.pspec','sass.cspec'):
        if (a.output/name).resolve()!=(source/name).resolve():shutil.copyfile(source/name,a.output/name)
    a.output=a.output.resolve();gen.OUT=a.output;modes=list(dict.fromkeys(a.fp or gen.MODES))
    tasks=list(product(architectures,modes));generated={}
    with ProcessPoolExecutor(max_workers=min(a.jobs,len(tasks))) as pool:
        futures={pool.submit(generate_one,arch,fp,a.output,a.coverage):(arch,fp) for arch,fp in tasks}
        for future in as_completed(futures):
            arch,fp=futures[future];result=future.result();generated[(arch,fp)]=result
            name=gen.variant(arch,fp)
            print(f'{name}: generated {result["lines"]} lines, {result["subtables"]} subtables',flush=True)
            for klass,why in result['skipped'][:10]:print('   skipped',klass,why)
    gen.write_ldefs()
    if not a.generate_only:
        results=compile_all(a.sleigh,a.output,tasks,a.jobs)
        (a.output/'build-results.json').write_text(json.dumps(results,indent=2)+'\n')

if __name__=='__main__':main()
