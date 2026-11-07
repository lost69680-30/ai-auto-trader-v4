CREATE POLICY solo_backup_reader_select
ON paper_ledger.market_intelligence_reports
FOR SELECT
TO solo_backup_reader
USING (true);
