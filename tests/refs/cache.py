OPERATIONS = ("PF1", "PF2", "PML2", "DML2")


def invalidate(): return dict(op="CCTL", operation="IVALL")


def released_payload(threads, value): return [value * (2 ** 32)] * threads


def event(operation, base, offset, address_bits):
    address = ((base % (2 ** address_bits)) + offset) % (2 ** 64)
    if operation not in OPERATIONS: raise ValueError(operation)
    if operation == "DML2" and address % 128: raise ValueError("cache priority address must be 128-byte aligned")
    return dict(op="CCTL", operation=operation, address=address)
