import json
import hashlib
import subprocess
from pathlib import Path
from sass.assemble import assemble_instruction
from sass.paths import NVCC, PTXAS
from tests.harness.gpu import Driver, Fixture, Template, compile, encode


def main():
    image = compile('deferred_barrier', arch='SM89')
    template = Template(image, 'sem_deferred_order')
    offsets = [o for o in range(0, len(template.code), 16) if template.decoded(o).klass.mnemonic == 'BAR']
    assert len(offsets) == 1
    offset = offsets[0]
    assert 'DEFER_BLOCKING' in template.decoded(offset).text
    plain = template.patch({offset: (encode('bar__SYNC_II_optionalCount_II', Sb=0, Sc=0), template.control(offset))})
    next_offset = offset + 16
    next_word = int.from_bytes(template.code[next_offset:next_offset + 16], 'little')
    barrier_word = int.from_bytes(template.code[offset:offset + 16], 'little')
    dependency = template.patch({
        offset: (barrier_word, template.control(offset) & ~(7 << 113)),
        next_offset: (next_word, template.control(next_offset) | 1 << 116),
    })
    driver = Driver()
    try:
        results = {}
        for label, binary in (('deferred', image), ('dependency_wait', dependency), ('blocking', plain)):
            fixture = Fixture(name=label, image=binary, kernel=template.kernel, data=bytes(64 * 12),
                              expected=None, output_size=8, block=64)
            results[label] = [driver.run(fixture) for _ in range(8)]
        data = dict(arch=driver.arch, device=driver.device, instruction=template.decoded(offset).text,
                    offset=offset, code=template.code.hex(), observations=results)
        data['tools'] = {name: dict(path=str(Path(path).resolve()),
                                  sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                                  version=subprocess.check_output([path, '--version'], text=True).strip())
                         for name, path in (('nvcc', NVCC), ('ptxas', PTXAS))}
        Path('research/deferred-barrier-SM89.json').write_text(json.dumps(data, indent=1) + '\n')
        for label, runs in results.items():
            print(label, [min(row[32:]) - max(row[:32]) for row in runs])
        load = Template(image, 'sem_deferred_load')
        site = next(o for o in range(0, len(load.code), 16) if load.decoded(o).klass.mnemonic == 'LDS')
        clock = next(o for o in range(site + 16, len(load.code), 16) if load.decoded(o).klass.mnemonic == 'CS2R')
        scoreboard = int.from_bytes(load.code[site:site + 16], 'little') >> 110 & 7
        assert scoreboard < 6
        clock_word = int.from_bytes(load.code[clock:clock + 16], 'little')
        waited = load.patch({clock: (clock_word, load.control(clock) | 1 << (116 + scoreboard))})
        mufu = load.patch({site: (encode('mufu__RRR_RR', Rd=0, Rb=255, mufuop='RCP'), load.control(site))})
        s2r = load.patch({site: (encode('s2r_', Rd=0, SRa='SR_TID.X'), load.control(site))})
        branch = load.patch({site: (int.from_bytes(assemble_instruction('SM89', f'BRA {clock:#x}', addr=site), 'little'),
                                   load.control(site) | 7 << 110)})
        fixed = load.patch({site: (int.from_bytes(assemble_instruction('SM89', 'MOV R0, 0'), 'little'),
                                  load.control(site) | 7 << 110)})
        consumer = next(o for o in range(clock, len(load.code), 16)
                        if load.decoded(o).klass.mnemonic == 'IADD3' and load.decoded(o).env.get('Ra') == 0)
        value = int.from_bytes(load.code[consumer:consumer + 16], 'little')
        normalized = value & ~(255 << 24) | 255 << 24
        for label, binary in (('load_without_wait', image), ('load_wait', waited),
                              ('mufu_without_wait', mufu), ('s2r_without_wait', s2r),
                              ('branch', branch), ('fixed', fixed)):
            binary = load.patch({consumer: (normalized, load.control(consumer))}, binary)
            fixture = Fixture(name=label, image=binary, kernel=load.kernel, data=bytes(64 * 12),
                              expected=None, output_size=8, block=64)
            results[label] = [driver.run(fixture) for _ in range(8)]
            print(label, [min(row[32:]) - max(row[:32]) for row in results[label]])
        data['load_code'] = load.code.hex()
        data['load_wait'] = dict(load=site, clock=clock, scoreboard=scoreboard)
        data['load_normalize'] = dict(offset=consumer, original=value, patched=normalized)
        Path('research/deferred-barrier-SM89.json').write_text(json.dumps(data, indent=1) + '\n')
    finally: driver.close()


if __name__ == '__main__': main()
