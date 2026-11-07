def copied(value, width, enabled):
    return ((value if enabled else 0) & ((1 << width) - 1)).to_bytes(width // 8, "little")


def descriptor_error(policy):
    return "zero packed async descriptor" if policy == 0 else None
