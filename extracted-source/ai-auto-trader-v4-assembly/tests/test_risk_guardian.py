import unittest
from app.core.models import TradeProposal, RiskLimits, MarketSnapshot
from app.core.risk_guardian import evaluate_proposal


class RiskGuardianTests(unittest.TestCase):
    def setUp(self):
        self.limits = RiskLimits(
            max_position_notional=1000,
            max_daily_loss=100,
            max_open_positions=3,
            max_risk_per_trade=20,
        )
        self.market = MarketSnapshot(symbol="BTCUSDT", mark_price=50000, observed_at=1000, is_fresh=True)
        self.proposal = TradeProposal(
            proposal_id="p1", symbol="BTCUSDT", side="LONG", quantity=0.01,
            entry_price=50000, stop_loss=49000, take_profit=52000,
            estimated_risk=10, signal_timestamp=1000,
        )

    def test_approves_valid_proposal(self):
        decision = evaluate_proposal(self.proposal, self.market, self.limits,
                                     daily_pnl=-10, open_positions=1, now=1000, equity_baseline=10_000, same_asset_exposure=0, gross_exposure=0)
        self.assertTrue(decision.approved)
        self.assertEqual(decision.code, "APPROVED")

    def test_rejects_stale_market_data(self):
        stale = MarketSnapshot(symbol="BTCUSDT", mark_price=50000, observed_at=1, is_fresh=False)
        decision = evaluate_proposal(self.proposal, stale, self.limits,
                                     daily_pnl=0, open_positions=0, now=1000, equity_baseline=10_000, same_asset_exposure=0, gross_exposure=0)
        self.assertFalse(decision.approved)
        self.assertEqual(decision.code, "STALE_MARKET_DATA")

    def test_rejects_when_daily_loss_limit_breached(self):
        decision = evaluate_proposal(self.proposal, self.market, self.limits,
                                     daily_pnl=-101, open_positions=0, now=1000, equity_baseline=10_000, same_asset_exposure=0, gross_exposure=0)
        self.assertFalse(decision.approved)
        self.assertEqual(decision.code, "DAILY_LOSS_LIMIT")

    def test_rejects_invalid_long_stop_geometry(self):
        bad = TradeProposal(**{**self.proposal.__dict__, "stop_loss": 50100})
        decision = evaluate_proposal(bad, self.market, self.limits,
                                     daily_pnl=0, open_positions=0, now=1000, equity_baseline=10_000, same_asset_exposure=0, gross_exposure=0)
        self.assertFalse(decision.approved)
        self.assertEqual(decision.code, "INVALID_STOP_GEOMETRY")

    def test_rejects_excess_notional(self):
        big = TradeProposal(**{**self.proposal.__dict__, "quantity": 0.1})
        decision = evaluate_proposal(big, self.market, self.limits,
                                     daily_pnl=0, open_positions=0, now=1000, equity_baseline=10_000, same_asset_exposure=0, gross_exposure=0)
        self.assertFalse(decision.approved)
        self.assertEqual(decision.code, "POSITION_NOTIONAL_LIMIT")


if __name__ == "__main__":
    unittest.main()
