"""Additional integer adapters; logical results come from independent test references."""
from tests.harness import autocase_profiles
from tests.harness.case import source
from tests.refs import integer

OPS = {'VIADD', 'UVIMNMX', 'UI2IP', 'MOV64IUR'}
FAMILIES = {op: 'int_simd' if op == 'UVIMNMX' else 'conversions' if op == 'UI2IP' else 'int_alu' for op in OPS}


def generate(c, k, form, profiles):
    from tests.harness import autocases
    op = k.mnemonic
    if profiles != autocase_profiles.names(op, k.name, form):
        raise autocases.Unsupported('incompatible additional integer profiles')
    if op == 'MOV64IUR':
        if form: raise autocases.Unsupported('unhandled pair move modifiers')
        for j, value in enumerate(autocase_profiles.values('move64')):
            s = autocases.Inputs(c, k, form)
            s.operand(('Sb', 'URb'), value, 4, size=8)
            rd = 4 if j & 1 else 2
            out = s.output(('Rd',), rd)
            result = integer.mov(value, 64)
            s.add({out: result & 0xffffffff, 'R'+str(rd+1): result >> 32}, form)
        return c
    if op == 'UI2IP':
        return pack(c, k, form)
    bname = source(k, 'Sb', 'Rb', 'URb')
    if op == 'VIADD':
        allowed = {'fmt', 'isat', 'Ra@negate', bname+'@negate'}
        if set(form)-allowed: raise autocases.Unsupported('unhandled integer add modifiers')
        if form.get('fmt') not in ('32', 'U32', 'S32') or form.get('isat', 'noisat') != 'noisat':
            raise autocases.Unsupported('packed and saturating VIADD remain unresolved')
    else:
        if set(form)-{'fmt', 'relu', 'UPp@not'}:
            raise autocases.Unsupported('unhandled uniform min/max modifiers')
        if form.get('fmt') not in ('U32','S32','U16x2','S16x2','U8x4','S8x4'):
            raise autocases.Unsupported('unsupported uniform min/max format')
        if form.get('relu') not in ('norelu', 'RELU'):
            raise autocases.Unsupported('unknown uniform min/max RELU')
        if form.get('relu') == 'RELU' and form['fmt'].startswith('U'):
            raise autocases.Unsupported('unsigned RELU remains unresolved')
    vectors = (v for profile in profiles for v in autocase_profiles.vectors(profile))
    for j, (a, b, _, pred, _) in enumerate(vectors):
        s = autocases.Inputs(c, k, form)
        s.operand(('Ra', 'URa'), a, 4)
        s.operand((bname,), b, 6)
        if op == 'VIADD':
            result = integer.iadd3(a, b, 0, (form.get('Ra@negate')=='set',
                                          form.get(bname+'@negate')=='set', False))[0]
        else:
            s.predicate(('UPp',), pred, 0)
            effective = pred ^ (form.get('UPp@not') == 'set')
            width = 16 if '16' in form['fmt'] else 8 if '8' in form['fmt'] else 32
            result = integer.vminmax((a,b), width, form['fmt'].startswith('S'),
                                    effective, form['relu']=='RELU')
        dest = s.output(('Rd', 'URd'), (4,6,2)[j % 3])
        s.add({dest: result}, form)
    return c


def pack(c, k, form):
    from tests.harness import autocases
    allowed = {'dstfmt', 'satrelu', 'extract_limited'}
    if set(form)-allowed: raise autocases.Unsupported('unhandled uniform integer pack modifiers')
    dst = form.get('dstfmt')
    if dst not in ('S2','U2','S4','U4','S8','U8','S16','U16'):
        raise autocases.Unsupported('unknown uniform integer pack format')
    mode = form.get('satrelu')
    if mode not in ('nosatrelu_ui2ip', 'SAT', 'RELU'):
        raise autocases.Unsupported('reserved uniform integer pack saturation')
    extract = form.get('extract_limited')
    if extract not in ('H0','H1','24','28'):
        raise autocases.Unsupported('unknown uniform integer pack extraction')
    # SATRELU_ui2ip assigns both SATRELU and RELU to code 2: RELU is a
    # spelling alias for saturation plus rectification (MD _04_REGISTERS).
    spec = autocase_profiles.PROFILES['packed_conversion_forms']
    words = spec['integers']
    width = int(dst[1:])
    for j,a in enumerate(words):
        b = words[(j+3)%len(words)]
        preserved = (spec['preserved']+j*0x10203)&0xffffffff
        s = autocases.Inputs(c,k,form)
        s.operand(('URa',),a,4)
        s.operand(('Sb','URb'),b,6)
        s.operand(('URc',),preserved,8)
        result = integer.i2ip(integer.sext(a,32), integer.sext(b,32), preserved,
                              width,dst.startswith('S'),mode in ('SAT','RELU'),mode=='RELU',extract)
        s.add({s.output(('URd',),(4,6,8,2)[j%4]):result},form)
    return c
