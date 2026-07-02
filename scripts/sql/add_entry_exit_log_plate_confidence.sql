-- add_entry_exit_log_plate_confidence.sql
-- Adds entry_exit_log.plate_confidence (winning ANPR read confidence) to an EXISTING
-- database. Idempotent: no-ops if the column already exists.
--
-- Why this exists: container deploys build schema via SQLAlchemy create_all()
-- (app/database.py), which creates missing tables but never alters existing ones,
-- and Alembic does not run inside the container. So a column added to the ORM model
-- (app/models/entry_exit_log.py) / via Alembic migration 5aa7ca8a676a will be absent
-- on already-provisioned databases. Run this against each existing environment.
--
-- Fresh deploys do NOT need this — create_all() already includes the column.
--
-- Usage (sqlcmd):
--   sqlcmd -S <host>,1433 -d damanat_pms -U damanat -P <pwd> -C -N -i scripts/sql/add_entry_exit_log_plate_confidence.sql

USE damanat_pms;
GO

IF NOT EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_NAME = 'entry_exit_log' AND COLUMN_NAME = 'plate_confidence'
)
BEGIN
    ALTER TABLE entry_exit_log ADD plate_confidence INT NULL;
    PRINT 'Column entry_exit_log.plate_confidence added.';
END
ELSE
BEGIN
    PRINT 'Column entry_exit_log.plate_confidence already exists. No change.';
END
GO
