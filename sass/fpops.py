"""Floating-point lowerings: exact (hardware results) or readable (plain formula).

The readable lowering is the arithmetic a person writes: native-width
operators, any rounding mode as round-to-nearest, no denormal flushing, no
fused rounding and no NaN/signed-zero selection rules.

The exact lowering computes s = RN_wide(x) and the exact residual e with
x = s + e (TwoSum for additions, zero for wide-exact products), narrows s with
round-to-nearest, then moves the narrow result one ulp in integer encoding when
the residual says the requested direction differs. Wide formats hold every
product exactly, so the residual sign decides each directed mode correctly.
"""

MODES=('exact','readable')
ROUNDING=['RN','RM','RP','RZ']


def readable(b):
    return b.g.fp=='readable'


def flush(v,size=4):
    """Denormal to signed zero."""
    sign,exponent={4:(0x80000000,0x7f800000),8:(1<<63,0x7ff0000000000000)}[size]
    return f'{v} & ({sign:#x} | ({sign-1:#x} * zext(({v} & {exponent:#x}) != 0)))'


def saturate(size=4):
    one={2:0x3c00,4:0x3f800000,8:0x3ff0000000000000}[size]
    return (f'if (!nan(r) && (0:{size} f< r)) goto <positive>; r = 0; goto <saturated>; '
            f'<positive> if (r f<= {one:#x}:{size}) goto <saturated>; r = {one:#x}; <saturated> ')


def scale_constant(k,size):
    """2^k as a float constant of `size` bytes."""
    return {4:f'{(127+k)<<23:#x}:4',8:f'{(1023+k)<<52:#x}:8'}[size]


def arithmetic(op,xs,size,rnd='RN',scale=0,exact=True):
    """Statements leaving op(xs) * 2^scale, rounded per `rnd`, in `local r:size`."""
    if not exact:
        x=f'{xs[0]} f* {xs[1]} f+ {xs[2]}' if op=='fma' else f'{xs[0]} {"f+" if op=="add" else "f*"} {xs[1]}'
        if scale:x=f'({x}) f* {scale_constant(scale,size)}'
        return f'local r:{size} = {x};'
    if rnd=='RN':return nearest(op,xs,size,scale)
    w=2*size;zero=f'0:{w}' if w<=8 else 'zero'
    code=(f'local zero:{w} = zext(0:8); ' if w>8 else '')+''.join(f'local w{i}:{w} = float2float({x}); ' for i,x in enumerate(xs))
    if op=='mul':
        code+=f'local s:{w} = w0 f* w1; '+(f's = s f* {scale_constant(scale,w)}; ' if scale else '')
        residual=''
    else:
        a,c=('p','w2') if op=='fma' else ('w0','w1')
        if op=='fma':code+=f'local p:{w} = w0 f* w1; '
        code+=(f'local s:{w} = {a} f+ {c}; local v:{w} = s f- {a}; '
               f'local e:{w} = ({a} f- (s f- v)) f+ ({c} f- v); ')
        residual=' f+ e'
    code+=f'local r:{size} = float2float(s); local d:{w} = (s f- float2float(r)){residual}; local negative:1 = r s< 0; '
    # Infinite inputs leave a NaN residual: the result is already exact. Ghidra
    # 12.1.4 orders NaN in binary128 comparisons, so test it explicitly.
    code+='local ordered:1 = !nan(d); '
    if rnd!='RM':code+=f'local above:1 = ordered && ({zero} f< d); '
    if rnd!='RP':code+=f'local below:1 = ordered && (d f< {zero}); '
    code+={'RZ':'r = r - zext((below && !negative) || (above && negative)); ',
           'RP':'r = r + zext(above && !negative) - zext(above && negative); ',
           'RM':'r = r + zext(below && negative) - zext(below && !negative); '}[rnd]
    if rnd=='RM' and op!='mul':
        # An exact zero sum is -0 under RM unless both addends are +0.
        sign=f'({xs[0]} ^ {xs[1]}) | {xs[2]}' if op=='fma' else f'{xs[0]} | {xs[1]}'
        code+=f'r = r | (zext((s f== {zero}) && (({sign}) s< 0)) << {8*size-1}); '
    return code.rstrip()


def nearest(op,xs,size,scale):
    if op=='add':return f'local r:{size} = {xs[0]} f+ {xs[1]};'
    if op=='mul' and not scale:return f'local r:{size} = {xs[0]} f* {xs[1]};'
    # Products of these formats are exact in the doubled width; scaling there
    # is exact too. FMA then rounds the wide sum and the narrowing: rare
    # midpoint cases double-round, unlike the hardware's single rounding.
    w=2*size;wide=[f'float2float({x})' for x in xs]
    if op=='mul':return f'local r:{size} = float2float(({wide[0]} f* {wide[1]}) f* {scale_constant(scale,w)});'
    return (''.join(f'local w{i}:{w} = {x}; ' for i,x in enumerate(wide))+
            f'local r:{size} = float2float(w0 f* w1 f+ w2);')
