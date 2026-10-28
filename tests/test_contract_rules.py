from backend.contract_rules import validate_paper_quantity
from backend.scanner import Contract


def contract(**kwargs):
    defaults = dict(symbol="BTCUSDT", status="TRADING", contract_type="PERPETUAL", quote_asset="USDT",
                    tick_size=0.1, quantity_step=0.001, min_quantity=0.001,
                    max_quantity=100.0, min_notional=5.0)
    defaults.update(kwargs)
    return Contract(**defaults)


def test_quantity_filters_accept_aligned_quantity():
    result = validate_paper_quantity(contract(), quantity=0.01, price=1000)
    assert result.accepted is True
    assert result.notional == 10
    assert result.orders_authorized is False


def test_quantity_filters_reject_step_minimum_maximum_and_notional():
    assert "step" in validate_paper_quantity(contract(), quantity=0.0011, price=1000).reason
    assert "minimum" in validate_paper_quantity(contract(min_quantity=0.002), quantity=0.001, price=1000).reason
    assert "maximum" in validate_paper_quantity(contract(), quantity=101, price=1000).reason
    assert "notional" in validate_paper_quantity(contract(), quantity=0.01, price=100).reason


def test_missing_filters_fail_closed():
    result = validate_paper_quantity(contract(max_quantity=None), quantity=1, price=100)
    assert result.accepted is False
    assert result.quantity is None
