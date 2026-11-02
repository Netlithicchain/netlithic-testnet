"""Strict JSON parsing shared by the node, the web wallet, the desktop app and the ops tools.

Only plain JSON numbers are accepted: number constants outside the JSON standard are refused, and so is any
number too large to hold as a float."""
import json
import math


def _refuse_constant(name):
    raise ValueError(f"number constant {name} not allowed")


def _finite_float(text):
    v = float(text)
    if not math.isfinite(v):
        raise ValueError("number out of range")
    return v


def loads(text):
    return json.loads(text, parse_constant=_refuse_constant, parse_float=_finite_float)


def load(fp):
    return loads(fp.read())
