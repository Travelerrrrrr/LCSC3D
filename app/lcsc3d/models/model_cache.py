"""Bounded resource reuse within one export, including its worker threads."""
from collections import OrderedDict
from concurrent.futures import Future, TimeoutError
from contextvars import ContextVar
from functools import wraps
import threading


_current = ContextVar('model_resources', default=None)


class ModelCache:
    def __init__(self, max_bytes=32 * 1024 * 1024, max_entries=256):
        self.max_bytes, self.max_entries = max_bytes, max_entries
        self.items, self.pending = OrderedDict(), {}
        self.size = 0
        self.lock = threading.RLock()

    def get(self, key, create, size, check):
        check()
        with self.lock:
            if key in self.items:
                value, _ = self.items[key]
                self.items.move_to_end(key)
                return value
            future = self.pending.get(key)
            owner = future is None
            if owner:
                future = self.pending[key] = Future()
        if not owner:
            while True:
                check()
                try:
                    value = future.result(timeout=.05)
                    check()
                    return value
                except TimeoutError:
                    if future.done():
                        raise  # The resource operation itself raised TimeoutError.
        try:
            value = create()
            check()
            cost = size(value) if value is not None else 0
            with self.lock:
                # Missing/failed resources are never cached, including optional
                # 404 results. Oversized values only serve current waiters.
                if value is not None and 0 < cost <= self.max_bytes:
                    self.items[key] = value, cost
                    self.size += cost
                    while self.size > self.max_bytes or len(self.items) > self.max_entries:
                        _, (_, removed) = self.items.popitem(last=False)
                        self.size -= removed
                future.set_result(value)
                self.pending.pop(key, None)
            return value
        except BaseException as exc:
            with self.lock:
                future.set_exception(exc)
                self.pending.pop(key, None)
            raise


def cached_model(key, create, *, size=len, check=lambda: None):
    cache = _current.get()
    if cache is None:
        check()
        return create()
    return cache.get(key, create, size, check)


def reuse_models(function):
    """Start a fresh scope; nested exports and copy_context workers share it."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        if _current.get() is not None:
            return function(*args, **kwargs)
        token = _current.set(ModelCache())
        try:
            return function(*args, **kwargs)
        finally:
            _current.reset(token)
    return wrapped
