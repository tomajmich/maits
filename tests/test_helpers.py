import pytest

from maits.ctrader.auth import AuthError, extract_code
from maits.ctrader.trader import lots_to_volume, normalize_symbol, pips_to_relative

FX_LOT = 10_000_000  # 100,000 units in 1/100 units


def test_lots_to_volume_basic():
    assert lots_to_volume(1, FX_LOT, 100_000, 100_000, 10_000_000_000) == FX_LOT
    assert lots_to_volume(0.01, FX_LOT, 100_000, 100_000, 10_000_000_000) == 100_000


def test_lots_to_volume_has_no_float_error():
    # 0.07 * 10_000_000 is 700000.0000000001 in floating point
    assert lots_to_volume(0.07, FX_LOT, 1, 1, 10**12) == 700_000


def test_lots_to_volume_rounds_down_to_step():
    assert lots_to_volume(0.0149, FX_LOT, 100_000, 100_000, 10**12) == 100_000


def test_lots_to_volume_limits():
    with pytest.raises(ValueError, match="minimum"):
        lots_to_volume(0.001, FX_LOT, 100_000, 100_000, 10**12)
    with pytest.raises(ValueError, match="maximum"):
        lots_to_volume(500, FX_LOT, 100_000, 100_000, 10 * FX_LOT)
    with pytest.raises(ValueError):
        lots_to_volume(0, FX_LOT, 1, 1, 10**12)


def test_pips_to_relative_5_digit_pair():  # EURUSD: 1.12345, pip = 0.0001
    assert pips_to_relative(10, pip_position=4, digits=5) == 100  # 0.00100 in 1/100000
    assert pips_to_relative(0.5, pip_position=4, digits=5) == 5


def test_pips_to_relative_jpy_pair():  # USDJPY: 123.456, pip = 0.01, price grid = 0.001
    assert pips_to_relative(10, pip_position=2, digits=3) == 10_000  # 0.10 yen
    assert pips_to_relative(0.5, pip_position=2, digits=3) == 500


def test_pips_to_relative_snaps_to_price_grid():
    # 4-digit symbol: grid is 10 protocol units, so 0.34 pips (3.4 units) snaps up to one step
    assert pips_to_relative(0.34, pip_position=4, digits=4) == 10


def test_normalize_symbol():
    assert normalize_symbol(" oanda:eur/usd ") == "EURUSD"
    assert normalize_symbol("EURUSD") == "EURUSD"


def test_extract_code():
    assert extract_code("abc123") == "abc123"
    assert extract_code("http://localhost:8080/callback?code=abc123&state=x") == "abc123"
    with pytest.raises(AuthError):
        extract_code("http://localhost:8080/callback?error=access_denied&code=")
