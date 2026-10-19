"""Sanity tests for the pure-Python parts.   python3 -m pytest -q tests  (or python3 tests/test_core.py)"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hia.bdrate import bd_quality, bd_rate  # noqa: E402
from hia.metrics import block_means, cer, ssim_map  # noqa: E402


def test_bdrate_constant_ratio():
    r = np.array([1000, 2000, 4000, 8000.0])
    q = np.array([70, 80, 88, 94.0])
    for m in ("pchip", "poly"):
        assert abs(bd_rate(r, q, 0.8 * r, q, m) - (-20.0)) < 1e-6
        assert abs(bd_rate(r, q, r, q, m)) < 1e-9


def test_bdquality_shift():
    r = np.array([1000, 2000, 4000, 8000.0])
    q = np.array([70, 80, 88, 94.0])
    assert abs(bd_quality(r, q, r, q + 2.5) - 2.5) < 1e-6


def test_ssim_identity_and_drop():
    rng = np.random.default_rng(0)
    x = rng.integers(0, 256, (128, 192)).astype(np.uint8)
    assert np.allclose(ssim_map(x, x), 1.0, atol=1e-4)
    smooth = np.tile(np.linspace(40, 200, 192), (128, 1)).astype(np.uint8)
    y = np.clip(smooth + rng.normal(0, 20, smooth.shape), 0, 255).astype(np.uint8)
    assert ssim_map(smooth, y).mean() < 0.5


def test_block_means_partial_blocks():
    m = np.ones((100, 130))
    bm = block_means(m, 64)
    assert bm.shape == (2, 3) and np.allclose(bm, 1.0)
    m[64:, :] = 0
    assert np.allclose(block_means(m, 64)[1], 0)


def test_cer():
    assert cer("MKD 2-1 RNS 2ND HALF 38:12.3", "MKD  2 - 1  RNS2ND HALF  38:12.3") == 0
    assert 0 < cer("MKD 2-1 RNS 2ND HALF 38:12.8", "MKD 2-1 RNS 2ND HALF 38:12.3") < 0.1
    assert cer("", "ABC") == 1.0


if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f()
            print("ok", k)
