"""A subprocess queue recorder for isolated socket tests; never starts a model."""
import json
from pathlib import Path
import sys


def install_queue(root):
    root = Path(root)
    binary = root / "fixture-codex"
    binary.write_text(f'''#!{sys.executable}
import json, pathlib, sys, time, uuid
args = sys.argv
thread = args[args.index('--thread') + 1]
prompt = args[args.index('--message') + 1]
root = pathlib.Path({str(root)!r})
directory = root / 'queue' / thread
directory.mkdir(parents=True, exist_ok=True)
failed = (root / ('fail-' + thread)).exists()
record = {{'prompt': prompt, 'failed': failed}}
(directory / (str(time.time_ns()) + '-' + str(uuid.uuid4()) + '.json')).write_text(json.dumps(record))
sys.exit(73 if failed else 0)
''')
    binary.chmod(0o700)
    return str(binary)


def calls(root, thread):
    return [json.loads(path.read_text()) for path in sorted((Path(root) / "queue" / thread).glob("*.json"))]


def messages(root, thread):
    return [json.loads(call["prompt"].splitlines()[-1])[0] for call in calls(root, thread) if not call['failed']]
