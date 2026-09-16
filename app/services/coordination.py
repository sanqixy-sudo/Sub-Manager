"""Serialize short group mutations and publication, never network I/O."""
from functools import wraps
from threading import RLock
from inspect import signature
from contextlib import ExitStack

_locks = [RLock() for _ in range(64)]


def group_lock(subscription_id: int):
    return _locks[subscription_id % len(_locks)]


def group_mutation(function):
    first_parameter = next(iter(signature(function).parameters))
    @wraps(function)
    def guarded(*args, **kwargs):
        subscription_id = args[0] if args else kwargs[first_parameter]
        with group_lock(subscription_id):
            return function(*args, **kwargs)
    guarded.__signature__ = signature(function, eval_str=True)
    return guarded


def all_groups_mutation(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        with ExitStack() as stack:
            for lock in _locks: stack.enter_context(lock)
            return function(*args, **kwargs)
    guarded.__signature__ = signature(function, eval_str=True)
    return guarded
