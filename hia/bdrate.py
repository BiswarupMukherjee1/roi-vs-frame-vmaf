"""Bjontegaard delta rate / delta quality.

Default interpolation is PCHIP on log-rate (as in AOM CTC / libaom tools), which avoids the
overshoot of the original cubic polynomial fit. The cubic fit is kept for comparison.
"""
from __future__ import annotations

import numpy as np
from scipy.interpolate import PchipInterpolator


def _prep(x, y):
    x, y = np.asarray(x, float), np.asarray(y, float)
    o = np.argsort(x)
    x, y = x[o], y[o]
    keep = np.concatenate([[True], np.diff(x) > 1e-9])
    return x[keep], y[keep]


def _integral(x, y, lo, hi, method):
    if method == "pchip":
        f = PchipInterpolator(x, y)
        return float(f.integrate(lo, hi))
    p = np.polyint(np.polyfit(x, y, 3))
    return float(np.polyval(p, hi) - np.polyval(p, lo))


def bd_rate(rate_a, q_a, rate_b, q_b, method="pchip") -> float:
    """% bitrate of B relative to A at equal quality (negative = B needs fewer bits)."""
    qa, la = _prep(q_a, np.log(rate_a))
    qb, lb = _prep(q_b, np.log(rate_b))
    if len(qa) < 2 or len(qb) < 2:
        return float("nan")
    lo, hi = max(qa.min(), qb.min()), min(qa.max(), qb.max())
    if hi - lo <= 1e-6:
        return float("nan")
    d = (_integral(qb, lb, lo, hi, method) - _integral(qa, la, lo, hi, method)) / (hi - lo)
    return float((np.exp(d) - 1) * 100)


def bd_quality(rate_a, q_a, rate_b, q_b, method="pchip") -> float:
    """Average quality difference B - A at equal bitrate."""
    la, qa = _prep(np.log(rate_a), q_a)
    lb, qb = _prep(np.log(rate_b), q_b)
    if len(la) < 2 or len(lb) < 2:
        return float("nan")
    lo, hi = max(la.min(), lb.min()), min(la.max(), lb.max())
    if hi - lo <= 1e-6:
        return float("nan")
    return float((_integral(lb, qb, lo, hi, method) - _integral(la, qa, lo, hi, method)) / (hi - lo))


def overlap(q_a, q_b) -> float:
    """Fraction of the joint quality range that both curves cover (low = BD number is fragile)."""
    lo, hi = max(min(q_a), min(q_b)), min(max(q_a), max(q_b))
    span = max(max(q_a), max(q_b)) - min(min(q_a), min(q_b))
    return max(0.0, hi - lo) / span if span > 0 else 0.0


def delta_at_equal(x_a, y_a, x_b, y_b, method="pchip") -> float:
    """Average y_b - y_a over the shared x range (e.g. overlay PSNR at equal frame VMAF)."""
    xa, ya = _prep(x_a, y_a)
    xb, yb = _prep(x_b, y_b)
    if len(xa) < 2 or len(xb) < 2:
        return float("nan")
    lo, hi = max(xa.min(), xb.min()), min(xa.max(), xb.max())
    if hi - lo <= 1e-6:
        return float("nan")
    return float((_integral(xb, yb, lo, hi, method) - _integral(xa, ya, lo, hi, method)) / (hi - lo))
