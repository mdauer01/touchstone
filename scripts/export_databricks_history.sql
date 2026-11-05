-- touchstone pilot: export executed statements from Databricks.
--
-- Run in a Databricks SQL editor (system tables are admin-readable by
-- default), download the result as CSV, and save it as the pilot's
-- history file (e.g. pilot/history.csv). touchstone reads these column names
-- as-is; extra columns are listed in the report, never silently used.
--
-- BEFORE RUNNING: confirm the column names against your workspace —
--     DESCRIBE TABLE system.query.history;
-- They follow the published system-table reference; a renamed column would
-- otherwise arrive as "no attribution" rather than as an error.
--
-- SESSION CONTEXT: the published schema carries no per-query default
-- catalog/schema. Queries that name tables in full (catalog.schema.table)
-- resolve on their own; queries using short names are UNVERIFIABLE unless
-- pilot.yml asserts one execution_context for every exported execution.
-- Only assert it when it is true — e.g. every dashboard in the window runs
-- against one catalog and schema.
--
-- EVERY statement type is exported, with its session: a USE, SET or
-- CREATE TEMP VIEW earlier in a session changes what that session's short
-- names read, and touchstone makes the affected queries unverifiable rather
-- than guess. Filtering to SELECT would hide exactly those statements.
--
-- TIME: start_time is compared with the definition windows in pilot.yml,
-- which are UTC. Exporting in UTC keeps that comparison honest.

SET TIME ZONE 'UTC';

SELECT
  statement_id,
  statement_text,
  start_time,
  executed_by,
  client_application,
  session_id,
  statement_type,
  query_source.dashboard_id          AS dashboard_id,
  query_source.legacy_dashboard_id   AS legacy_dashboard_id,
  query_source.genie_space_id        AS genie_space_id,
  query_source.job_info.job_id       AS job_id,
  query_source.alert_id              AS alert_id,
  query_source.notebook_id           AS notebook_id,
  query_source.sql_query_id          AS sql_query_id,
  query_source.pipeline_info.pipeline_id AS pipeline_id
FROM system.query.history
WHERE start_time >= TIMESTAMP '2026-10-01 00:00:00'   -- the pilot window.from
  AND start_time <  TIMESTAMP '2026-10-15 00:00:00'   -- the pilot window.to
  AND execution_status = 'FINISHED'
ORDER BY start_time;
