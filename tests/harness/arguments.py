import struct
from tests.harness import gpu


def fixture(arch="SM89"):
    arguments = ((1, 0xff), (2, 0x8001), (3, b"\x12\0\xab"), (4, 0xffffffff),
                 (8, 0x8000000000000001), (12, bytes(range(12))), (16, bytes(range(15, -1, -1))))
    payload = b"\xff\x01\x80\x12\0\xab" + b"\xff" * 4 + b"\x01" + b"\0" * 6 + b"\x80"
    payload += bytes(range(12)) + bytes(range(15, -1, -1))
    total = sum((i + 1) * value for i, value in enumerate(payload))
    return gpu.Fixture("kernel_arguments", gpu.compile("arguments", arch=arch), "arguments",
                       struct.pack("<96I", *range(96)), [3 * i + total for i in range(32)],
                       arguments=arguments, exit=True)
