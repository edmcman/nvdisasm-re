"""Bit-exact binary IEEE arithmetic for CPU primitive execution.

Finite arithmetic uses rational numbers and rounds once. NaN results are canonical
quiet NaNs; GPU payload selection is deliberately not claimed as verified.
"""
from fractions import Fraction

FORMATS={16:(5,10),32:(8,23),64:(11,52)}


def unpack(bits,width=32,ftz=False):
    e,m=FORMATS[width];sgn=bits>>(width-1)&1;ex=bits>>m&((1<<e)-1);mant=bits&((1<<m)-1)
    if ex==(1<<e)-1:return ('nan' if mant else 'inf',sgn,None)
    if ex==0:
        if ftz or not mant:return 'zero',sgn,Fraction(0)
        exp=1-((1<<(e-1))-1)-m;sig=mant
    else:exp=ex-((1<<(e-1))-1)-m;sig=(1<<m)|mant
    value=Fraction(sig)*(Fraction(2)**exp)
    return 'finite',sgn,-value if sgn else value


def round_integer(value,mode='RN'):
    n,d=value.numerator,value.denominator;q,r=divmod(n,d)
    if mode=='RM':return q
    if mode=='RP':return q+(r!=0)
    if mode=='RZ':return q+(n<0 and r!=0)
    if mode!='RN':raise NotImplementedError('rounding '+mode)
    return q+(2*r>d or 2*r==d and q&1)


def pack(value,width=32,mode='RN',zero_sign=0,ftz=False):
    e,m=FORMATS[width];bias=(1<<(e-1))-1;maxexp=(1<<e)-1
    sign=int(value<0) if value else zero_sign;v=abs(value)
    if not v:return sign<<(width-1)
    exp=v.numerator.bit_length()-v.denominator.bit_length()
    if v<Fraction(2)**exp:exp-=1
    minimum=1-bias;shift=max(exp,minimum)-m
    rounded=round_integer((-v if sign else v)/(Fraction(2)**shift),mode)
    sig=abs(rounded)
    if sig>=1<<(m+1):sig>>=1;exp+=1
    if exp>bias:
        inf=mode=='RN' or mode=='RP' and not sign or mode=='RM' and sign
        return (sign<<(width-1)) | ((maxexp<<m) if inf else ((maxexp-1)<<m)|((1<<m)-1))
    if sig<1<<m:
        return sign<<(width-1) if ftz else (sign<<(width-1))|sig
    return (sign<<(width-1)) | ((max(exp,minimum)+bias)<<m) | (sig- (1<<m))


def nan(width):
    e,m=FORMATS[width];return (((1<<e)-1)<<m)|(1<<(m-1))


def arithmetic(op,inputs,width=32,mode='RN',ftz=False,sat=False,scale=0):
    parts=[unpack(x,width,ftz) for x in inputs];e,m=FORMATS[width]
    if any(k=='nan' for k,s,v in parts):result=nan(width)
    elif op=='add':
        a,b=parts
        if a[0]==b[0]=='inf' and a[1]!=b[1]:result=nan(width)
        elif a[0]=='inf' or b[0]=='inf':result=inputs[0] if a[0]=='inf' else inputs[1]
        else:
            zero=a[1] if a[0]==b[0]=='zero' and a[1]==b[1] else int(mode=='RM')
            result=pack(a[2]+b[2],width,mode,zero,ftz)
    elif op in ('mul','fma'):
        a,b=parts[:2];sgn=a[1]^b[1]
        invalid=(a[0]=='inf' and b[0]=='zero') or (a[0]=='zero' and b[0]=='inf')
        product_inf=a[0]=='inf' or b[0]=='inf'
        if invalid:result=nan(width)
        elif op=='mul':
            if product_inf:result=(sgn<<(width-1))|(((1<<e)-1)<<m)
            else:result=pack(a[2]*b[2]*(Fraction(2)**scale),width,mode,sgn,ftz)
        else:
            c=parts[2]
            if product_inf and c[0]=='inf' and c[1]!=sgn:result=nan(width)
            elif product_inf:result=(sgn<<(width-1))|(((1<<e)-1)<<m)
            elif c[0]=='inf':result=inputs[2]
            else:
                zero=sgn if (a[0]=='zero' or b[0]=='zero') and c[0]=='zero' and c[1]==sgn else int(mode=='RM')
                result=pack(a[2]*b[2]+c[2],width,mode,zero,ftz)
    else:raise NotImplementedError(op)
    if sat:
        k,s,v=unpack(result,width)
        if k=='nan' or s:return 0
        if k=='inf' or v>1:return pack(Fraction(1),width)
    return result


def float_to_int(bits,src_width,dst_width,signed,mode='RN',ftz=False):
    kind,sign,value=unpack(bits,src_width,ftz)
    if kind in ('nan','inf'):raise NotImplementedError('SASS invalid float-to-int result convention')
    result=round_integer(value,mode)
    lo=-(1<<(dst_width-1)) if signed else 0;hi=(1<<(dst_width-int(signed)))-1
    if not lo<=result<=hi:raise NotImplementedError('SASS out-of-range conversion convention')
    return result&((1<<dst_width)-1)
