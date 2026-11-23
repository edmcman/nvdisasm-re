from dataclasses import dataclass
from fractions import Fraction
import z3
from access_relations import Atom, Polynomial, signed
from sass.semantics.fp import softfloat

EPS = Fraction(1, 1 << 23)


def q(value): return z3.RealVal(str(Fraction(value)))


@dataclass(frozen=True)
class FloatBounds:
    target: object
    offsets: tuple
    lower: Fraction
    upper: Fraction


class NumericRelations:
    def __init__(self, analysis, state):
        self.analysis, self.graph, self.normal = analysis, analysis.graph, analysis.normal
        self.state = state
        self.solver = z3.SolverFor('QF_NRA')
        self.solver.set(timeout=2000)
        self.words, self.floats, self.intervals, self.symbols = {}, {}, {}, {}
        self.active = set()
        self.polynomials = {}
        self.floors = {}
        self.moduli = []
        self.serial = 0
        for row in state.rows:
            self.solver.add(self.row(dict(row)) == 0)
        for row, upper in state.bounds:
            self.solver.add(self.row(dict(row)) <= q(upper))
        for atom in sorted(self.symbols, key=lambda a: a.operation != 'word' or not self.basic(a.arguments[0])):
            self.link(atom)

    def row(self, expression):
        total = q(0)
        for factors, coefficient in expression.items():
            term = q(coefficient)
            for atom in factors:
                if atom not in self.symbols: self.symbols[atom] = z3.Real('a'+str(len(self.symbols)))
                term *= self.symbols[atom]
            total += term
        return total

    def prove(self, predicate):
        solver = z3.SolverFor('QF_NRA')
        solver.set(timeout=2000)
        solver.add(*self.solver.assertions(), z3.Not(predicate))
        return solver.check() == z3.unsat

    def auxiliary(self, prefix):
        self.serial += 1
        return z3.Real(f'{prefix}{self.serial}')

    def link(self, atom):
        if atom.operation != 'word': return
        source = atom.arguments[0]
        try: word = self.polynomial(source)
        except NotImplementedError: return
        half = 1 << (source.width-1)
        match source.width:
            case 1: interpreted = word
            case _ if self.prove(word < half): interpreted = word
            case _ if self.prove(word >= half): interpreted = word-(1 << source.width)
            case _: interpreted = z3.If(word >= half, word-(1 << source.width), word)
        self.solver.add(self.symbols[atom] == interpreted)

    def fresh(self, value, lower=0, upper=None):
        upper = (1 << value.width)-1 if upper is None else upper
        result = z3.Real('v'+str(value.index))
        self.words[value] = result
        self.intervals[value] = (Fraction(lower), Fraction(upper))
        self.solver.add(result >= q(lower), result <= q(upper))
        return result

    def restrict(self, value, lower, upper):
        oldlo, oldhi = self.intervals[value]
        lo, hi = max(oldlo, lower), min(oldhi, upper)
        self.intervals[value] = (lo,hi)
        self.solver.add(self.words[value] >= q(lo), self.words[value] <= q(hi))

    def modulo(self, expression, width):
        m = 1 << width
        if self.prove(z3.And(expression >= 0, expression < m)): return expression
        if self.prove(z3.And(expression >= -m, expression <= 0)): return z3.If(expression == 0, q(0), expression+m)
        quotient, result = self.auxiliary('quotient'), self.auxiliary('remainder')
        self.solver.add(expression == result+m*quotient, result >= 0, result < m)
        self.moduli.append((expression, m, result))
        return result

    def reduce_moduli(self):
        for _ in range(4):
            remaining = []
            for expression, modulus, result in self.moduli:
                match self.prove(z3.And(expression >= 0, expression < modulus)), self.prove(z3.And(expression >= -modulus, expression <= 0)):
                    case True, _: self.solver.add(result == expression)
                    case _, True: self.solver.add(result == z3.If(expression == 0, 0, expression+modulus))
                    case _: remaining.append((expression, modulus, result))
            if len(remaining) == len(self.moduli): break
            self.moduli = remaining

    def floor(self, expression, denominator=1):
        if denominator == 1: return expression
        key=(z3.simplify(expression).sexpr(),denominator)
        if key in self.floors: return self.floors[key]
        result = self.auxiliary('floor')
        self.floors[key]=result
        self.solver.add(result*denominator <= expression, expression < (result+1)*denominator)
        return result

    def polynomial(self,term):
        if term not in self.polynomials: self.polynomials[term]=self.compute_polynomial(term)
        return self.polynomials[term]

    def compute_polynomial(self, term):
        if isinstance(term, Polynomial):
            result=q(0)
            for factors,c in term.terms:
                product=q(c if term.width == 1 else signed(c,term.width))
                for a in factors: product *= self.polynomial(a)
                result += product
            return self.modulo(result, term.width)
        match term:
            case Atom('input', width, (name,)):
                if (name, width) in self.graph.inputs: return self.word(self.graph.inputs[name,width])
                if name.startswith('cb'):
                    address = int(name[2:], 16)
                    for (base, size), value in self.graph.inputs.items():
                        if base.startswith('cb') and int(base[2:],16) <= address and address*8+width <= int(base[2:],16)*8+size:
                            return self.modulo(self.floor(self.word(value),1 << ((address-int(base[2:],16))*8)),width)
                raise NotImplementedError('unavailable input')
            case Atom('pure', _, (index, *_)) | Atom('opaque', _, (index,)):
                return self.word(self.graph.values[index])
            case Atom('extract', width, (source, _, lo)):
                return self.modulo(self.floor(self.polynomial(source), 1 << lo), width)
            case Atom('zext', _, (source,)): return self.polynomial(source)
            case Atom('and', width, (left, right)):
                for source, mask in ((left, right.constant_value()), (right, left.constant_value())):
                    if mask is not None and mask & (mask+1) == 0:
                        return self.modulo(self.polynomial(source), mask.bit_length())
                raise NotImplementedError('noncontiguous bit mask')
            case Atom('shr', _, (source, shift)):
                c=shift.constant_value()
                if c is None: raise NotImplementedError('variable shift')
                return self.floor(self.polynomial(source),1<<c)
            case Atom('concat', _, parts):
                result=q(0)
                for part in parts: result=result*(1<<part.width)+self.polynomial(part)
                return result
            case _: raise NotImplementedError(term)

    def basic(self, term):
        match term:
            case Polynomial(_, terms): return all(self.basic(a) for m,_ in terms for a in m)
            case Atom('input'): return True
            case _: return False

    def word(self, value):
        value = self.normal.aliases.get(value, value)
        if value in self.words: return self.words[value]
        if value in self.active: raise NotImplementedError('cyclic value')
        self.active.add(value)
        result = self.fresh(value)
        if value in self.graph.numeric_operations:
            self.conversion(value, *self.graph.numeric_operations[value])
        else:
            op,args=value.op,value.args
            match op:
                case 'input':
                    interval=self.analysis.range_context.word(self.normal(value))
                    self.restrict(value, interval.lower, interval.upper)
                    if hasattr(self.analysis, 'value'):
                        row = self.analysis.value(value)
                        upper = self.state.query(row)
                        negative = self.state.query({m: -c for m, c in row.items()})
                        if upper is not None and negative is not None and negative <= 0:
                            self.restrict(value, -negative, upper)
                case 'const': self.solver.add(result==args[0]); self.restrict(value,args[0],args[0])
                case 'phi': pass
                case 'primitive' if args[0] == 'mufu_rcp':
                    source=self.float(args[1])
                    if source.offsets != (0, 0): raise NotImplementedError('reciprocal of an approximate float')
                    f=source.target
                    lo,hi=Fraction(1)/source.upper,Fraction(1)/source.lower
                    self.floats[value]=FloatBounds(1/f,(-1,1),lo*(1-2*EPS),hi*(1+2*EPS))
                    self.float_bits(value)
                case 'add' | 'sub' | 'mul' | 'neg':
                    for operand in args: self.word(operand)
                    normalized=self.normal(value)
                    raw=q(0)
                    for factors,c in normalized.terms:
                        product=q(signed(c,normalized.width))
                        for atom in factors: product*=self.polynomial(atom)
                        raw+=product
                    if self.basic(normalized):
                        # Conditional integer interpretation: modular overflow is checked separately.
                        self.solver.add(raw >= -(1<<value.width),raw < 1<<value.width)
                    self.solver.add(result == self.modulo(raw,value.width))
                    intervals = [self.interval(a) for a in args]
                    match op:
                        case 'add': lower, upper = intervals[0][0]+intervals[1][0], intervals[0][1]+intervals[1][1]
                        case 'sub': lower, upper = intervals[0][0]-intervals[1][1], intervals[0][1]-intervals[1][0]
                        case 'neg': lower, upper = -intervals[0][1], -intervals[0][0]
                        case 'mul':
                            products = [a*b for a in intervals[0] for b in intervals[1]]
                            lower, upper = min(products), max(products)
                    if self.basic(normalized):
                        lower, upper = 0, 0
                        for factors, coefficient in normalized.terms:
                            lo = hi = signed(coefficient, normalized.width)
                            for atom in factors:
                                bounds = self.interval(self.graph.inputs[atom.arguments[0], atom.width])
                                products = [x*y for x in (lo, hi) for y in bounds]
                                lo, hi = min(products), max(products)
                            lower, upper = lower+lo, upper+hi
                    modulus = 1 << value.width
                    if lower // modulus == upper // modulus:
                        self.restrict(value, lower % modulus, upper % modulus)
                    elif self.basic(normalized) and lower >= 0:
                        self.restrict(value, lower, modulus-1)
                    if op=='add':
                        for source,constant in (args,args[::-1]):
                            if constant.op!='const' or source not in self.floats: continue
                            old=self.floats[source]
                            c=signed(constant.args[0],value.width)
                            exponent=(c+(1<<22))//(1<<23)
                            displacement=c-exponent*(1<<23)
                            lo,hi=self.interval(source)
                            if not (0x800000 <= lo+c <= hi+c < 0x7f800000): continue
                            lower=softfloat.unpack(int(lo+c),32)[2]
                            upper=softfloat.unpack(int(hi+c),32)[2]
                            self.floats[value]=FloatBounds(old.target*q(Fraction(2)**exponent),tuple(x+displacement for x in old.offsets),lower,upper)
                            self.restrict(value,lo+c,hi+c)
                case 'zext': self.solver.add(result==self.word(args[0])); self.restrict(value,*self.interval(args[0]))
                case 'sext':
                    x=self.word(args[0]); self.solver.add(result==z3.If(x>=1<<(args[0].width-1),x+(1<<value.width)-(1<<args[0].width),x))
                case 'concat':
                    raw=q(0)
                    lower, upper = 0, 0
                    for a in args:
                        raw=raw*(1<<a.width)+self.word(a)
                        lo, hi = self.interval(a)
                        lower, upper = lower*(1<<a.width)+lo, upper*(1<<a.width)+hi
                    self.solver.add(result==raw)
                    self.restrict(value,lower,upper)
                case 'extract':
                    source,hi,lo=args
                    x=self.word(source)
                    low,high=self.interval(source)
                    if int(low)//(1<<(hi+1))==int(high)//(1<<(hi+1)):
                        self.restrict(value,int(low)//(1<<lo) % (1<<value.width),int(high)//(1<<lo) % (1<<value.width))
                    self.solver.add(result==self.modulo(self.floor(x,1<<lo),value.width))
                case 'shr':
                    source,shift=args
                    if shift.op!='const': raise NotImplementedError('variable shift')
                    self.solver.add(result==self.floor(self.word(source),1<<shift.args[0]))
                    lo, hi = self.interval(source)
                    self.restrict(value,int(lo) >> shift.args[0],int(hi) >> shift.args[0])
                case 'not':
                    self.solver.add(result==(1<<value.width)-1-self.word(args[0]))
                    lo, hi = self.interval(args[0])
                    self.restrict(value,(1<<value.width)-1-hi,(1<<value.width)-1-lo)
                case 'eq' | 'ne' | 'ult' | 'ule' | 'slt' | 'sle':
                    a,b=(self.word(x) for x in args)
                    if op.startswith('s'):
                        a=z3.If(a>=1<<(args[0].width-1),a-(1<<args[0].width),a)
                        b=z3.If(b>=1<<(args[1].width-1),b-(1<<args[1].width),b)
                    test={'eq':lambda:a==b,'ne':lambda:a!=b,'ult':lambda:a<b,'ule':lambda:a<=b,'slt':lambda:a<b,'sle':lambda:a<=b}[op]()
                    self.solver.add(result==z3.If(test,1,0))
                    if self.prove(test): self.restrict(value,1,1)
                    elif self.prove(z3.Not(test)): self.restrict(value,0,0)
                case 'ite':
                    condition,yes,no=args
                    c=self.word(condition); a=self.word(yes); b=self.word(no)
                    self.solver.add(result==z3.If(c!=0,a,b))
                    lo, hi = self.interval(condition)
                    match lo, hi:
                        case 0, 0: self.restrict(value,*self.interval(no))
                        case _ if lo > 0: self.restrict(value,*self.interval(yes))
                        case _:
                            x,y=self.interval(yes),self.interval(no)
                            self.restrict(value,min(x[0],y[0]),max(x[1],y[1]))
                    for branch,selected in ((yes,c!=0),(no,c==0)):
                        if branch in self.floats and self.prove(selected): self.floats[value]=self.floats[branch]
                case 'and' | 'or' if value.width==1:
                    a,b=(self.word(x) for x in args)
                    test=z3.And(a!=0,b!=0) if op=='and' else z3.Or(a!=0,b!=0)
                    self.solver.add(result==z3.If(test,1,0))
                case 'or':
                    for a,b in (args,args[::-1]):
                        if b.op=='const' and b.args[0] and b.args[0]&(b.args[0]-1)==0:
                            x=self.word(a); c=b.args[0]
                            if self.prove(x<c): self.solver.add(result==x+c); break
                case 'and':
                    for a,b in (args,args[::-1]):
                        x,y=self.word(a),self.word(b)
                        if self.prove(y==(1<<value.width)-1):
                            self.solver.add(result==x); self.restrict(value,*self.interval(a))
                            if a in self.floats: self.floats[value]=self.floats[a]
                            break
                        if self.prove(y==0): self.solver.add(result==0); break
                        if b.op=='const' and (mask:=b.args[0]):
                            shift=(mask&-mask).bit_length()-1; bits=mask>>shift
                            if bits&(bits+1)==0:
                                self.solver.add(result==self.modulo(self.floor(x,1<<shift),bits.bit_length())*(1<<shift))
                                low,high=self.interval(a)
                                if int(low)//(1<<(shift+bits.bit_length()))==int(high)//(1<<(shift+bits.bit_length())):
                                    self.restrict(value,int(low)&mask,int(high)&mask)
                                break
                case _: raise NotImplementedError(op)
        self.active.remove(value)
        return result

    def float_bits(self,value):
        f=self.floats[value]
        if f.lower<Fraction(2)**-126 or f.upper>Fraction(2)**127: raise NotImplementedError('float range includes exceptional values')
        low=softfloat.pack(f.lower,32,'RM'); high=softfloat.pack(f.upper,32,'RP')
        self.restrict(value,max(0x800000,low-2),min(0x7f7fffff,high+2))

    def float(self,value):
        self.word(value)
        if value not in self.floats: raise NotImplementedError(('unknown float',value.index))
        return self.floats[value]

    def interval(self,value): return self.intervals[self.normal.aliases.get(value,value)]

    def conversion(self,value,op,args):
        result=self.words[value]
        match op.split('.'):
            case ['cvt',mode,'f32','u32']:
                x=self.word(args[0]); lo,hi=self.interval(args[0])
                if lo<1:
                    if not self.prove(x>=1): raise NotImplementedError("possibly zero conversion input")
                    lo=1
                f=z3.Real('f'+str(value.index))
                match mode:
                    case 'rp': self.solver.add(f>=x,f<=x*q(1+EPS))
                    case _: raise NotImplementedError(op)
                self.floats[value]=FloatBounds(f,(0,0),lo,hi*(1+EPS))
                self.float_bits(value)
            case ['cvt','rzi'|'rzi.ntz','u32','f32'] | ['cvt','rzi','ntz','u32','f32']:
                f=self.float(args[0]); a,b=f.offsets
                if b>=0: raise NotImplementedError('upper floating error')
                if not self.prove(z3.And(f.target>0,f.target<=1<<32)): raise NotImplementedError('possibly saturating conversion')
                # One encoding step changes a positive normal value by at most 2*EPS relatively.
                self.solver.add(result<=f.target, result>=f.target*q(1-2*(abs(a)+1)*EPS)-1)
                self.restrict(value,max(0,int(f.lower)-1),min((1<<32)-1,int(f.upper)))
            case _: raise NotImplementedError(op)
