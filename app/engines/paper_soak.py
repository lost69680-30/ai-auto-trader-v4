"""Deterministic synthetic soak for the persistent paper ledger and protection.

This is an engineering stress/recovery check, not a market backtest and not a
strategy-performance estimate. It uses deliberately constructed price paths so
that fills, protective exits, stale-data rejection, and SQLite restarts happen
predictably. The live-data connectivity probe is a separate command.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import tempfile

from app.engines.orchestrator import PaperOrderIntent
from app.engines.paper_replay import (
    FillEvent, MarketUpdateEvent, PaperReplayHarness, RecordIntentEvent, RestartEvent,
)


@dataclass(frozen=True, slots=True)
class PaperSoakReport:
    simulated_hours: int
    events_processed: int
    orders_recorded: int
    duplicate_orders: int
    fills: int
    closed_positions: int
    open_positions: int
    protection_exits: int
    stale_market_updates_rejected: int
    restart_events: int
    realized_net_pnl: float
    total_fees: float
    maximum_realized_drawdown: float
    status_counts: dict[str, int]
    emergency_stop_active: bool
    orders_authorized: bool = False

    @property
    def passed(self) -> bool:
        expected_trades = self.simulated_hours * 12
        return (
            self.orders_authorized is False
            and self.orders_recorded == expected_trades
            and self.duplicate_orders == 0
            and self.fills == expected_trades
            and self.closed_positions == expected_trades
            and self.open_positions == 0
            and self.protection_exits == expected_trades
            and self.stale_market_updates_rejected >= (self.simulated_hours * 12) // 30
            and self.restart_events == self.simulated_hours
            and self.emergency_stop_active is False
        )

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["passed"] = self.passed
        result["interpretation"] = (
            "Synthetic reliability/recovery check only; P&L is generated from scripted prices and is not a strategy-performance estimate."
        )
        return result


def build_deterministic_soak_events(hours: int = 24) -> list[object]:
    """Build 12 controlled paper trades per simulated hour for repeatable soak runs."""
    if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= 720:
        raise ValueError("hours must be an integer from 1 to 720")

    events: list[object] = []
    base_time = 1_800_000_000.0
    trade_count = hours * 12
    for index in range(trade_count):
        at = base_time + index * 300.0
        is_long = index % 4 < 2
        is_win_path = index % 2 == 0
        side = "LONG" if is_long else "SHORT"
        stop_loss, take_profit = (98.0, 103.0) if is_long else (102.0, 97.0)
        proposal_id = f"synthetic-soak-{index:06d}"
        intent = PaperOrderIntent(
            proposal_id=proposal_id,
            signal_id=f"synthetic-signal-{index:06d}",
            symbol="BTCUSDT",
            side=side,
            quantity=0.1,
            entry_price=100.0,
            stop_loss=stop_loss,
            take_profit=take_profit,
            recorded_at=at,
        )
        events.append(RecordIntentEvent(at=at, intent=intent))
        events.append(FillEvent(
            at=at + 1.0, proposal_id=proposal_id, market_price=100.0,
            fee_rate=0.0004, slippage_bps=2.0,
        ))
        middle_price = 100.35 if is_long else 99.65
        events.append(MarketUpdateEvent(
            at=at + 2.0, observed_at=at + 2.0, prices={"BTCUSDT": 100.1 if is_long else 99.9},
        ))
        events.append(MarketUpdateEvent(
            at=at + 30.0, observed_at=at + 30.0, prices={"BTCUSDT": middle_price},
        ))
        if is_long:
            terminal_price = (103.5 if is_win_path else 97.5)
        else:
            terminal_price = (96.5 if is_win_path else 102.5)
        events.append(MarketUpdateEvent(
            at=at + 60.0, observed_at=at + 60.0, prices={"BTCUSDT": terminal_price},
        ))

        # Re-open SQLite once per simulated hour to verify persisted positions/orders.
        if index % 12 == 11:
            events.append(RestartEvent(at=at + 120.0))
        # Inject one stale quote every 30 trades; it must not mutate position marks.
        if index % 30 == 29:
            events.append(MarketUpdateEvent(
                at=at + 180.0, observed_at=at + 60.0, prices={"BTCUSDT": terminal_price},
            ))

    return events


def _max_drawdown(realized_positions: tuple[object, ...]) -> float:
    ordered = sorted(
        (position for position in realized_positions if position.status == "CLOSED" and position.realized_pnl is not None),
        key=lambda position: position.closed_at if position.closed_at is not None else math.inf,
    )
    cumulative = 0.0
    peak = 0.0
    worst = 0.0
    for position in ordered:
        cumulative += float(position.realized_pnl)
        peak = max(peak, cumulative)
        worst = max(worst, peak - cumulative)
    return worst


def run_paper_soak(*, database_path: str | Path, hours: int = 24) -> PaperSoakReport:
    """Run the synthetic soak in a fresh file-backed database and return metrics."""
    if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= 720:
        raise ValueError("hours must be an integer from 1 to 720")
    path = Path(database_path).expanduser()
    if path.exists() and path.stat().st_size > 0:
        raise ValueError("soak database must be new or empty; refusing to mix results with existing paper state")
    path.parent.mkdir(parents=True, exist_ok=True)
    harness = PaperReplayHarness(
        path,
        max_market_age_seconds=5.0,
        fee_rate=0.0004,
        slippage_bps=2.0,
        trailing_distance_pct=None,
    )
    report = harness.run(build_deterministic_soak_events(hours))
    counts = Counter(step.status for step in report.steps)
    closed = tuple(position for position in report.positions if position.status == "CLOSED")
    return PaperSoakReport(
        simulated_hours=hours,
        events_processed=len(report.steps),
        orders_recorded=counts["RECORDED"],
        duplicate_orders=counts["DUPLICATE"],
        fills=counts["FILLED"],
        closed_positions=len(closed),
        open_positions=len(report.open_positions),
        protection_exits=counts["PROTECTION_EXIT"],
        stale_market_updates_rejected=counts["STALE_MARKET_REJECTED"],
        restart_events=counts["RESTARTED"],
        realized_net_pnl=round(sum(float(p.realized_pnl or 0.0) for p in closed), 8),
        total_fees=round(sum(p.total_fees for p in closed), 8),
        maximum_realized_drawdown=round(_max_drawdown(report.positions), 8),
        status_counts=dict(sorted(counts.items())),
        emergency_stop_active=report.emergency_stop_active,
        orders_authorized=report.orders_authorized,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=24, help="simulated hours, 1-720")
    parser.add_argument("--database", type=Path, help="optional new/empty SQLite file (temporary file used otherwise)")
    args = parser.parse_args(argv)
    if not 1 <= args.hours <= 720:
        parser.error("--hours must be from 1 to 720")

    if args.database:
        report = run_paper_soak(database_path=args.database, hours=args.hours)
    else:
        with tempfile.TemporaryDirectory(prefix="ai-trader-paper-soak-") as directory:
            report = run_paper_soak(database_path=Path(directory) / "paper-soak.sqlite3", hours=args.hours)
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    return 0 if report.passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
