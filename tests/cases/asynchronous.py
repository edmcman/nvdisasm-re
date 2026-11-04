from tests.harness import asynchronous


def emulator_cases(c):
    return c


def gpu_fixtures(arch="SM89"):
    return asynchronous.fixtures(arch)
