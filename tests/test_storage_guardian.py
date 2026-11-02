from app.engines.storage_guardian import assess_capacity, build_storage_report, collect_storage_report


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


class _Column:
    def __init__(self, name):
        self.name = name


class _Cursor:
    def __init__(self, rows, columns=()):
        self._rows = rows
        self.description = [_Column(name) for name in columns]

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _ReadOnlyFakeConnection:
    def __init__(self, fail_object_inventory=False):
        self.queries = []
        self.fail_object_inventory = fail_object_inventory

    def execute(self, query):
        self.queries.append(query)
        normalized = query.lower()
        if "pg_database_size" in normalized:
            return _Cursor([(200,)])
        if "pg_total_relation_size" in normalized:
            return _Cursor(
                [("paper_ledger", "market_candles", 1000, 150, 60, 90)],
                ("schema_name", "table_name", "estimated_rows", "total_bytes", "heap_bytes", "index_bytes"),
            )
        if "storage.buckets" in normalized:
            if self.fail_object_inventory:
                raise PermissionError("storage catalog is not readable")
            return _Cursor(
                [("bucket-1", "private-backups", False, 10485760, 2, 4096)],
                ("bucket_id", "bucket_name", "is_public", "file_size_limit", "object_count", "object_bytes"),
            )
        raise AssertionError("unexpected SQL statement")


def test_database_inventory_uses_select_only_and_reports_object_usage():
    connection = _ReadOnlyFakeConnection()
    report = collect_storage_report(connection, quota_bytes=500)
    assert all(query.lstrip().upper().startswith("SELECT") for query in connection.queries)
    assert report["largest_tables"][0]["table_name"] == "market_candles"
    assert report["object_storage"]["object_count"] == 2
    assert report["object_storage"]["object_bytes"] == 4096
    assert report["safety"]["mutations_performed"] is False


def test_unavailable_storage_catalog_is_not_reported_as_empty():
    connection = _ReadOnlyFakeConnection(fail_object_inventory=True)
    report = collect_storage_report(connection, quota_bytes=500)
    assert report["object_storage"]["status"] == "NOT_INSPECTED"
    assert report["object_storage"]["object_count"] is None
    assert all(query.lstrip().upper().startswith("SELECT") for query in connection.queries)
