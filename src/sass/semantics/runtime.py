"""CPU implementations of named instruction primitives.

Unknown operations fail explicitly. This is single-thread/explicit-warp context,
not an implementation of CUDA's warp scheduler or memory concurrency model.
"""
import functools, json, math, operator, re
from fractions import Fraction
from pathlib import Path
from sass.decode import ir
from sass.semantics.fp import softfloat


class UnsupportedSemantics(NotImplementedError): pass


MEMORY_CONTROLS=('sem','sco','cop','cop2','private')   # the encodings memory-ordering calls pass, in this order


class Runtime:
    def __init__(self, language_dir, sm, context=None, handlers=None, variant=None):
        self.sm=sm;self.handlers=handlers or {};self.variant=variant or f'sm{sm}'
        self.language_dir=Path(language_dir);self.context=context or {};self.events=[];self.manifests={}

    def manifest(self,sm):
        if sm not in self.manifests:
            path=self.language_dir/f'sass_{self.variant}_runtime.json'
            if not path.exists():path=self.language_dir/f'sass_{self.variant}_coverage.json'
            rows=json.loads(path.read_text())
            self.manifests[sm]={r['cls']:r for r in rows}
        return self.manifests[sm]

    def open_atomic(self):
        return next((e for e in self.events if e.get('atomic')=='open'),None)

    def call(self,name,args,size):
        if name in self.handlers:return self.handlers[name](args,size,self.context)
        sm=self.sm
        if name.startswith('sass_opaque_'):
            raise UnsupportedSemantics(f'{name}{tuple(args)}')
        if name=='sass_operand_value':
            ordinal,index,*children=args;k=ir.load('SM'+str(sm)).classes[ordinal]
            atom=list(k.operand_types.values())[index]
            # Stateful operands are provided by the execution environment rather
            # than given a made-up bank/descriptor layout.
            supplied=self.context.get('operands',{})
            key=k.name+'.'+atom.name
            if key not in supplied:raise UnsupportedSemantics('operand '+key)
            return int(supplied[key])
        if name in ('sass_memory_order','sass_atomic_begin'):
            ordinal,*controls=args;k=ir.load('SM'+str(sm)).classes[ordinal]
            for key,value in zip((n for n in MEMORY_CONTROLS if n in k.operand_types),controls):
                atom=k.operand_types[key];label=ir.load('SM'+str(sm)).rev_enums[atom.type].get(value,'INVALID')
                if label.startswith('INVALID'):raise UnsupportedSemantics('memory control '+label)
            event=dict(op=k.mnemonic,controls=controls)
            if name=='sass_atomic_begin':
                if self.open_atomic():raise UnsupportedSemantics('nested atomic region')
                event['atomic']='open'
            self.events.append(event);return 0
        if name=='sass_atomic_end':
            event=self.open_atomic()
            if event is None:raise UnsupportedSemantics('atomic region end without a begin')
            event['atomic']='closed';return 0
        if name=='sass_memory_descriptor':
            # Bit 59 marks a range-form L2 policy; some of those trap on SM89 and which ones is unknown.
            descriptor,=args
            if descriptor>>59&1:raise UnsupportedSemantics(f'range-form memory descriptor {descriptor:#x}')
            return 0
        if not name.startswith('sass_prim_'):raise UnsupportedSemantics(name)
        ordinal,*values=args;arch=ir.load('SM'+str(sm));k=arch.classes[ordinal]
        variants=self.manifest(sm)[k.name]['variants']
        if len(variants)!=1:
            # Selectors that change only register spans (QSPC's E, MATCH's U32/U64)
            # keep one input/output signature: p-code has already assembled the
            # selected span into the integer argument.
            signatures={(tuple(x['inputs']),tuple(x['outputs'].items())) for x in variants}
            if len(signatures)==1:
                inputs,outputs=variants[0]['inputs'],variants[0]['outputs']
            else:raise UnsupportedSemantics('primitive with modifier-dependent register spans '+k.name)
        else:inputs,outputs=variants[0]['inputs'],variants[0]['outputs']
        if len(values)!=len(inputs):raise UnsupportedSemantics('primitive input ABI mismatch '+k.name)
        v=dict(zip(inputs,values))
        # Multi-destination operations return a low-to-high byte bundle. All
        # inputs are evaluated before that bundle is assigned to registers.
        suffix=name[len('sass_prim_'+k.mnemonic):].lstrip('_')
        if suffix:return self.execute(arch,k,v,suffix,size)
        offset=0;result=0
        for dest,width in outputs.items():
            result|=(self.execute(arch,k,v,dest,width)&((1<<(width*8))-1))<<offset
            offset+=width*8
        if not outputs:self.execute(arch,k,v,'',0)
        return result

    def execute(self,arch,k,v,dest,size):
        op=k.mnemonic
        def label(n,default=None):
            atom=k.operand_types.get(n)
            if atom is None:return default
            out=arch.rev_enums.get(atom.type,{}).get(v[n]) if n in v else default
            if out is None or out.startswith('INVALID'):raise UnsupportedSemantics(f'{op} {n}={out}')
            return out
        def get(*names):
            for n in names:
                if n in v:return n,v[n]
            raise UnsupportedSemantics(op+' missing operand '+str(names))
        def integer(*names):
            n,x=get(*names)
            if v.get(n+'@invert'):x=~x
            if v.get(n+'@negate'):x=-x
            if v.get(n+'@not'):x=int(not x)
            return x&0xffffffff
        def floating(width,*names):
            n,x=get(*names);mask=(1<<width)-1
            if v.get(n+'@absolute'):x&=(1<<(width-1))-1
            if v.get(n+'@negate'):x^=1<<(width-1)
            return x&mask
        if op in ('FADD','FMUL','FFMA'):
            rnd=label('rnd','RN');fmz=label('fmz','nofmz');ftz=label('ftz','noftz')
            if fmz=='nofmz_hfma2':fmz='nofmz'
            if fmz not in ('nofmz','FTZ'):raise UnsupportedSemantics(op+' '+fmz)
            flush=fmz=='FTZ' or ftz=='FTZ';sat=label('sat','nosat')=='SAT'
            a=floating(32,'Ra','Sa');b=floating(32,*(('Rc','Sc','URc') if op=='FADD' else ('Rb','Sb','URb')))
            inputs=[a,b]
            if op=='FFMA':inputs.append(floating(32,'Rc','Sc','URc'))
            scale=label('scale','noscale');exponent={'noscale':0,'D2':-1,'D4':-2,'D8':-3,'M2':1,'M4':2,'M8':3}.get(scale)
            if exponent is None:raise UnsupportedSemantics(op+' scale '+scale)
            return softfloat.arithmetic({'FADD':'add','FMUL':'mul','FFMA':'fma'}[op],inputs,32,rnd,flush,sat,exponent)
        if op=='FSETP':
            flush=label('ftz','noftz')=='FTZ';a=softfloat.unpack(floating(32,'Ra','Sa'),32,flush);b=softfloat.unpack(floating(32,'Rb','Sb','URb'),32,flush)
            cmp=label('fcomp');unordered=a[0]=='nan' or b[0]=='nan'
            def numeric(x):return (-math.inf if x[1] else math.inf) if x[0]=='inf' else x[2]
            comparisons={'LT':lambda x,y:x<y,'EQ':lambda x,y:x==y,'LE':lambda x,y:x<=y,'GT':lambda x,y:x>y,'NE':lambda x,y:x!=y,'GE':lambda x,y:x>=y}
            if cmp=='NUM':result=not unordered
            elif cmp=='NAN':result=unordered
            elif cmp in ('F','T'):result=cmp=='T'
            else:
                base=cmp.removesuffix('U')
                if base not in comparisons:raise UnsupportedSemantics('FCMP '+cmp)
                result=(unordered and cmp.endswith('U')) or (not unordered and comparisons[base](numeric(a),numeric(b)))
            if dest=='Pv':result=not result
            pred=bool(integer('Pp'));bop=label('bop','AND')
            return int({'AND':lambda:result and pred,'OR':lambda:result or pred,'XOR':lambda:result!=pred}[bop]())
        if op=='QSPC':
            query=label('space')
            space={'G':'global','L':'local','S':'shared',
                   'D':'shared_cluster'}.get(query)
            if space is None:raise UnsupportedSemantics(f'QSPC query space {query}')
            ranges=self.context.get('address_spaces')
            if not isinstance(ranges,dict):
                raise UnsupportedSemantics(f'QSPC address_spaces.{space} context required')
            offset=v.get('Ra_offset',0)&0xffffffff
            if offset&0x80000000:offset-=1<<32
            address=(v.get('Ra',0)+v.get('Ra_URb',0)+offset)&((1<<64)-1)
            def in_space(name):
                if name not in ranges:
                    raise UnsupportedSemantics(f'QSPC address_spaces.{name} context required')
                intervals=ranges[name]
                if not isinstance(intervals,(list,tuple)):
                    raise UnsupportedSemantics(f'malformed QSPC address ranges for {name}')
                matched=False
                for interval in intervals:
                    if not isinstance(interval,(list,tuple)) or len(interval)!=2:
                        raise UnsupportedSemantics(f'malformed QSPC address range for {name}')
                    start,end=map(int,interval)
                    if not 0<=start<=end<=1<<64:
                        raise UnsupportedSemantics(f'invalid QSPC address range for {name}')
                    matched |= start<=address<end
                return matched
            if dest=='Pu':return int(in_space(space))
            if dest=='Rd':
                tags={'global':1,'shared':2,'local':4,'shared_cluster':8}
                matches=[tag for name,tag in tags.items()
                         if name in ranges and in_space(name)]
                if len(matches)!=1:
                    raise UnsupportedSemantics('QSPC input address has no unique mapped space')
                return matches[0]
            raise UnsupportedSemantics(f'QSPC output {dest}')
        if op in ('F2I','I2F','I2FP'):
            src=label('srcfmt');dst=label('dstfmt');n,bits=get('Sb','Rb','Ra','Sa')
            if not src or not dst:raise UnsupportedSemantics(op+' format')
            match=lambda s:re.fullmatch(r'([FUS])(8|16|32|64)',s)
            sf,df=match(src),match(dst)
            if not sf or not df:raise UnsupportedSemantics(op+' format '+src+'/'+dst)
            sw,dw=int(sf[2]),int(df[2]);shift=0
            if sw==16:shift=16 if label('hsel','H0')=='H1' else 0
            if sw==8:shift=int(label('b3b0','B0')[1:])*8
            bits=bits>>shift&((1<<sw)-1)
            rnd=label('rnd','RN');rnd={'ROUND':'RN','FLOOR':'RM','CEIL':'RP','TRUNC':'RZ'}.get(rnd,rnd)
            if op=='F2I':
                ntz=label('ntz','nontz')
                if v.get(n+'@absolute'):bits&=(1<<(sw-1))-1
                if v.get(n+'@negate'):bits^=1<<(sw-1)
                ftz=label('ftz','noftz')=='FTZ'
                if ntz=='NTZ':
                    # Measured SM89 F2I.TRUNC.NTZ: NaNs become zero, infinities
                    # and finite overflow clamp to the signed destination range.
                    if (arch.name,sw,dw,df[1],rnd)!=('SM89',32,32,'S','RZ'):
                        raise UnsupportedSemantics('unverified NTZ conversion')
                    kind,sign,value=softfloat.unpack(bits,sw,ftz)
                    lo,hi=-(1<<31),(1<<31)-1
                    result=0 if kind=='nan' else (lo if sign else hi) if kind=='inf' else max(lo,min(hi,softfloat.round_integer(value,rnd)))
                elif ntz in ('nontz','NOTZ','noNTZ'):
                    result=softfloat.float_to_int(bits,sw,dw,df[1]=='S',rnd,ftz)
                else:raise UnsupportedSemantics('NTZ conversion '+ntz)
                if df[1]=='S' and dw<size*8 and result>>(dw-1):result-=1<<dw
            else:
                if sf[1]=='S' and bits>>(sw-1):bits-=1<<sw
                if v.get(n+'@negate'):bits=-bits
                if v.get(n+'@absolute'):bits=abs(bits)
                result=softfloat.pack(Fraction(bits),dw,rnd)
            return result&((1<<(size*8))-1)
        if op=='PRMT':
            mode=label('pmode','IDX')
            a=integer('Ra');selector=integer('Rb','Sb');b=integer('Rc','Sc');data=a|(b<<32);out=0
            for i in range(4):
                pick=selector>>(i*4)&15 if mode=='IDX' else {'F4E':lambda:(selector&3)+i, 'B4E':lambda:((selector&3)-i)&7, 'RC8':lambda:selector&3, 'RC16':lambda:((selector&1)*2)+(i&1), 'ECL':lambda:max(i,selector&3), 'ECR':lambda:min(i,selector&3)}[mode]()
                byte=data>>((pick&7)*8)&255
                if mode=='IDX' and pick&8:byte=255 if byte&128 else 0
                out|=byte<<(i*8)
            return out
        if op in ('SHFL','VOTE','VOTEU','MATCH','REDUX','ELECT'):
            warp=self.context.get('warp')
            if not warp:raise UnsupportedSemantics('explicit 32-lane warp context required')
            lane=int(warp.get('lane',0));active=int(warp.get('active',0xffffffff))
            if not 0<=lane<32 or not active>>lane&1:raise UnsupportedSemantics('warp lane is inactive or invalid')
            lanes=[i for i in range(32) if active>>i&1]
            if op=='ELECT':
                # PTX elect.sync: the lowest active lane of the member mask leads; d is its lane id.
                mask=integer('URa') if 'URa' in v else 0xffffffff
                members=[i for i in lanes if mask>>i&1]
                if not members:raise UnsupportedSemantics('ELECT without an active member lane')
                return int(lane==members[0]) if dest=='Pu' else members[0]
            if op in ('MATCH','REDUX'):
                values=warp.get('values',[])
                if len(values)!=32:raise UnsupportedSemantics('warp values required')
            if op=='MATCH':
                # PTX match.any/all.sync over the active lanes (Ra's span already gives U32/U64).
                equal=sum(1<<i for i in lanes if values[i]==values[lane])
                if label('op')=='ANY':return equal
                return int(equal==active) if dest=='Pu' else (active if equal==active else 0)
            if op=='REDUX':
                # PTX redux.sync over the active lanes; MIN/MAX compare signed with .S32.
                xs=[values[i]&0xffffffff for i in lanes];mode=label('op')
                if label('sz','U32')=='S32' and mode in ('MIN','MAX'):xs=[x-(1<<32) if x>>31 else x for x in xs]
                combine={'SUM':operator.add,'AND':operator.and_,'OR':operator.or_,'XOR':operator.xor,'MIN':min,'MAX':max}[mode]
                return functools.reduce(combine,xs)&0xffffffff
            if op in ('VOTE','VOTEU'):
                preds=warp.get('predicates')
                if preds is None or len(preds)!=32:raise UnsupportedSemantics('warp predicates required')
                if v.get('Pp@not'):preds=[not x for x in preds]
                mask=sum((1<<i) for i in range(32) if active>>i&1 and preds[i])
                mode=label('voteop');result={'ALL':mask==active,'ANY':bool(mask),'EQ':mask==0 or mask==active}[mode]
                return mask if dest in ('Rd','URd') else int(result)
            if len(warp.get('values',[]))!=32:raise UnsupportedSemantics('warp values required')
            # PTX shfl.sync's rule; SM89 matches it exactly (524288 lane results, random b and c).
            # UP's bound is a lower one (its clamp is the segment start); the others only check j <= bound.
            b=integer('Rb','Sb')&31;c=integer('Rc','Sc');segment=c>>8&31
            bound=(lane&segment)|(c&31&~segment);mode=label('shflmd')
            target={'IDX':(lane&segment)|(b&~segment),'UP':lane-b,'DOWN':lane+b,'BFLY':lane^b}[mode]
            valid=target>=bound if mode=='UP' else target<=bound
            if valid and not active>>target&1:raise UnsupportedSemantics('inactive shuffle source has undefined value')
            return int(valid) if dest=='Pu' else int(warp['values'][target if valid else lane])
        if op in ('BAR','BSSY','BSYNC','BMOV','BREAK','WARPSYNC'):
            # Synchronization is supplied by the environment. A missing handler
            # fails rather than silently treating reconvergence/barriers as no-ops.
            if 'Pp' in v and not integer('Pp'):return 0
            key=op.lower();handler=self.context.get('synchronization',{}).get(key)
            if not handler:raise UnsupportedSemantics(op+' synchronization context required')
            self.events.append(dict(op=op,values=v))
            if not dest:return 0
            # BMOV reads convergence-barrier state, which only the environment holds.
            state=label('cbu_state')
            if op!='BMOV' or not isinstance(handler,dict) or state not in handler:
                raise UnsupportedSemantics(op+' reduction/result requires an environment handler')
            return int(handler[state])
        if op=='MUFU':raise UnsupportedSemantics('MUFU approximation requires a hardware-specific implementation')
        raise UnsupportedSemantics('primitive '+op)
