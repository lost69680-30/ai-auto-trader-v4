from app.engines.supabase_audit_mirror import stable_source_event_key


def test_source_event_key_is_stable_and_namespaced():
    assert stable_source_event_key("render-prod", 17) == "render-prod:17"
    assert stable_source_event_key("render-prod", 17) != stable_source_event_key("render-stage", 17)
