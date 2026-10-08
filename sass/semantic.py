"""Operand values for p-code, independent of assembly display.

The opaque ABI is (register and immediate inputs...).
Primitive calls add the class ordinal and decoded scalar inputs. Modifiers and
scheduling operands without value semantics stay in the instruction bytes.
The coverage manifest records names and widths in that order. Unknown stateful
operands are explicit runtime primitives, never fabricated constants.
"""
from dataclasses import dataclass
import re
from sass import mdexpr, pydecode


def prune_unused_values(lines):
    """Remove unused operand materialization, including its dependency chain."""
    lines=list(lines)
    while True:
        names=re.findall(r'\b\w+\b',' '.join(lines))
        dead=[]
        for i,line in enumerate(lines):
            match=re.fullmatch(r'local (\w+):\d+ = .*;',line)
            if match and names.count(match[1])==1:dead.append(i)
        if not dead:return lines
        lines=[line for i,line in enumerate(lines) if i not in dead]


@dataclass(frozen=True)
class Value:
    symbol: str
    size: int
    kind: str = 'value'


class Builder:
    def __init__(self, gen, klass, guard, spans):
        from sass.gen_sleigh import ENUM_FILE, REG_FILES
        self.g, self.k, self.spans = gen, klass, spans
        self.body = [f'build {guard};'] if guard else []
        self.refs, self.values, self.outputs, self.inventory = [], {}, {}, []
        self.selector_guards = {}
        self.native_constraints = []
        self.native_supported = True
        self.serial = 0
        from sass.operations import OPERATION_FAMILIES
        self.decode_values = klass.mnemonic in OPERATION_FAMILIES
        self.guard_name = next((a.name for a in klass.format if a.kind == 'guard'), None)
        roles = {o['name']: o for o in gen.roles.get(klass.name, {}).get('operands', [])}
        for atom in klass.operand_types.values():
            if atom.kind == 'guard': continue
            name, file = atom.name, ENUM_FILE.get(atom.type)
            record = roles.get(name, {})
            if file:
                if name in gen.dynamic_inputs(klass):
                    src = gen.source(klass, name)
                    sym, size = gen.dynregsem(klass, name, src[1], file, gen.table_values(src[1], src[2], src[3]) if src[0] == 'table' else None)
                    self.refs.append(('sub', sym))
                    self.values[name] = Value(sym, size, 'register')
                    self.inventory.append(dict(name=name, type=atom.type, kind='register', bytes=size))
                    continue
                span = record.get('span') or 1
                if isinstance(span, str): span = spans[name]
                # A computed zero span is genuinely absent, not a one-register access.
                if name in spans: span = spans[name]
                if not span:
                    self.values[name] = Value('0:4', 4, 'zero-span')
                    self.inventory.append(dict(name=name, type=atom.type, kind='zero-span'))
                    continue
                size = REG_FILES[file][1] * span
                src = gen.source(klass, name)
                if not src:
                    enum=gen.arch.enums.get(atom.type,{})
                    if len(set(enum.values()))==1:
                        value=1 if file in ('P','UP') else 0
                        self.values[name]=(self.local(size,'zext(0:8)','default') if size>8 else Value(f'{value}:{size}',size,'default'))
                        self.inventory.append(dict(name=name,type=atom.type,kind='default'))
                    else:
                        self.values[name] = self.unknown(atom,size)
                        self.inventory.append(dict(name=name,type=atom.type,kind='runtime-primitive'))
                    continue
                f = src[1]
                from sass.gen_sleigh import pieces
                mapping = (gen.table_values(f, src[2], src[3]) if src[0] == 'table'
                           else {v: v * src[2] for v in range(1 << f.width)}
                           if len(pieces(f)) != 1 or src[2] != 1 else None)
                size = REG_FILES[file][1] * span
                if 'write' in record.get('role', []):
                    sym = gen.regsem(f, file, span, True, mapping)
                    self.refs.append(('sub', sym))
                    self.outputs[name] = Value(sym, size, 'register')
                if 'read' in record.get('role', []) or not record.get('role'):
                    sym = gen.regsem(f, file, span, False, mapping)
                    self.refs.append(('sub', sym))
                    self.values[name] = Value(sym, size, 'register')
                self.inventory.append(dict(name=name, type=atom.type, kind='register', bytes=size))
            elif atom.type not in ('C', 'CX', 'A', 'DESC'):
                immediate = atom.kind=='operand' and pydecode.IMM.match(atom.type)
                if not (self.decode_values or immediate) or atom.type=='REUSE':
                    self.inventory.append(dict(name=name,type=atom.type,kind='raw-instruction'))
                    continue
                self.values[name] = self.scalar(name, atom)
                self.inventory.append(dict(name=name, type=atom.type, kind=self.values[name].kind,
                                           bytes=self.values[name].size))
        # Attributes are independent decoded inputs. Native overrides interpret them
        # in the operand's type context; opaque/primitive calls receive their bits.
        attrs = set()
        for _, rule in klass.rules:
            if rule[0] == 'attr': attrs.add((rule[1], rule[2]))
            if rule[0] == 'table':
                attrs.update((a[1], a[2]) for a in rule[2] if a[0] == 'attr')
        for name, attr in sorted(attrs):
            if name != self.guard_name:
                if not self.decode_values:
                    self.inventory.append(dict(name=name+'@'+attr,type='attribute',kind='raw-instruction'))
                    continue
                key = name + '@' + attr
                self.values[key] = self.scalar((name, attr), size=1)
                self.inventory.append(dict(name=key, type='attribute', kind='decoded', bytes=1))
        for atom in klass.operand_types.values():
            if atom.type in ('C', 'CX', 'A', 'DESC'):
                if not self.decode_values:
                    self.inventory.append(dict(name=atom.name,type=atom.type,kind='raw-instruction'))
                    continue
                self.values[atom.name] = self.compound(atom)
                self.inventory.append(dict(name=atom.name, type=atom.type,
                                           kind=self.values[atom.name].kind, bytes=self.values[atom.name].size))

    def local(self, size, expr, kind='value'):
        name = f'sv{self.serial}'; self.serial += 1
        if size>256:
            name=f'SEMTMP{self.serial}_{size}'
            self.g.temporaries[name]=size
            self.body.append(f'{name} = {expr};')
        else:self.body.append(f'local {name}:{size} = {expr};')
        return Value(name, size, kind)

    def expression(self, size, expr, kind='value'):
        """Reusable value calculation; emitted only when an instruction uses it."""
        from sass.gen_sleigh import pattern
        names=set(re.findall(r'\b\w+\b',expr))
        refs=[r for r in self.refs if r[1] in names]
        table=self.g.subtable('value',[('""',pattern([],refs),'',f'local t:{size} = {expr}; export t;')])
        self.refs.append(('sub',table))
        return Value(table,size,kind)

    def scalar(self, name, atom=None, size=None):
        from sass.gen_sleigh import pieces, pattern, eq, default_value
        src = self.g.attr_source(self.k, *name) if isinstance(name, tuple) else self.g.source(self.k, name)
        size = size or (8 if atom and re.match(r'(?:R?S|U)Imm', atom.type) and
                        int(atom.args.split('/')[0] or '32') > 32 else
                        8 if atom and atom.type == 'F64Imm' else
                        2 if atom and atom.type == 'F16Imm' else 4)
        if atom and atom.type in ('SImm','RSImm','UImm') and atom.name in ('Sa','Sb','Sc'):
            size=max(size,self.source_size(atom.name))
        if src is None:
            default = default_value(self.g.arch, atom) if atom else None
            if default is None and atom and atom.type in self.g.arch.enums:
                default = self.g.arch.enums[atom.type].get(atom.default)
                if default is None and len(set(self.g.arch.enums[atom.type].values()))==1:
                    default=next(iter(self.g.arch.enums[atom.type].values()))
            if default is not None: return Value(f'{default & ((1 << (8*size))-1):#x}:{size}', size, 'default')
            return self.unknown(atom, size)
        f = src[1]
        if src[0] == 'table':
            rows = self.g.table_values(f, src[2], src[3])
            cons = [('""', pattern(eq(self.g, f, raw)), '',
                     f'export {value & ((1 << (size*8))-1):#x}:{size};')
                    for raw, value in rows.items() if isinstance(value, int)]
            sym = self.g.subtable('v', cons)
            self.refs.append(('sub', sym))
            return Value(sym, size, 'table')
        refs, expr, shift = [], None, 0
        for tok, lo, hi, width in reversed(pieces(f)):
            field = self.g.field(tok, lo, hi, '_sem')
            refs.append((tok, field))
            expr = field if expr is None else f'({field} << {shift}) | ({expr})'
            shift += width
        if atom and atom.type in ('SImm', 'RSImm') and f.width < 64:
            sign=1 << (f.width-1)
            expr=f'(({expr}) ^ {sign}) - {sign}'
        if src[2] != 1: expr = f'({expr}) * {src[2]}'
        if atom and atom.type == 'F64Imm': expr = f'({expr}) << 32'
        # convertFloatType is resolved by a separate format subtable.
        conv = next((r[3] for _, r in self.k.rules if r[0]=='op' and r[1]==name and r[3]), None)
        if conv:
            return self.converted_immediate(atom, src, size, conv)
        # These expressions depend only on instruction bits, not machine state.
        # A constructor action exports a constant without runtime COPY/shift ops.
        # Anchor the computed operand to this instruction. Ghidra 12.1's native
        # compiler crashes on a computed export without an instruction anchor.
        sym = self.g.subtable('v', [('t', pattern([], refs), f' [ t = inst_start * 0 + ({expr}); ]', f'export *[const]:{size} t;')])
        self.refs.append(('sub', sym))
        return Value(sym, size, 'decoded')

    def converted_immediate(self, atom, src, size, conv):
        from sass.gen_sleigh import pattern, pieces, eq
        from itertools import product
        parts = [p.strip() for p in conv.split(',')]
        names = sorted({c.split('==')[0].strip() for p in parts[0:-1:2] for c in p.split('||')})
        sources = [self.g.source(self.k,n) for n in names]
        if any(s is None or s[0]!='field' for s in sources): return self.unknown(atom,size)
        cons = []
        f = src[1]
        rawrefs, expr, shift = [], None, 0
        for tok,lo,hi,width in reversed(pieces(f)):
            field=self.g.field(tok,lo,hi,'_sem');rawrefs.append((tok,field))
            expr=field if expr is None else f'({field} << {shift}) | ({expr})';shift+=width
        for combo in product(*(range(1<<s[1].width) for s in sources)):
            env={n:v*s[2] for n,s,v in zip(names,sources,combo)}
            kind=pydecode.float_kind(self.g.arch,env,conv)
            clauses=[c for s,v in zip(sources,combo) for c in eq(self.g,s[1],v)]
            # F16/E8M7/E6M9 are packed 16-bit values, F64 supplies its high word.
            val=f'({expr}) << 32' if kind=='F64' else expr
            cons.append(('t',pattern(clauses,rawrefs),f' [ t = inst_start * 0 + ({val}); ]',f'export *[const]:{size} t;'))
        sym=self.g.subtable('v',cons);self.refs.append(('sub',sym))
        return Value(sym,size,'float-bits')

    def unknown(self, atom, size=4):
        name = atom.name if atom else 'unknown'
        index = list(self.k.operand_types).index(name) if name in self.k.operand_types else 0
        args = [f'{self.k.order}:4', f'{index}:4']
        fn = 'sass_operand_value'; self.g.pcodeops.add(fn)
        return self.expression(size, f'{fn}({", ".join(args)})', 'runtime-primitive')

    def source_size(self, name):
        # Latency ISRC sizes can describe a pointer register (CX uses UR pairs),
        # rather than the value fetched through it. Use the consuming data format.
        atom=self.k.operand_types.get('srcfmt')
        if atom:
            widths={int(m[1]) for label in self.g.arch.enums.get(atom.type,{})
                    if (m:=re.fullmatch(r'[FUS](8|16|32|64)',label))}
            if len(widths)==1:return max(4,next(iter(widths))//8)
        if self.k.mnemonic=='IMAD' and name=='Sc':
            width=self.g.sizes.get(self.k.name,{}).get('ISRC_C_SIZE',32)
            if isinstance(width,int):return max(4,width//8)
        if self.k.mnemonic=='MOV':
            rd=next((o for o in self.g.roles.get(self.k.name,{}).get('operands',[]) if o['name']=='Rd'),{})
            if isinstance(rd.get('span'),int):return 4*rd['span']
        from sass.operations import OPERATION_FAMILIES
        if self.k.mnemonic in OPERATION_FAMILIES:return 4
        match = re.match(r'S([abcde])$', name, re.I)
        expr = self.g.sizes.get(self.k.name, {}).get('ISRC_'+match[1].upper()+'_SIZE',32) if match else 32
        if isinstance(expr, int): return max(1,(expr+7)//8)
        # Non-register sources whose size depends on format are intentionally
        # runtime primitives until a fixed-size override selects their view.
        return 4

    def compound(self, atom):
        name = atom.name
        if atom.type=='C' and self.k.mnemonic=='LDC' and name+'_bank' in self.values and 'Ra' in self.values:
            # LDC's compound operand is an address, not an eagerly loaded scalar.
            bank=self.values[name+'_bank'].symbol;base=self.values['Ra'].symbol
            off=self.values['Ra_offset'].symbol if 'Ra_offset' in self.values else '0:4'
            pointer=self.expression(4,f'{base}:4 + {off}')
            return self.expression(8,f'(zext({bank}) << 32) | zext({pointer.symbol})','constant-address')
        if atom.type == 'C' and name+'_bank' in self.values and name+'_addr' in self.values:
            bank=self.values[name+'_bank'].symbol;off=self.values[name+'_addr'].symbol
            addr=self.expression(8,f'(zext({bank}) << 32) | zext({off})')
            return self.expression(self.source_size(name),f'*[cbank]:{self.source_size(name)} {addr.symbol}','constant-load')
        if atom.type == 'SpecialRegister':
            return self.unknown(atom)
        # Stateful descriptors, attribute memory, dynamic-bank pointers, etc.
        # Child operands remain explicit inputs to the callback in FORMAT order.
        index=list(self.k.operand_types).index(name)
        args=[f'{self.k.order}:4',f'{index}:4']
        start=self.k.format.index(atom)+1
        for child in self.k.format[start:]:
            if child.kind=='lit' and child.name==',':break
            if child.kind=='operand' and child.name in self.values:
                args.append(self.values[child.name].symbol)
        fn='sass_operand_value';self.g.pcodeops.add(fn)
        return self.expression(self.source_size(name),f'{fn}({", ".join(args)})','runtime-primitive')

    def cast(self, name, size, signed=False):
        v=self.values[name]
        if v.size==size:return v.symbol
        expr=f'{"sext" if signed else "zext"}({v.symbol})' if v.size<size else f'{v.symbol}:{size}'
        return self.local(size,expr).symbol

    def flag(self, name, float_value=False):
        v=self.values[name]; sym=v.symbol
        for attr in ('absolute','negate','invert','not'):
            bit=self.values.get(name+'@'+attr)
            if bit is None:continue
            if float_value:
                if attr=='absolute': expr=f'{sym} & {((1<<(v.size*8-1))-1):#x}'
                elif attr=='negate':expr=f'{sym} ^ {1<<(v.size*8-1):#x}'
                else:raise ValueError(f'unsupported floating attribute {attr}')
            else:
                expr={'negate':f'-{sym}','invert':f'~{sym}','not':f'!{sym}',
                      'absolute':f'abs({sym})'}.get(attr)
                if attr=='absolute':
                    code=f'local t:{v.size} = {sym}; if (t s>= 0) goto <positive>; t = -t; <positive> export t;'
                    sym=self.select_code((name+'@'+attr,),{(0,):f'local t:{v.size} = {sym}; export t;', (1,):code})
                    continue
            sym=self.select(name+'@'+attr,{0:sym,1:expr},v.size)
        return sym

    def select_code(self, names, cases):
        """Share small decode-time choices without multiplying root constructors."""
        from sass.gen_sleigh import pattern
        constructors=[]
        for values,code in cases.items():
            clauses=[];valid=True
            for name,value in zip(names,values):
                key=tuple(name.split('@',1)) if '@' in name else name
                constraint=self.g.values_where(self.k,key,lambda v:v==value)
                if constraint is None:
                    valid &= int(self.values[name].symbol.split(':')[0],0)==value
                else:clauses+=constraint
            if not valid or not pattern(clauses):continue
            symbols=set(re.findall(r'\b\w+\b',code))
            refs=[r for r in self.refs if r[1] in symbols]
            # A selected register view is an alias, not a value to snapshot.
            # The parent calculation captures inputs when overlap requires it.
            alias=re.fullmatch(r'local t:\d+ = (\w+); export t;',code)
            if alias and any(ref==alias[1] for _,ref in refs):code=f'export {alias[1]};'
            constructors.append(('""',pattern(clauses,refs),'',code))
        table=self.g.subtable('select',constructors)
        self.refs.append(('sub',table))
        return table

    def select(self, name, expressions, size):
        """Choose an expression at decode time, exporting its runtime value."""
        from sass.gen_sleigh import pattern
        if any(re.search(r'\b(?:sv\d+|SEMTMP\w+)\b',expr) for expr in expressions.values()):
            # Compound operands may already have evaluated a stateful load.
            # Keep those locals in this constructor instead of crossing scopes.
            result=self.local(size,next(iter(expressions.values())))
            end=f'selectdone{self.serial}'
            for value,expr in expressions.items():
                skip=f'selectskip{self.serial}';self.serial+=1
                self.body += [f'if ({self.values[name].symbol} != {value}) goto <{skip}>;',
                              f'{result.symbol} = {expr};',f'goto <{end}>;',f'<{skip}>']
            self.body.append(f'<{end}>')
            return result.symbol
        key=tuple(name.split('@',1)) if '@' in name else name
        constructors=[]
        for value,expr in expressions.items():
            clauses=self.g.values_where(self.k,key,lambda v:v==value)
            if clauses is None:
                current=self.values[name].symbol
                if int(current.split(':')[0],0)!=value:continue
                clauses=[]
            pat=pattern(clauses)
            if not pat:continue
            names=set(re.findall(r'\b\w+\b',expr))
            refs=[r for r in self.refs if r[1] in names]
            code=f'export {expr};' if re.fullmatch(r'\w+',expr) and any(ref==expr for _,ref in refs) else f'local t:{size} = {expr}; export t;'
            constructors.append(('""',pattern(clauses,refs),'',code))
        table=self.g.subtable('select',constructors)
        self.refs.append(('sub',table))
        return table

    def allow(self, name, test):
        """Restrict a native constructor using decoded instruction bits."""
        key = tuple(name.split('@',1)) if '@' in name else name
        clauses = self.g.values_where(self.k,key,test)
        if clauses is None:
            value = self.values.get(name)
            if value and re.fullmatch(r'(?:0x[0-9a-f]+|\d+):\d+',value.symbol):
                self.native_supported &= test(int(value.symbol.split(':')[0],0))
            else:
                raise ValueError(f'{self.k.name}: native selector {name} is not static')
        else:
            self.native_constraints += clauses

    def reject(self):
        self.native_supported = False

    def bank_guards(self):
        # A wide view containing the hardwired zero slot cannot be written as
        # ordinary backing storage. Keep such boundary encodings opaque until
        # their partial-register behavior has an independent implementation.
        from sass.gen_sleigh import ENUM_FILE,REG_FILES
        conditions=[]
        for name,value in {**self.values,**self.outputs}.items():
            atom=self.k.operand_types.get(name)
            file=ENUM_FILE.get(atom.type) if atom else None
            if value.kind!='register' or file not in ('R','UR'):continue
            element=REG_FILES[file][1];span=value.size//element
            if span<=1:continue
            index=self.scalar(name,atom);zero=REG_FILES[file][3]
            conditions.append(f'{index.symbol} != {zero} && {index.symbol} >= {max(0,zero-span+1)}')
        return conditions

    def opaque_input(self, name, value):
        atom=self.k.operand_types.get(name)
        return value.kind=='register' or bool(atom and atom.kind=='operand' and pydecode.IMM.match(atom.type)
                                              and value.kind not in ('default','runtime-primitive'))

    def call(self, prefix='opaque'):
        stem='sass_'+prefix+'_'+re.sub(r'\W','_',self.k.mnemonic)
        args=[] if prefix=='opaque' else [f'{self.k.order}:4']
        args += [v.symbol for n,v in self.values.items() if prefix!='opaque' or self.opaque_input(n,v)]
        self.g.pcodeops.add(stem)
        if not self.outputs:
            return [f'{stem}({", ".join(args)});']
        # One result bundle avoids duplicating the complete input ABI for each
        # destination and evaluates stateful primitives exactly once.
        if len(self.outputs)==1:
            output=next(iter(self.outputs.values()))
            return [f'{output.symbol} = {stem}({", ".join(args)});']
        total=sum(v.size for v in self.outputs.values())
        start=len(self.body)
        result=self.local(total,f'{stem}({", ".join(args)})')
        lines=self.body[start:];del self.body[start:]
        offset=0
        for name,v in self.outputs.items():
            expr=result.symbol if v.size==total else f'{result.symbol}({offset})'
            lines.append(f'{v.symbol} = {expr};');offset+=v.size
        return lines

    def finish(self, status, reason=''):
        row=self.g.coverage.setdefault(self.k.name,dict(cls=self.k.name,opcode=self.k.mnemonic,
              status=status,reason=reason,operands=self.inventory,
              inputs=list(self.values),outputs=list(self.outputs),hardware_verified=False,
              implicit=self.g.roles.get(self.k.name,{}).get('implicit',[])))
        row['status']=status
        row['selector_guards']=self.selector_guards
        row['raw_bits_preserved']=True
        row['flow']={n:v for n,v in self.g.props.get(self.k.name,{}).items() if n.startswith('BRANCH_')}
        variant=dict(spans=self.spans,inputs=list(self.values),outputs={n:v.size for n,v in self.outputs.items()})
        if variant not in row.setdefault('variants',[]):row['variants'].append(variant)
        body=' '.join(prune_unused_values(self.body))
        names=set(re.findall(r'\b\w+\b',body))
        return body,[r for r in self.refs if r[1] in names]


def emit(gen,klass,guard,spans):
    from sass import operations
    b=Builder(gen,klass,guard,spans)
    status,reason=operations.emit(b)
    if status=='opaque':b.body += b.call()
    return b.finish(status,reason)
