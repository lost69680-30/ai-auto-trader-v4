from app.engines.storage_guardian import assess_capacity, build_storage_report


def test_capacity_is_healthy_below_warning_threshold():
    result = assess_capacity(used_bytes=349, quota_bytes=500)
    assert result["status"] == "HEALTHY"
    assert result["usage_percent"] == 69.8
    assert result["capacity_limit_reached"] is False


def test_capacity_warning_starts_at_seventy_percent():
    result = assess_capacity(used_bytes=350, quota_bytes=500)
    assert result["status"] == "WARNING"


def test_capacity_critical_starts_at_eighty_five_percent():
    result = assess_capacity(used_bytes=425, quota_bytes=500)
    assert result["status"] == "CRITICAL"


def test_capacity_emergency_starts_at_ninety_five_percent():
    result = assess_capacity(used_bytes=475, quota_bytes=500)
    assert result["status"] == "EMERGENCY"
    assert result["capacity_limit_reached"] is False


def test_capacity_exhaustion_is_flagged_without_granting_order_authority():
    result = assess_capacity(used_bytes=500, quota_bytes=500)
    assert result["status"] == "CAPACITY_EXHAUSTED"
    assert result["capacity_limit_reached"] is True
    assert "orders_authorized" not in result


def test_capacity_rejects_invalid_inputs():
    for used, quota in ((-1, 500), (1, 0), (1, -1)):
        try:
            assess_capacity(used_bytes=used, quota_bytes=quota)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid storage measurements must be rejected")


def test_report_sorts_tables_by_total_size_without_mutating_inputs():
    tables = [
        {"schema_name": "paper_ledger", "table_name": "small", "total_bytes": 10, "index_bytes": 2},
        {"schema_name": "paper_ledger", "table_name": "large", "total_bytes": 90, "index_bytes": 40},
    ]
    report = build_storage_report(
        database_bytes=100,
        quota_bytes=500,
        table_rows=tables,
        object_rows=[],
    )
    assert report["capacity"]["status"] == "HEALTHY"
    assert [row["table_name"] for row in report["largest_tables"]] == ["large", "small"]
    assert report["largest_tables"][0]["index_bytes"] == 40
    assert tables[0]["table_name"] == "small"
    assert report["safety"]["orders_authorized"] is False
    assert report["safety"]["mutations_performed"] is False


def test_report_does_not_treat_missing_object_storage_inventory_as_zero_usage():
    report = build_storage_report(
        database_bytes=100,
        quota_bytes=500,
        table_rows=[],
        object_rows=None,
    )
    assert report["object_storage"]["status"] == "NOT_INSPECTED"
    assert report["object_storage"]["object_count"] is None
