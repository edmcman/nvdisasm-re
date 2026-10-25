"""One disposable CUDA context, invoked by device_cases in a bounded subprocess."""
import hashlib
import pickle
import sys
import traceback
from tests.harness import device_cases


def main():
    probes = pickle.loads(sys.stdin.buffer.read())
    driver = None
    try:
        driver = device_cases.Driver(probes[0].code.arch)
        binary = device_cases.image(probes)
        result = dict(results=device_cases.execute(driver, probes, binary=binary), device=driver.device,
                      kernels=[hashlib.sha256(binary).hexdigest()[:24]])
    except Exception:
        result = dict(error=traceback.format_exc())
    finally:
        if driver:
            try: driver.close()
            except RuntimeError: pass  # a device fault invalidates cleanup as well
    sys.stdout.buffer.write(pickle.dumps(result))


if __name__ == '__main__': main()
