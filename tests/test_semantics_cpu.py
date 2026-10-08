"""Independent CPU arithmetic checks; no Ghidra or GPU dependency."""
import ctypes,math,random,struct,unittest
from fractions import Fraction
from sass import softfloat


def bits(x):return int.from_bytes(struct.pack('<f',x),'little')
def floating(x):return struct.unpack('<f',x.to_bytes(4,'little'))[0]

class SoftFloatTests(unittest.TestCase):
    def test_directed_ties(self):
        one=Fraction(1);half=Fraction(1,1<<24)
        self.assertEqual(softfloat.pack(one+half),0x3f800000)
        self.assertEqual(softfloat.pack(one+half,mode='RP'),0x3f800001)
        self.assertEqual(softfloat.pack(-one-half,mode='RM'),0xbf800001)
        self.assertEqual(softfloat.pack(-one-half,mode='RZ'),0xbf800000)
    def test_specials(self):
        self.assertEqual(softfloat.arithmetic('add',[0x80000000,0x80000000]),0x80000000)
        self.assertEqual(softfloat.arithmetic('add',[0x7f800000,0xff800000]),0x7fc00000)
        self.assertEqual(softfloat.arithmetic('mul',[0x7f800000,0]),0x7fc00000)
        self.assertEqual(softfloat.arithmetic('add',[1,0],ftz=True),0)
        self.assertEqual(softfloat.arithmetic('add',[0x7fc00000,0],sat=True),0)
        self.assertEqual(softfloat.pack(Fraction(1,1<<149)),1)
    def test_host_arithmetic(self):
        rng=random.Random(42)
        lib=ctypes.CDLL('libm.so.6');lib.fmaf.argtypes=[ctypes.c_float]*3;lib.fmaf.restype=ctypes.c_float
        for _ in range(1000):
            xs=[rng.getrandbits(32) for _ in range(3)];a,b,c=map(floating,xs)
            if not all(map(math.isfinite,(a,b,c))):continue
            actual=softfloat.arithmetic('fma',xs)
            expected=bits(lib.fmaf(a,b,c))
            self.assertEqual(actual,expected,tuple(hex(x) for x in xs))
            for op,reference in [('add',a+b),('mul',a*b)]:
                try:expected=bits(reference)
                except OverflowError:expected=0xff800000 if reference<0 else 0x7f800000
                self.assertEqual(softfloat.arithmetic(op,xs[:2]),expected)
    def test_conversions(self):
        for rnd,expected in [('RN',2),('RM',1),('RP',2),('RZ',1)]:
            self.assertEqual(softfloat.float_to_int(bits(1.5),32,32,True,rnd),expected)
        with self.assertRaises(NotImplementedError):softfloat.float_to_int(0x7fc00000,32,32,True)
        self.assertEqual(softfloat.pack(Fraction(65504),16),0x7bff)
        self.assertEqual(softfloat.unpack(0x3ff0000000000000,64)[2],1)

if __name__=='__main__':unittest.main()
