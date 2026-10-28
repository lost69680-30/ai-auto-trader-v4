"""ATR-based price planning with fail-closed freshness and cost checks.

This component generates a candidate plan only. It never authorizes orders and
must not be interpreted as evidence that the strategy is profitable.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
import math

from app.engines.signal_handoff import SignalPricePlan
from backend.market_data_adapter import MarketCandle


def _aware_utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _finite(value: object, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(result):
        raise ValueError(f"{field} must be finite")
    return result


def _round_tick(price: float, tick: float, mode: str) -> float:
    d_price, d_tick = Decimal(str(price)), Decimal(str(tick))
    rounding = {"floor": ROUND_FLOOR, "ceil": ROUND_CEILING, "nearest": ROUND_HALF_UP}[mode]
    units = (d_price / d_tick).quantize(Decimal("1"), rounding=rounding)
    return float(units * d_tick)


@dataclass(frozen=True, slots=True)
class PricePlanningConfig:
    atr_period: int = 14
    stop_atr_multiple: float = 1.5
    reward_to_risk: float = 2.0
    trigger_buffer_atr: float = 0.1
    minimum_net_reward_to_risk: float = 1.2
    max_reference_age_seconds: int = 30
    max_candle_age_seconds: int = 180
    max_future_skew_seconds: int = 2
    max_round_trip_cost_pct: float = 0.5

    def __post_init__(self) -> None:
        if isinstance(self.atr_period, bool) or not isinstance(self.atr_period, int) or self.atr_period < 2:
            raise ValueError("atr_period must be an integer of at least 2")
        for field_name in ("stop_atr_multiple", "reward_to_risk", "minimum_net_reward_to_risk"):
            value = _finite(getattr(self, field_name), field_name)
            if value <= 0:
                raise ValueError(f"{field_name} must be positive")
        trigger = _finite(self.trigger_buffer_atr, "trigger_buffer_atr")
        if trigger < 0:
            raise ValueError("trigger_buffer_atr must be non-negative")
        if self.max_reference_age_seconds <= 0 or self.max_candle_age_seconds <= 0 or self.max_future_skew_seconds < 0:
            raise ValueError("age limits must be positive and future skew non-negative")
        cost = _finite(self.max_round_trip_cost_pct, "max_round_trip_cost_pct")
        if cost < 0:
            raise ValueError("max_round_trip_cost_pct must be non-negative")


@dataclass(frozen=True, slots=True)
class PricePlanRequest:
    signal_id: str
    symbol: str
    direction: str
    reference_price: float
    reference_price_at: datetime
    candles: tuple[MarketCandle, ...]
    now: datetime
    expires_at: datetime
    estimated_round_trip_cost_pct: float
    tick_size: float

    def __post_init__(self) -> None:
        if not self.signal_id.strip():
            raise ValueError("signal_id is required")
        if not self.symbol.strip() or self.symbol != self.symbol.strip().upper():
            raise ValueError("symbol must be non-empty, trimmed and uppercase-normalized")
        if self.direction not in {"LONG", "SHORT"}:
            raise ValueError("direction must be LONG or SHORT")
        now = _aware_utc(self.now, "now")
        created = _aware_utc(self.reference_price_at, "reference_price_at")
        expires = _aware_utc(self.expires_at, "expires_at")
        if expires <= now:
            raise ValueError("expires_at must be later than now")
        if created > now + timedelta(seconds=2):
            raise ValueError("reference_price_at is too far in the future")
        tick = _finite(self.tick_size, "tick_size")
        if tick <= 0:
            raise ValueError("tick_size must be positive")
        cost = _finite(self.estimated_round_trip_cost_pct, "estimated_round_trip_cost_pct")
        if cost < 0:
            raise ValueError("estimated_round_trip_cost_pct must be non-negative")


@dataclass(frozen=True, slots=True)
class PricePlanResult:
    status: str
    reason: str
    plan: SignalPricePlan | None = None
    atr: float | None = None
    net_reward_to_risk: float | None = None
    orders_authorized: bool = False


def plan_prices(request: PricePlanRequest, config: PricePlanningConfig | None = None) -> PricePlanResult:
    """Create a closed-candle ATR plan or return NO_PLAN; never places an order."""
    config = config or PricePlanningConfig()
    now = _aware_utc(request.now, "now")
    reference_at = _aware_utc(request.reference_price_at, "reference_price_at")
    expires_at = _aware_utc(request.expires_at, "expires_at")
    price = _finite(request.reference_price, "reference_price")
    costs = _finite(request.estimated_round_trip_cost_pct, "estimated_round_trip_cost_pct")
    tick = _finite(request.tick_size, "tick_size")
    if price <= 0:
        return _no_plan("reference price must be finite and positive")
    if now - reference_at > timedelta(seconds=config.max_reference_age_seconds):
        return _no_plan("reference market price is stale")
    if reference_at > now + timedelta(seconds=config.max_future_skew_seconds):
        return _no_plan("reference market price timestamp is in the future")
    if costs > config.max_round_trip_cost_pct:
        return _no_plan("estimated transaction costs exceed configured maximum")
    if expires_at <= now:
        return _no_plan("signal expiry must be in the future")

    if not request.candles:
        return _no_plan("candle history is missing")
    validated: list[MarketCandle] = []
    previous_open: datetime | None = None
    for candle in request.candles:
        if candle.symbol != request.symbol:
            return _no_plan("candle symbol does not match requested market")
        try:
            opened = _aware_utc(candle.open_time, "candle open_time")
            closed = _aware_utc(candle.close_time, "candle close_time")
            values = tuple(_finite(getattr(candle, name), f"candle {name}")
                           for name in ("open", "high", "low", "close", "volume", "quote_volume"))
        except (ValueError, AttributeError):
            return _no_plan("candle data contains invalid values or timestamps")
        open_price, high, low, close, volume, quote_volume = values
        if (closed < opened or min(open_price, high, low, close) <= 0 or volume < 0 or quote_volume < 0
                or high < max(open_price, close, low) or low > min(open_price, close, high)):
            return _no_plan("candle OHLCV geometry is invalid")
        if previous_open is not None and opened <= previous_open:
            return _no_plan("candle timestamps must be strictly increasing")
        if opened > now + timedelta(seconds=config.max_future_skew_seconds):
            return _no_plan("candle open time is in the future")
        if candle.is_closed:
            if closed > now + timedelta(seconds=config.max_future_skew_seconds):
                return _no_plan("closed candle has a future close timestamp")
            validated.append(candle)
        elif closed < now:
            return _no_plan("candle is marked open even though its close time has passed")
        # Unclosed candles are expected from the latest REST kline response but
        # are never included in ATR calculations.
        previous_open = opened

    if not validated:
        return _no_plan("no closed candles available")
    last_closed_at = _aware_utc(validated[-1].close_time, "last candle close_time")
    if now - last_closed_at > timedelta(seconds=config.max_candle_age_seconds):
        return _no_plan("latest closed candle is stale")
    if last_closed_at > now + timedelta(seconds=config.max_future_skew_seconds):
        return _no_plan("latest closed candle timestamp is in the future")
    if len(validated) < config.atr_period + 1:
        return _no_plan(f"insufficient closed candles for ATR({config.atr_period})")

    history = validated[-(config.atr_period + 1):]
    true_ranges: list[float] = []
    for previous, current in zip(history, history[1:]):
        true_ranges.append(max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        ))
    atr = sum(true_ranges) / len(true_ranges)
    if not math.isfinite(atr) or atr <= 0:
        return _no_plan("ATR is invalid or zero")

    entry = _round_tick(price, tick, "nearest")
    stop_distance = max(atr * config.stop_atr_multiple, tick)
    target_distance = stop_distance * config.reward_to_risk
    trigger_distance = max(atr * config.trigger_buffer_atr, tick)
    if request.direction == "LONG":
        stop = _round_tick(entry - stop_distance, tick, "floor")
        target = _round_tick(entry + target_distance, tick, "ceil")
        trigger = _round_tick(entry + trigger_distance, tick, "ceil")
        trigger_rule = "AT_OR_ABOVE"
    else:
        stop = _round_tick(entry + stop_distance, tick, "ceil")
        target = _round_tick(entry - target_distance, tick, "floor")
        trigger = _round_tick(entry - trigger_distance, tick, "floor")
        trigger_rule = "AT_OR_BELOW"

    if min(entry, stop, target, trigger) <= 0:
        return _no_plan("rounded prices must remain positive")
    if request.direction == "LONG" and not (stop < entry < target and trigger > entry):
        return _no_plan("rounded long price geometry is invalid")
    if request.direction == "SHORT" and not (target < entry < stop and trigger < entry):
        return _no_plan("rounded short price geometry is invalid")

    risk_pct = abs(entry - stop) / entry * 100.0
    gross_reward_pct = abs(target - entry) / entry * 100.0
    net_reward_pct = gross_reward_pct - costs
    all_in_risk_pct = risk_pct + costs
    net_rr = net_reward_pct / all_in_risk_pct if all_in_risk_pct > 0 else 0.0
    if net_reward_pct <= 0 or net_rr < config.minimum_net_reward_to_risk:
        return _no_plan("cost-adjusted reward-to-risk is below configured minimum", atr=atr, net_rr=net_rr)

    plan = SignalPricePlan(
        signal_id=request.signal_id,
        direction=request.direction,
        created_at=now,
        expires_at=expires_at,
        market_data_at=reference_at,
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        estimated_round_trip_cost_pct=costs,
        trigger_price=trigger,
        trigger_rule=trigger_rule,
        invalidation_price=stop,
    )
    return PricePlanResult(
        status="READY", reason="ATR-based plan passed input, freshness, geometry, and cost checks",
        plan=plan, atr=atr, net_reward_to_risk=net_rr, orders_authorized=False,
    )


def _no_plan(reason: str, *, atr: float | None = None, net_rr: float | None = None) -> PricePlanResult:
    return PricePlanResult("NO_PLAN", reason, plan=None, atr=atr, net_reward_to_risk=net_rr, orders_authorized=False)
