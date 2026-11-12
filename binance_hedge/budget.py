"""Absolute operation budgets, propagated to both independent HTTP lanes."""
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
import time

from .planner import GuardError


_deadline = ContextVar("binance_operation_deadline", default=None)


class BudgetExpired(GuardError):
    def __init__(self):
        super().__init__("本次核验已到总期限；保留原订单号和账户锁，不重发")


def deadline():
    return _deadline.get()


def remaining(default=2):
    end = deadline()
    value = default if end is None else min(default, end - time.monotonic())
    if value <= 0:
        raise BudgetExpired()
    return value


@contextmanager
def budget(end):
    previous = deadline()
    token = _deadline.set(min(previous, end) if previous is not None else end)
    try:
        remaining()
        yield
    finally:
        _deadline.reset(token)


def submit(pool, function, *args):
    return pool.submit(copy_context().run, function, *args)
