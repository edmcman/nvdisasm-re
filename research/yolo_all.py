import argparse, ctypes, hashlib, json, math, time
from collections import Counter
from pathlib import Path


def capture(args):
    import torch
    from functools import partial
    from ultralytics import YOLO

    torch.manual_seed(114)
    torch.set_num_threads(1)
    torch.load = partial(torch.load, weights_only=False)
    recorder = ctypes.CDLL(str(args.recorder.resolve()))
    recorder.start_capture.argtypes = [ctypes.c_char_p]
    args.directory.mkdir(parents=True, exist_ok=True)
    if recorder.start_capture(str(args.directory.resolve()).encode()): raise RuntimeError("CUPTI recorder initialization failed")
    try:
        model = YOLO(str(args.weights)).model.cuda().eval()
        x = torch.rand(1, 3, args.size, args.size, device='cuda')
        torch.cuda.synchronize()
        # Keep setup launches in the trace and mark the exact inference interval.
        trace = args.directory / 'launches.jsonl'
        first = len(trace.read_text().splitlines())
        with torch.inference_mode(): output = model(x)
        torch.cuda.synchronize()
        last = len(trace.read_text().splitlines())
        prediction = output[0] if isinstance(output, tuple) else output
        (args.directory / 'prediction.bin').write_bytes(prediction.contiguous().cpu().numpy().tobytes())
        metadata = dict(torch=torch.__version__, device=torch.cuda.get_device_name(), input_shape=list(x.shape),
                        prediction_shape=list(prediction.shape), first_inference_event=first, last_inference_event=last,
                        weights_sha256=hashlib.sha256(args.weights.read_bytes()).hexdigest())
        (args.directory / 'inference.json').write_text(json.dumps(metadata, indent=2) + '\n')
        print(json.dumps(metadata, indent=2), flush=True)
    finally: recorder.stop_capture()


def replay(args):
    from sass import launch
    from sass.decode import cubin, pydecode
    from sass.emulate import kernel
    from sass.semantics import lifter

    root = args.directory
    metadata = json.loads((root / 'inference.json').read_text())
    events = [json.loads(line) for line in (root / 'launches.jsonl').read_text().splitlines()]
    events = events[metadata['first_inference_event']:metadata['last_inference_event']]
    wanted = {e['name'] for e in events if 'name' in e}
    modules = {}
    for path in root.glob('module-*.cubin'):
        data = path.read_bytes()
        if not cubin.is_elf(data): continue
        for section in cubin.text_sections(data):
            name = section.removeprefix('.text.')
            if name in wanted: modules[name] = path
    reports, images = [], {}
    for event in events:
        row = dict(id=event.get('id'), name=event.get('name'), status='blocked')
        try:
            if 'unsupported_api' in event: raise ValueError('unrecorded launch API: ' + event['unsupported_api'])
            if event['error']: raise ValueError('capture: ' + event['error'])
            name = event['name']
            if name not in modules: raise ValueError('no captured cubin for symbol')
            if name not in images:
                image = cubin.kernel_image(modules[name].read_bytes(), name)
                counts, gaps = Counter(), Counter()
                for offset in range(0, len(image['code']), 16):
                    word = image['code'][offset:offset + 16]
                    try:
                        d = pydecode.decode(image['arch'], word)
                        counts[d.klass.mnemonic] += 1
                        semantics = lifter.lift(image['arch'], word)
                        if semantics.status == 'opaque': gaps[d.klass.mnemonic + ': ' + semantics.reason] += 1
                    except pydecode.NoMatch: counts['undecodable'] += 1
                images[name] = image, dict(counts), dict(gaps)
            image, counts, gaps = images[name]
            row.update(instructions=counts, static_gaps=gaps, code_bytes=len(image['code']))
            if len(image['code']) > args.max_code_bytes: raise ValueError('code exceeds replay compilation budget')
            threads = math.prod(event['grid']) * math.prod(event['block'])
            row['threads'] = threads
            if threads > args.max_threads: raise ValueError('launch exceeds replay thread budget')
            memory, targets = [], []
            for region in event['regions']:
                address = region['address']
                before = (root / f"{event['id']}-before-{address}.bin").read_bytes()
                after = (root / f"{event['id']}-after-{address}.bin").read_bytes()
                if len(before) != region['size'] or len(after) != region['size']: raise ValueError('incomplete memory snapshot')
                memory.append(dict(space='ram', address=address, hex=before.hex()))
                targets.append((address, after))
            if not targets: raise ValueError('no captured device memory to compare')
            values = [bytes.fromhex(a) for a in event['arguments']]
            if len(values) != len(image['parameters']): raise ValueError('parameter count differs from cubin metadata')
            row['comparisons'] = []
            for address, expected in targets:
                stride = math.ceil(len(expected) / threads)
                request = launch.kernel_request(image, values, memory, grid=event['grid'], block=event['block'],
                                                output=dict(address=address, size=stride, stride=stride),
                                                context=dict(synchronization=dict(bar=True, warpsync=True)))
                request['kernel']['max_steps'] = args.max_steps
                start = time.monotonic()
                result = kernel.run_kernel(request, image['arch'])
                if 'error' in result: raise ValueError(result['error'])
                actual = bytes.fromhex(''.join(result['outputs']))[:len(expected)]
                row['comparisons'].append(dict(address=address, bytes=len(expected), exact=actual == expected,
                                               differing_bytes=sum(a != b for a, b in zip(actual, expected)), seconds=time.monotonic() - start))
            row['status'] = 'pass' if all(c['exact'] for c in row['comparisons']) else 'mismatch'
        except Exception as error: row['reason'] = str(error)
        reports.append(row)
        print(json.dumps(row), flush=True)
        (root / 'report.json').write_text(json.dumps(dict(summary=dict(Counter(r['status'] for r in reports)), launches=reports), indent=2) + '\n')
    print('SUMMARY', dict(Counter(r['status'] for r in reports)), 'unique symbols', len(wanted), flush=True)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    cap = sub.add_parser('capture')
    cap.add_argument('--weights', required=True, type=Path)
    cap.add_argument('--recorder', type=Path, default=Path('/tmp/issue114-yolo-record.so'))
    cap.add_argument('--size', type=int, default=64)
    rep = sub.add_parser('replay')
    rep.add_argument('--max-code-bytes', type=int, default=131072)
    rep.add_argument('--max-threads', type=int, default=65536)
    rep.add_argument('--max-steps', type=int, default=10000)
    for command in (cap, rep): command.add_argument('directory', type=Path)
    args = parser.parse_args()
    dict(capture=capture, replay=replay)[args.command](args)


if __name__ == '__main__': main()
