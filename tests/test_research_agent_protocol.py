from datetime import datetime, timedelta, timezone

import pytest

from backend.research_agent_protocol import (
    ResearchAgentInput,
    execute_specialist_agents,
)


NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


def _snapshot():
    return ResearchAgentInput(
        symbol="BTCUSDT",
        market_type="FUTURES",
        venue="BINANCE_USDM_TESTNET",
        data_fingerprint="a" * 64,
        observed_at=NOW - timedelta(seconds=10),
        expires_at=NOW + timedelta(seconds=90),
        orders_authorized=False,
    )


def test_specialist_supervisor_records_complete_read_only_evidence():
    handlers = {
        "technical_analyst": lambda snapshot: {"ema20": 101.25, "rsi14": 54.0},
        "regime_analyst": lambda snapshot: {"trend": "UP", "volatility": "MEDIUM"},
    }

    run = execute_specialist_agents(
        _snapshot(),
        handlers,
        required_roles=("technical_analyst", "regime_analyst"),
        as_of=NOW,
        max_age_seconds=60,
    )

    assert run.status == "COMPLETE"
    assert run.missing_roles == ()
    assert {item.role for item in run.messages} == {"technical_analyst", "regime_analyst"}
    assert all(item.orders_authorized is False for item in run.messages)
    assert run.orders_authorized is False


def test_missing_required_specialist_makes_run_incomplete_not_complete():
    run = execute_specialist_agents(
        _snapshot(),
        {"technical_analyst": lambda snapshot: {"ema20": 101.25}},
        required_roles=("technical_analyst", "regime_analyst"),
        as_of=NOW,
        max_age_seconds=60,
    )

    assert run.status == "INCOMPLETE"
    assert run.missing_roles == ("regime_analyst",)


def test_specialist_exception_is_sanitized_and_run_fails_closed():
    def broken(snapshot):
        raise RuntimeError("private URL token=secret")

    run = execute_specialist_agents(
        _snapshot(),
        {"technical_analyst": broken},
        required_roles=("technical_analyst",),
        as_of=NOW,
        max_age_seconds=60,
    )

    assert run.status == "FAILED"
    assert run.messages[0].error_code == "RuntimeError"
    assert run.messages[0].payload == {}
    assert "secret" not in repr(run)


def test_specialist_cannot_return_order_authority_or_risk_controls():
    run = execute_specialist_agents(
        _snapshot(),
        {"technical_analyst": lambda snapshot: {"orders_authorized": True, "score": 1.0}},
        required_roles=("technical_analyst",),
        as_of=NOW,
        max_age_seconds=60,
    )

    assert run.status == "FAILED"
    assert run.messages[0].error_code == "ValueError"
    assert run.orders_authorized is False


def test_expired_or_stale_snapshot_is_rejected_before_handlers_run():
    called = []
    snapshot = ResearchAgentInput(
        symbol="BTCUSDT",
        market_type="FUTURES",
        venue="BINANCE_USDM_TESTNET",
        data_fingerprint="a" * 64,
        observed_at=NOW - timedelta(minutes=10),
        expires_at=NOW - timedelta(minutes=1),
        orders_authorized=False,
    )
    with pytest.raises(ValueError, match="expired or stale"):
        execute_specialist_agents(
            snapshot,
            {"technical_analyst": lambda value: called.append(value) or {}},
            required_roles=("technical_analyst",),
            as_of=NOW,
            max_age_seconds=60,
        )
    assert called == []


def test_risk_guardian_is_not_a_specialist_message_role():
    with pytest.raises(ValueError, match="unsupported specialist role"):
        execute_specialist_agents(
            _snapshot(),
            {"risk_guardian": lambda snapshot: {"approved": True}},
            required_roles=("risk_guardian",),
            as_of=NOW,
            max_age_seconds=60,
        )


def test_agent_supervisor_rejects_non_finite_and_oversized_payloads():
    run = execute_specialist_agents(
        _snapshot(),
        {"technical_analyst": lambda snapshot: {"score": float("nan")}},
        required_roles=("technical_analyst",),
        as_of=NOW,
        max_age_seconds=60,
    )
    assert run.status == "FAILED"
    assert run.messages[0].error_code == "ValueError"


def test_agent_supervisor_rejects_unknown_required_roles():
    with pytest.raises(ValueError, match="unsupported specialist role"):
        execute_specialist_agents(
            _snapshot(),
            {"technical_analyst": lambda snapshot: {}},
            required_roles=("execution_agent",),
            as_of=NOW,
            max_age_seconds=60,
        )


def test_explicit_incomplete_specialist_output_produces_incomplete_run():
    from backend.research_agent_protocol import SpecialistOutput

    run = execute_specialist_agents(
        _snapshot(),
        {"technical_analyst": lambda snapshot: SpecialistOutput("INCOMPLETE", {"rsi14": 54.0})},
        required_roles=("technical_analyst",),
        as_of=NOW,
        max_age_seconds=60,
    )

    assert run.status == "INCOMPLETE"
    assert run.messages[0].status == "INCOMPLETE"
    assert run.orders_authorized is False


def test_run_rejects_message_from_a_different_venue_or_market_type():
    from dataclasses import replace
    from backend.research_agent_protocol import ResearchAgentRun

    run = execute_specialist_agents(
        _snapshot(),
        {"technical_analyst": lambda snapshot: {"ema20": 101.0}},
        required_roles=("technical_analyst",),
        as_of=NOW,
        max_age_seconds=60,
    )
    mismatched = replace(run.messages[0], venue="OTHER_TESTNET")
    with pytest.raises(ValueError, match="market snapshot"):
        ResearchAgentRun(
            status="COMPLETE",
            symbol=run.symbol,
            market_type=run.market_type,
            venue=run.venue,
            data_fingerprint=run.data_fingerprint,
            required_roles=run.required_roles,
            missing_roles=(),
            messages=(mismatched,),
            orders_authorized=False,
        )


def test_mixed_incomplete_and_failed_specialists_never_report_complete():
    from backend.research_agent_protocol import SpecialistOutput

    run = execute_specialist_agents(
        _snapshot(),
        {
            "technical_analyst": lambda snapshot: SpecialistOutput("INCOMPLETE", {}),
            "regime_analyst": lambda snapshot: (_ for _ in ()).throw(RuntimeError("failure")),
        },
        required_roles=("technical_analyst", "regime_analyst"),
        as_of=NOW,
        max_age_seconds=60,
    )

    assert run.status == "FAILED"
    assert {message.status for message in run.messages} == {"FAILED", "INCOMPLETE"}


def test_camel_case_risk_and_order_fields_are_rejected():
    run = execute_specialist_agents(
        _snapshot(),
        {"technical_analyst": lambda snapshot: {"ordersAuthorized": True}},
        required_roles=("technical_analyst",),
        as_of=NOW,
        max_age_seconds=60,
    )

    assert run.status == "FAILED"
    assert run.messages[0].error_code == "ValueError"


def test_nested_specialist_evidence_is_immutable_and_json_safe():
    from backend.research_agent_protocol import ResearchAgentInput

    captured = {"indicator": {"ema20": 100.0}, "conflicts": ["1h"]}
    run = execute_specialist_agents(
        _snapshot(),
        {"technical_analyst": lambda snapshot: captured},
        required_roles=("technical_analyst",),
        as_of=NOW,
        max_age_seconds=60,
    )
    captured["indicator"]["ema20"] = -1.0
    captured["conflicts"].append("4h")

    payload = run.messages[0].payload
    assert payload["indicator"]["ema20"] == 100.0
    assert payload["conflicts"] == ("1h",)
    with pytest.raises(TypeError):
        payload["indicator"]["ema20"] = 999.0


def test_specialist_run_cannot_be_constructed_complete_with_missing_role():
    from backend.research_agent_protocol import ResearchAgentRun

    run = execute_specialist_agents(
        _snapshot(),
        {"technical_analyst": lambda snapshot: {"ema20": 100.0}},
        required_roles=("technical_analyst", "regime_analyst"),
        as_of=NOW,
        max_age_seconds=60,
    )
    assert run.status == "INCOMPLETE"
    assert run.missing_roles == ("regime_analyst",)


def test_specialist_message_exposes_versioned_contract():
    run = execute_specialist_agents(
        _snapshot(),
        {"technical_analyst": lambda snapshot: {"ema20": 100.0}},
        required_roles=("technical_analyst",),
        as_of=NOW,
        max_age_seconds=60,
    )

    assert run.messages[0].protocol_version == "1.0"
    assert run.messages[0].orders_authorized is False
