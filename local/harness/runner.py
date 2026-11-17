"""Two user callbacks, one worker process, one fixed 100 ms decision loop."""
import importlib.util
import json
import multiprocessing
from pathlib import Path
import sys
import threading
import time
import traceback

TICK_MS = 100


def worker(path, pipe):
    try:
        context = json.loads(pipe.recv_bytes())
        sys.path.insert(0, str(Path(path).resolve().parent))
        spec = importlib.util.spec_from_file_location('user_harness', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not callable(getattr(module, 'start', None)) or not callable(getattr(module, 'tick', None)):
            raise ValueError('harness.py must define start(context) and tick(observation, memory)')
        memory = module.start(context)
        pipe.send_bytes(b'{}')
        while True:
            observation = json.loads(pipe.recv_bytes())
            actions = module.tick(observation, memory)
            if not isinstance(actions, list) or len(actions) > 32:
                raise ValueError('tick must return a list of at most 32 actions; return [] to do nothing')
            pipe.send_bytes(json.dumps({'actions': actions}, allow_nan=False).encode())
    except EOFError:
        pass
    except BaseException:
        try:
            pipe.send_bytes(json.dumps({'error': traceback.format_exc()}).encode())
        except (OSError, BrokenPipeError):
            pass
    finally:
        pipe.close()


class Runner:
    def __init__(self, path, snapshot, submit, failed):
        self.path, self.snapshot, self.submit, self.failed = str(path), snapshot, submit, failed
        self.stopped = threading.Event()
        self.thread = self.process = self.pipe = None
        self.ticks = 0

    def call(self, value, timeout):
        self.pipe.send_bytes(json.dumps(value, allow_nan=False).encode())
        if not self.pipe.poll(timeout):
            raise TimeoutError('harness callback exceeded its time limit')
        result = json.loads(self.pipe.recv_bytes(1 << 20))
        if 'error' in result:
            raise RuntimeError(result['error'])
        return result

    def start(self, context):
        ctx = multiprocessing.get_context('spawn')
        self.pipe, child = ctx.Pipe()
        self.process = ctx.Process(target=worker, args=(self.path, child), daemon=True)
        self.process.start()
        child.close()
        try:
            self.call(context, 5)
        except BaseException:
            self.close()
            raise
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        started = previous = deadline = time.monotonic()
        try:
            while not self.stopped.wait(max(0, deadline - time.monotonic())):
                now = time.monotonic()
                observation = self.snapshot()
                observation['tick'] = {'index': self.ticks, 'dt_ms': (now - previous) * 1000,
                                       'elapsed_ms': (now - started) * 1000}
                actions = self.call(observation, TICK_MS / 1000)['actions']
                if self.stopped.is_set():
                    break
                self.submit(actions)
                self.ticks += 1
                previous = now
                deadline += TICK_MS / 1000
                if deadline < time.monotonic():
                    deadline = time.monotonic() + TICK_MS / 1000
        except BaseException as error:
            if self.process.is_alive():
                self.process.kill()
            if not self.stopped.is_set():
                self.failed(str(error) or type(error).__name__)
        finally:
            self.stopped.set()
            if self.process.is_alive():
                self.process.kill()
            self.process.join(timeout=1)
            self.pipe.close()

    def close(self):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=4)
        elif self.process:
            if self.process.is_alive():
                self.process.kill()
            self.process.join(timeout=1)
            self.pipe.close()
