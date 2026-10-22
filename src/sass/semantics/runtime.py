"""CPU implementations of named instruction primitives (src/sass/semantics/primitive.py) and the
other pcodeops the languages call.

Unknown operations fail explicitly. This is single-thread/explicit-warp context,
not an implementation of CUDA's warp scheduler or memory concurrency model.
"""
import functools, operator
from sass.decode import ir
from sass.semantics.bitvec import PRIMITIVE_OP
from sass.semantics.fp import mufu


class UnsupportedSemantics(NotImplementedError): pass


REDUCTIONS = {'and':operator.and_,'or':operator.or_,'xor':operator.xor,'add':operator.add,'umin':min,'umax':max,'smin':min,'smax':max}
SPACES = {'global':1,'shared':2,'local':4,'shared_cluster':8}
SYNCHRONIZATION = ('bar','bssy','bsync','break','warpsync','bmov')


class Runtime:
    def __init__(self, sm, context=None, handlers=None):
        self.sm=sm;self.handlers=handlers or {};self.context=context or {};self.events=[]

    def call(self,name,args,size):
        """The pcodeop `name` on integer args, a size-byte result (0: an effect)."""
        if name in self.handlers:return self.handlers[name](args,size,self.context)
        if name.startswith(PRIMITIVE_OP):return self.primitive(name[len(PRIMITIVE_OP):],args,size)
        if name.startswith('sass_opaque_'):
            raise UnsupportedSemantics(f'{name}{tuple(args)}')
        arch=ir.load('SM'+str(self.sm))
        if name=='sass_operand_value':
            ordinal,index,*children=args;k=arch.classes[ordinal]
            atom=list(k.operand_types.values())[index]
            # Stateful operands are provided by the execution environment rather
            # than given a made-up bank/descriptor layout.
            supplied=self.context.get('operands',{})
            key=k.name+'.'+atom.name
            if key not in supplied:raise UnsupportedSemantics('operand '+key)
            return int(supplied[key])
        if name=='sass_memory_order':
            ordinal,*controls=args;k=arch.classes[ordinal]
            for key,value in zip((n for n in ('sem','sco','cop','cop2','private') if n in k.operand_types),controls):
                atom=k.operand_types[key];label=arch.rev_enums[atom.type].get(value,'INVALID')
                if label.startswith('INVALID'):raise UnsupportedSemantics('memory control '+label)
            self.events.append(dict(op=k.mnemonic,controls=controls));return 0
        if name=='sass_memory_descriptor':
            # Bit 59 marks a range-form L2 policy; some of those trap on SM89 and which ones is unknown.
            descriptor,=args
            if descriptor>>59&1:raise UnsupportedSemantics(f'range-form memory descriptor {descriptor:#x}')
            return 0
        raise UnsupportedSemantics(name)

    def primitive(self,name,args,size):
        family,_,variant=name.partition('_')
        match name,args:
            case 'mufu_sin'|'mufu_cos',[x]:return mufu.sincos(x,variant.upper())
            case _ if family=='mufu':
                raise UnsupportedSemantics(f'MUFU approximation is unresolved for {variant.upper()}'+(' (16-bit)' if len(args)==2 else ''))
            case 'lane',[]:return self.warp()[1]
            case 'active',[]:return self.warp()[2]
            case 'waiting',[]:
                # The lanes of this warp still alive but not executing this instruction (BRA.DIV/.CONV).
                waiting=self.context.get('warp',{}).get('waiting')
                if waiting is None:raise UnsupportedSemantics('warp waiting-lane mask required')
                return int(waiting)&0xffffffff
            case 'read_lane',[x,src]:return self.lanes('values',x)[0][src]
            case 'ballot',[p]:
                preds,lanes=self.lanes('predicates',p)
                return sum(1<<i for i in lanes if preds[i])
            case 'match',[x]:
                values,lanes=self.lanes('values',x)
                return sum(1<<i for i in lanes if values[i]==x)
            case _,[x] if family=='redux' and variant in REDUCTIONS:
                values,lanes=self.lanes('values',x)
                xs=[values[i]&0xffffffff for i in lanes]
                if variant[0]=='s':xs=[v-(1<<32) if v>>31 else v for v in xs]
                return functools.reduce(REDUCTIONS[variant],xs)&0xffffffff
            case 'address_space',[address]:return self.address_space(address)
            case _,[*inputs] if name in SYNCHRONIZATION:return self.synchronize(name,inputs,read=size>0)
        raise UnsupportedSemantics('primitive '+name)

    def warp(self):
        """(warp context, this lane, executing lanes mask)."""
        warp=self.context.get('warp')
        if not warp:raise UnsupportedSemantics('explicit 32-lane warp context required')
        lane=int(warp.get('lane',0));active=int(warp.get('active',0xffffffff))
        if not 0<=lane<32 or not active>>lane&1:raise UnsupportedSemantics('warp lane is inactive or invalid')
        return warp,lane,active

    def lanes(self,key,x):
        """(every lane's contribution from the warp context's `key`, this lane's being x; the executing lanes)."""
        warp,lane,active=self.warp()
        values=warp.get(key)
        if values is None or len(values)!=32:raise UnsupportedSemantics(f'warp {key} required')
        values=[int(v) for v in values];values[lane]=x
        return values,[i for i in range(32) if active>>i&1]

    def address_space(self,address):
        """The one-hot tag of the one mapped space containing a generic address."""
        ranges=self.context.get('address_spaces')
        if not isinstance(ranges,dict):raise UnsupportedSemantics('QSPC address_spaces context required')
        found=0
        for name,intervals in ranges.items():
            if name not in SPACES or not isinstance(intervals,(list,tuple)):
                raise UnsupportedSemantics(f'malformed QSPC address ranges for {name}')
            for interval in intervals:
                if not isinstance(interval,(list,tuple)) or len(interval)!=2:
                    raise UnsupportedSemantics(f'malformed QSPC address range for {name}')
                start,end=map(int,interval)
                if not 0<=start<=end<=1<<64:raise UnsupportedSemantics(f'invalid QSPC address range for {name}')
                if start<=address<end:found|=SPACES[name]
        if found.bit_count()!=1:raise UnsupportedSemantics('QSPC input address has no unique mapped space')
        return found

    def synchronize(self,op,inputs,read):
        """Synchronization is supplied by the environment: a missing handler fails rather than
        silently treating reconvergence/barriers as no-ops. An event's first input enables it; a
        read (BMOV to a register) returns the handler's value for its state encoding, its first input."""
        if not read and not inputs[0]:return 0
        handler=self.context.get('synchronization',{}).get(op)
        if not handler:raise UnsupportedSemantics(op.upper()+' synchronization context required')
        self.events.append(dict(op=op.upper(),inputs=inputs))
        if not read:return 0
        # BMOV reads convergence-barrier state, which only the environment holds.
        values={str(k):v for k,v in handler.items()} if isinstance(handler,dict) else {}
        if str(inputs[0]) not in values:raise UnsupportedSemantics(op.upper()+' reduction/result requires an environment handler')
        return int(values[str(inputs[0])])
