"""Generate and compile SASS languages, enforcing Ghidra's packed-data limit."""
import argparse,json,os,shutil,subprocess,tempfile,zlib
from pathlib import Path
from sass import gen_sleigh,coverage

ARCHES=['SM75','SM80','SM86','SM89','SM90','SM100','SM101','SM103','SM120']
LIMIT=16*1024*1024


def main():
    p=argparse.ArgumentParser()
    p.add_argument('architectures',nargs='*',metavar='SMxx')
    p.add_argument('--output',type=Path,default=gen_sleigh.OUT)
    install=Path(os.environ.get('GHIDRA_INSTALL_DIR',str(Path.home()/'Ghidra/ghidra_12.1.4_PUBLIC')))
    p.add_argument('--sleigh',type=Path,default=install/'Ghidra/Features/Decompiler/os/linux_x86_64/sleigh')
    a=p.parse_args();architectures=a.architectures or ARCHES
    if any(arch not in ARCHES for arch in architectures):p.error('supported architectures: '+', '.join(ARCHES))
    if not a.sleigh.is_file():p.error('native SLEIGH compiler not found; set --sleigh')
    a.output.mkdir(parents=True,exist_ok=True)
    source=Path(__file__).resolve().parents[1]/'processor/SASS/data/languages'
    for name in ('sass_common.sinc','sass.pspec','sass.cspec'):
        if (a.output/name).resolve()!=(source/name).resolve():shutil.copyfile(source/name,a.output/name)
    gen_sleigh.OUT=a.output;gen_sleigh.main(architectures)
    results=[]
    for arch in architectures:
        spec=a.output/f'sass_{arch.lower()}.slaspec'
        fd,tmp=tempfile.mkstemp(prefix=spec.stem+'-',suffix='.sla',dir=a.output);os.close(fd)
        try:
            process=subprocess.run([str(a.sleigh),str(spec),tmp],capture_output=True,text=True)
            if process.returncode:raise RuntimeError(process.stdout+process.stderr)
            size=len(zlib.decompress(Path(tmp).read_bytes()[4:]))
            if size>=LIMIT:raise RuntimeError(f'{arch}: decompressed SLA is {size}, limit {LIMIT}')
            os.replace(tmp,spec.with_suffix('.sla'))
            results.append(dict(arch=arch,decompressed_bytes=size))
            print(f'{arch}: compiled, {size} decompressed bytes',flush=True)
        finally:
            if Path(tmp).exists():Path(tmp).unlink()
    (a.output/'build-results.json').write_text(json.dumps(results,indent=2)+'\n')
    (a.output/'coverage-summary.json').write_text(json.dumps(coverage.summarize(a.output),indent=2)+'\n')

if __name__=='__main__':main()
