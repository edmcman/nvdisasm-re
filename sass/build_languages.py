"""Generate and compile SASS languages, enforcing Ghidra's packed-data limit."""
import argparse,json,os,shutil,subprocess,tempfile,zlib
from concurrent.futures import ProcessPoolExecutor,ThreadPoolExecutor,as_completed
from itertools import product
from pathlib import Path
from sass import gen_sleigh,coverage,fpops

ARCHES=['SM75','SM80','SM86','SM89','SM90','SM100','SM101','SM103','SM120']
LIMIT=16*1024*1024


def generate_one(arch,fp,output):
    """Generate one independent spec/coverage pair in a worker process."""
    output=Path(output)
    gen_sleigh.OUT=output
    generator=gen_sleigh.Gen(arch,fp)
    text=generator.slaspec()
    (output/f'sass_{gen_sleigh.variant(arch,fp)}.slaspec').write_text(text)
    (output/f'sass_{gen_sleigh.variant(arch,fp)}_coverage.json').write_text(
        json.dumps(list(generator.coverage.values()),indent=2)+'\n')
    return dict(arch=arch,fp=fp,lines=text.count('\n'),subtables=len(generator.tables),
                skipped=generator.skipped)


def main():
    p=argparse.ArgumentParser()
    p.add_argument('architectures',nargs='*',metavar='SMxx')
    p.add_argument('--output',type=Path,default=gen_sleigh.OUT)
    p.add_argument('--fp',choices=fpops.MODES,action='append',help='floating-point lowering (default: all)')
    p.add_argument('--jobs',type=int,default=os.cpu_count() or 1,
                   help='parallel SLEIGH compiler processes (default: available CPU count)')
    install=Path(os.environ.get('GHIDRA_INSTALL_DIR',str(Path.home()/'Ghidra/ghidra_12.1.4_PUBLIC')))
    launcher='sleigh.bat' if os.name=='nt' else 'sleigh'
    p.add_argument('--sleigh',type=Path,default=install/'support'/launcher)
    a=p.parse_args();architectures=list(dict.fromkeys(a.architectures or ARCHES))
    if any(arch not in ARCHES for arch in architectures):p.error('supported architectures: '+', '.join(ARCHES))
    if a.jobs<1:p.error('--jobs must be at least 1')
    if not a.sleigh.is_file():p.error('SLEIGH compiler launcher not found; set --sleigh')
    a.output.mkdir(parents=True,exist_ok=True)
    source=Path(__file__).resolve().parents[1]/'processor/SASS/data/languages'
    for name in ('sass_common.sinc','sass.pspec','sass.cspec'):
        if (a.output/name).resolve()!=(source/name).resolve():shutil.copyfile(source/name,a.output/name)
    a.output=a.output.resolve();gen_sleigh.OUT=a.output;modes=list(dict.fromkeys(a.fp or fpops.MODES))
    tasks=list(product(architectures,modes));generated={}
    with ProcessPoolExecutor(max_workers=min(a.jobs,len(tasks))) as pool:
        futures={pool.submit(generate_one,arch,fp,a.output):(arch,fp) for arch,fp in tasks}
        for future in as_completed(futures):
            arch,fp=futures[future];result=future.result();generated[(arch,fp)]=result
            name=gen_sleigh.variant(arch,fp)
            print(f'{name}: generated {result["lines"]} lines, {result["subtables"]} subtables',flush=True)
            for klass,why in result['skipped'][:10]:print('   skipped',klass,why)
    gen_sleigh.write_ldefs()
    def compile_one(arch,fp):
        name=gen_sleigh.variant(arch,fp);spec=a.output/f'sass_{name}.slaspec'
        fd,tmp=tempfile.mkstemp(prefix=spec.stem+'-',suffix='.sla',dir=a.output);os.close(fd)
        try:
            process=subprocess.run([str(a.sleigh),'-t','-e',str(spec),tmp],capture_output=True,text=True)
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
    with ThreadPoolExecutor(max_workers=min(a.jobs,len(tasks))) as pool:
        futures={pool.submit(compile_one,arch,fp):(arch,fp) for arch,fp in tasks}
        for future in as_completed(futures):
            arch,fp=futures[future];result=future.result();completed[(arch,fp)]=result
            name=gen_sleigh.variant(arch,fp)
            print(f'{name}: compiled, {result["decompressed_bytes"]} decompressed bytes',flush=True)
    results=[completed[task] for task in tasks]
    (a.output/'build-results.json').write_text(json.dumps(results,indent=2)+'\n')
    (a.output/'coverage-summary.json').write_text(json.dumps(coverage.summarize(a.output),indent=2)+'\n')

if __name__=='__main__':main()
