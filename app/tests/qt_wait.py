"""Wait for GUI correctness checks while dispatching Qt events."""

from math import ceil
from time import monotonic


def _pump_events(milliseconds):
    from PySide6.QtTest import QTest

    QTest.qWait(milliseconds)


def _timeout_error(started, state):
    return AssertionError(f'Timed out after {monotonic() - started:.3f} s: {state}')


def wait_until(predicate, *, message, timeout=60, signal_spy=None):
    """Wait for a condition and return True, or raise with the state from message().

    A spy on a GUI-thread signal must be created before triggering the operation.
    The signal and the subsequent condition share the same timeout in seconds.
    """
    started = monotonic()
    if signal_spy is not None and not signal_spy.count():
        if not signal_spy.wait(ceil(timeout * 1000)):
            raise _timeout_error(started, f'Completion signal not received. {message()}')

    while not predicate():
        remaining = timeout - (monotonic() - started)
        if remaining <= 0:
            raise _timeout_error(started, message())
        _pump_events(min(10, ceil(remaining * 1000)))
    return True
