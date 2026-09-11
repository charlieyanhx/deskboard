import math

import pytest

from deskboard.engine import greeks as bs


def test_canonical_atm_case_call_and_put():
    """S=K=100, r=5%, sigma=20%, T=1: call 10.4505835722, put 5.5735260223 (closed form, d1=0.35, d2=0.15)."""
    c = bs.greeks(100, 100, 1.0, 0.2, "C", r=0.05)
    p = bs.greeks(100, 100, 1.0, 0.2, "P", r=0.05)
    assert c.price == pytest.approx(10.4505835722, abs=1e-8)
    assert p.price == pytest.approx(5.5735260223, abs=1e-8)
    assert c.delta - p.delta == pytest.approx(1.0, abs=1e-12)          # put-call delta parity (q = 0)
    assert c.gamma == pytest.approx(p.gamma) and c.vega == pytest.approx(p.vega)


def test_hull_textbook_example():
    """Hull, Options, Futures and Other Derivatives, worked BSM example: S=42, K=40, r=10%, sigma=20%,
    T=0.5 → call 4.76, put 0.81 (exact 4.7594223929 / 0.8085993729)."""
    assert bs.price(42, 40, 0.5, 0.2, "C", r=0.10) == pytest.approx(4.7594223929, abs=1e-8)
    assert bs.price(42, 40, 0.5, 0.2, "P", r=0.10) == pytest.approx(0.8085993729, abs=1e-8)


def test_put_call_parity():
    S, K, T, r, q = 480.0, 500.0, 0.25, 0.03, 0.01
    c, p = bs.price(S, K, T, 0.22, "C", r, q), bs.price(S, K, T, 0.22, "P", r, q)
    assert c - p == pytest.approx(S * math.exp(-q * T) - K * math.exp(-r * T), abs=1e-10)


def test_finite_difference_greeks():
    S, K, T, s = 500.0, 480.0, 0.12, 0.19
    g = bs.greeks(S, K, T, s, "P")
    h = 1e-3
    fd_delta = (bs.price(S + h, K, T, s, "P") - bs.price(S - h, K, T, s, "P")) / (2 * h)
    fd_gamma = (bs.price(S + h, K, T, s, "P") - 2 * bs.price(S, K, T, s, "P") + bs.price(S - h, K, T, s, "P")) / h**2
    fd_vega = (bs.price(S, K, T, s + 1e-5, "P") - bs.price(S, K, T, s - 1e-5, "P")) / 2e-5
    fd_theta = (bs.price(S, K, T - 1 / 365, s, "P") - bs.price(S, K, T, s, "P"))
    assert g.delta == pytest.approx(fd_delta, rel=1e-5)
    assert g.gamma == pytest.approx(fd_gamma, rel=1e-3)
    assert g.vega == pytest.approx(fd_vega, rel=1e-5)
    assert g.theta == pytest.approx(fd_theta, rel=2e-2)


def test_implied_vol_roundtrip_and_bounds():
    px = bs.price(500, 520, 0.1, 0.31, "C")
    assert bs.implied_vol(px, 500, 520, 0.1, "C") == pytest.approx(0.31, abs=1e-8)
    assert math.isnan(bs.implied_vol(-1.0, 500, 520, 0.1, "C"))       # below intrinsic
    assert math.isnan(bs.implied_vol(1e9, 500, 520, 0.1, "C"))        # above the spot cap
    assert math.isnan(bs.implied_vol(1.0, 500, 520, 0.0, "C"))        # expired


def test_expiry_edge():
    g = bs.greeks(510, 500, 0.0, 0.2, "C")
    assert g.price == 10.0 and g.delta == 1.0 and g.gamma == 0.0
