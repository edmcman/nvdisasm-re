import sys
import zipfile
from itertools import product
from xml.etree import ElementTree

from sass.paths import variant
from sass.sleigh import build, gen

with zipfile.ZipFile(sys.argv[1]) as archive:
    names = archive.namelist()
    files = set(names)
    assert len(names) == len(files)
    assert all(name.startswith('SASS/') and '..' not in name.split('/') for name in names)
    assert {name for name in files if name.endswith('.jar')} == {'SASS/lib/SASS.jar'}
    assert not any('__pycache__' in name or name.endswith(('.lock', '.pyc')) for name in names)
    assert {'SASS/Module.manifest', 'SASS/README.md'} <= files
    assert {'SASS/examples/write_value.sass', 'SASS/examples/write_value.cubin'} <= files
    assert archive.read('SASS/examples/write_value.cubin').startswith(b'\x7fELF')
    properties = dict(line.split('=', 1) for line in archive.read('SASS/extension.properties').decode().splitlines() if '=' in line)
    assert properties['name'] == 'SASS' and properties['version'] == '12.0.4'
    assert properties['description'] and properties['author'] and properties['createdOn']
    base = 'SASS/data/languages/'
    index = ElementTree.fromstring(archive.read(base + 'sass.ldefs'))
    languages = index.findall('language')
    expected = {variant(arch, fp) for arch, fp in product(build.ARCHES, gen.MODES)}
    assert len(languages) == len(expected)
    assert {language.attrib['variant'] for language in languages} == expected
    for language in languages:
        v = language.attrib['variant']
        assert language.attrib['id'] == f'SASS:LE:64:{v}'
        for name in (language.attrib['slafile'], language.attrib['processorspec'], f'sass_{v}.slaspec'):
            assert base + name in files
        for compiler in language.findall('compiler'):
            assert base + compiler.attrib['spec'] in files
    assert {base + f'sass_{v}.sla' for v in expected} == {name for name in files if name.endswith('.sla')}
    assert {base + f'sass_{v}.slaspec' for v in expected} == {name for name in files if name.endswith('.slaspec')}
    assert {base + name for name in ('sass_common.sinc', 'sass.pspec', 'sass.cspec', 'sass.opinion')} <= files
