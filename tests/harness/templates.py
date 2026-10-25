"""Compiled wrappers with validated, named instruction patch sites on SM89 and SM90."""
from functools import cache
from sass import launch
from sass.assemble import elf
from sass.decode import cubin
from tests.harness.gpu import Template, compile, encode

SCOREBOARDS = 0x3f << 116


def offsets(t, mnemonic):
    return [o for o in range(0, len(t.code), 16) if t.decoded(o).klass.mnemonic == mnemonic]


def require(t, offset, mnemonic, **fields):
    d = t.decoded(offset)
    if d.klass.mnemonic != mnemonic or any(d.env.get(n) != v for n, v in fields.items()):
        raise RuntimeError(f'{t.arch} {t.kernel} at {offset:#x}: expected {mnemonic} {fields}, got {d.text}')


@cache
def cx_wrapper(arch="SM89"):
    t = Template(compile('bmsk_cx_semantics', arch=arch), 'sem_cx')
    meta = cubin.kernel_image(t.image, t.kernel)
    if meta['parameter_base'] != elf.PROFILES[arch].parameter_base or [p['size'] for p in meta['parameters']] != [8, 8]:
        raise RuntimeError('unexpected CX wrapper parameter ABI')
    bars = offsets(t, 'BAR')
    targets, stores = offsets(t, 'BMSK'), offsets(t, 'STG')
    if len(bars) != 4 or len(targets) != 1 or len(stores) != 1 or bars != list(range(bars[0], bars[0] + 64, 16)):
        raise RuntimeError(f'{arch}: compiler changed CX wrapper patch sites')
    target, store = targets[0], stores[0]
    require(t, target, 'BMSK', Rd=0, Ra=2, Rb=9 if arch == 'SM90' else 5)
    after = target + (16 if arch == 'SM90' else 32)
    if arch == 'SM89': require(t, target + 16, 'IMAD', Rd=4)
    require(t, after, 'LOP3', Rd=7, Ra=0, Rc=11 if arch == 'SM90' else 7)
    require(t, store, 'STG', Rb=7)
    if bars[-1] + 16 != target or store != after + 16:
        raise RuntimeError(f'{arch}: compiler changed CX wrapper ordering')
    loads = offsets(t, 'LDG')
    if len(loads) != 1: raise RuntimeError('compiler changed CX input loads')
    require(t, loads[0], 'LDG', Rd=2)
    descriptors = [o for o in offsets(t, 'ULDC') if t.decoded(o).env.get('Sa_addr') == launch.GLOBAL_DESCRIPTOR[arch]]
    if len(descriptors) != 1: raise RuntimeError('compiler changed CX descriptor setup')
    require(t, descriptors[0], 'ULDC', URd=4, Sa_bank=0)
    t.sites = dict(setup=bars, target=target, after=after)
    control = (t.control(target) & ~(31 << 105)) | (15 << 105) | SCOREBOARDS
    return t, control


def cx_image(cls, fields, units=128, offset=4, after=("nop_", {}), arch="SM89"):
    t, control = cx_wrapper(arch)
    bits, shift = launch.cx_layout(arch)
    setup = [
        ('uldc_const__RCR', dict(URd=0, Sa_bank=0, Sa_addr=elf.PROFILES[arch].parameter_base, sz='64')),
        ('ushf__URuIUR_URIR', dict(URd=0, URa=0, Sb=shift, URc=1, dir='R', fmt='U64', hilo='LO')),
        ('ushf__URuIUR_URIR', dict(URd=1, URa='URZ', Sb=shift, URc=1, dir='R', fmt='U32', hilo='HI')),
        ('ulop3_lut__URuIUR_URIR', dict(URd=1, URa=1, Sb=units << (bits - 32), URc='URZ', imm8=0xfc, UPp='UPT')),
    ]
    words = dict(zip(t.sites['setup'], setup))
    words[t.sites['target']] = cls, dict(fields, URb=0, Sb_offset=offset) if 'Sb_offset' not in fields else fields
    words[t.sites['after']] = after
    image = t.patch({o: (encode(c, arch=arch, **f), control) for o, (c, f) in words.items()})
    if Template(image, t.kernel).decoded(t.sites['target']).klass.name != cls:
        raise RuntimeError(f'{cls}: patched word decoded differently')
    return image


@cache
def carry_slots(arch="SM89"):
    t = Template(compile('carry_semantics', arch=arch), 'sem_probe')
    slots = offsets(t, 'BAR')
    if arch == 'SM90' and len(slots) == 8:
        # ptxas places output-address arithmetic between the last two barriers.
        # Hoist it before the patch region, while R11 still holds the thread index.
        middle = [o for o in range(slots[0], slots[-1], 16) if o not in slots]
        if len(middle) != 1: raise RuntimeError('compiler changed carry address calculation')
        require(t, middle[0], 'IMAD', Rd=8, Ra=11, Sb=4, Rc=8)
        if t.decoded(middle[0]).klass.name != 'imad_wide__RsIR_RIR': raise RuntimeError('expected wide output address')
        region = [middle[0], *slots]
        words = {o: (int.from_bytes(t.code[src:src + 16], 'little'), t.control(src))
                 for o, src in zip(range(slots[0], slots[-1] + 16, 16), region)}
        t = Template(t.patch(words), t.kernel)
        slots = offsets(t, 'BAR')
    if len(slots) != 8 or slots != list(range(slots[0], slots[0] + 128, 16)):
        raise RuntimeError(f'{arch}: compiler changed reserved probe slots')
    final = slots[-1] + 16
    require(t, final, 'IMAD', Ra=2, Rb=5, Rc=6, Rd=11)
    t.sites = dict(slots=slots, final=final)
    return t


def slot_image(sequence, final=None, arch="SM89"):
    t = carry_slots(arch); control = t.control(t.sites['final'])
    if len(sequence) > len(t.sites['slots']): raise ValueError('probe sequence exceeds reserved slots')
    words = {o: (w, control | SCOREBOARDS if i == 0 else control) for i, (o, w) in enumerate(zip(t.sites['slots'], sequence))}
    words[t.sites['final']] = (encode('nop_', arch=arch) if final is None else final, control)
    return t.patch(words)


def schedule(base, stall=4, wrb=7, wait=0):
    return (base & ~(0x1fffff << 105)) | stall << 105 | wrb << 110 | 7 << 113 | wait << 116


def variable_latency_fixture(name, op, triples, expected, arch="SM89", **kw):
    from tests.harness.gpu import Fixture
    t = carry_slots(arch); base = t.control(t.sites['final']); nop = encode('nop_', arch=arch)
    words = {o: (op if i == 0 else nop, schedule(base, stall=2 if i == 0 else 1, wrb=0 if i == 0 else 7, wait=0x3f if i == 0 else 0)) for i, o in enumerate(t.sites['slots'])}
    words[t.sites['final']] = (nop, schedule(base, stall=4, wait=1))
    return Fixture(name=name, image=t.patch(words), kernel=t.kernel, data=rows(triples), expected=expected, visited=(t.sites['slots'][0],), exit=True, **kw)


def without_barriers(image, arch="SM89"):
    t = carry_slots(arch)
    return t.patch({o: (encode('nop_', arch=arch), t.control(o)) for o in t.sites['slots']}, image)


def rows(triples):
    import struct
    return b''.join(struct.pack('<III', *t) for t in triples)


def cx_fixture(name, cls, fields, triples, expected, units=128, offset=4, after=("nop_", {}), arch="SM89", **kw):
    from tests.harness.gpu import Fixture
    t, _ = cx_wrapper(arch)
    return Fixture(name=name, image=cx_image(cls, fields, units, offset, after, arch), kernel=t.kernel, data=rows(triples), expected=expected,
                   visited=(t.sites['target'],), modes=('exact', 'readable'), **kw)


def slot_fixture(name, sequence, triples, expected, target, final=None, nobarrier_emulation=False, arch="SM89", **kw):
    """target is a reserved slot index, or 8 for the final placeholder."""
    from tests.harness.gpu import Fixture
    t = carry_slots(arch)
    offset = t.sites['final'] if target == 8 else t.sites['slots'][target]
    image = slot_image(sequence, final, arch)
    return Fixture(name=name, image=image, kernel=t.kernel, data=rows(triples), expected=expected, visited=(offset,), exit=True,
                   emulator_image=without_barriers(image, arch) if nobarrier_emulation else None, **kw)
