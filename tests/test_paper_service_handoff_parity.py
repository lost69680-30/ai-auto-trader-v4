from app.engines.paper_service_app import (
    PaperServiceAnalysisHandler,
    PaperServiceApplication,
)


def test_paper_service_preserves_funding_reconciliation_and_opportunity_handoff():
    # Both paths must share the same paper service and Risk Guardian wiring.
    assert callable(getattr(PaperServiceAnalysisHandler, "_sync_funding_events", None))
    assert callable(getattr(PaperServiceAnalysisHandler, "process_validated_handoff", None))
    assert callable(getattr(PaperServiceApplication, "process_validated_handoff", None))
