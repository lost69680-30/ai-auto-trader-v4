import unittest
from app.core.models import TradeProposal, RiskLimits, MarketSnapshot
from app.core.risk_guardian import evaluate_proposal


class AdditionalRiskGuardianTests(unittest.TestCase):
    def setUp(self):
        self.limits = RiskLimits(1000, 100, 3, 20, max_signal_age_seconds=60)
        self.market = MarketSnapshot("BTCUSDT", 50000, 1000, True)
        self.proposal = TradeProposal("p2", "BTCUSDT", "SHORT", 0.01, 50000, 51000, 48000, 10, 1000)

    def test_approves_valid_short_geometry(self):
        d = evaluate_proposal(self.proposal, self.market, self.limits, daily_pnl=0, open_positions=0, now=1000)
        self.assertTrue(d.approved)

    def test_rejects_symbol_mismatch(self):
        market = MarketSnapshot("ETHUSDT", 3000, 1000, True)
        d = evaluate_proposal(self.proposal, market, self.limits, daily_pnl=0, open_positions=0, now=1000)
        self.assertEqual(d.code, "SYMBOL_MISMATCH")

    def test_rejects_expired_signal(self):
        old = TradeProposal("p3", "BTCUSDT", "SHORT", 0.01, 50000, 51000, 48000, 10, 900)
        d = evaluate_proposal(old, self.market, self.limits, daily_pnl=0, open_positions=0, now=1000)
        self.assertEqual(d.code, "STALE_SIGNAL")

    def test_rejects_open_position_cap(self):
        d = evaluate_proposal(self.proposal, self.market, self.limits, daily_pnl=0, open_positions=3, now=1000)
        self.assertEqual(d.code, "OPEN_POSITION_LIMIT")

    def test_rejects_excess_per_trade_risk(self):
        risky = TradeProposal("p4", "BTCUSDT", "SHORT", 0.01, 50000, 51000, 48000, 21, 1000)
        d = evaluate_proposal(risky, self.market, self.limits, daily_pnl=0, open_positions=0, now=1000)
        self.assertEqual(d.code, "TRADE_RISK_LIMIT")


if __name__ == "__main__":
    unittest.main()
