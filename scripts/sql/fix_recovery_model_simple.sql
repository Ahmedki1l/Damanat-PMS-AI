/* ============================================================================
   P2 — stop the transaction log from filling and killing every write.
   Run against the PRODUCTION SQL Server hosting damanat_pms.

   WHY
   ---
   damanat_pms runs in FULL recovery with no log backup schedule. In FULL
   recovery the log is truncated ONLY by a log backup, so it grows without
   bound until it hits its limit and every write fails with:

       Msg 9002 — The transaction log for database 'damanat_pms' is full
       due to 'LOG_BACKUP'

   That is the 26-hour production write outage on 2026-09-14 12:55 →
   2026-09-15 15:10 (holdup LSN (400:14599:1) frozen throughout), which
   produced ~84 camera-facing 503s and 96 EntryStateLockUnavailable errors.
   The lock failures are downstream: transactions that cannot commit never
   release sp_getapplock.

   DECISION TAKEN (2026-09-18): point-in-time restore is NOT required, so we
   move to SIMPLE. The log then self-truncates at each checkpoint and this
   failure mode is gone permanently.

   WHAT YOU GIVE UP
   ----------------
   Point-in-time recovery. After this change your recovery point is your most
   recent FULL backup, so step 4 is not optional — it is now the ONLY thing
   standing between you and data loss.
   ============================================================================ */

/* -- 1. Confirm the starting state. Expect: FULL / LOG_BACKUP ------------- */
SELECT  name,
        recovery_model_desc,
        log_reuse_wait_desc,
        state_desc
FROM    sys.databases
WHERE   name = 'damanat_pms';

/* Log vs data size — a 9x log is the signature of a log never truncated. */
USE damanat_pms;
GO
SELECT  name,
        type_desc,
        size / 128.0 AS size_mb
FROM    sys.database_files;
GO


/* -- 2. Switch to SIMPLE -------------------------------------------------- */
/* Online and near-instant. Does not require exclusive access and does not
   interrupt PMS-AI. It DOES break the existing log backup chain, which is
   expected and acceptable given the decision above. */
ALTER DATABASE damanat_pms SET RECOVERY SIMPLE;
GO


/* -- 3. Truncate and reclaim the space ------------------------------------ */
USE damanat_pms;
GO
CHECKPOINT;
GO

/* Verify the log is now reusable before shrinking. Expect: NOTHING.
   If this still says LOG_BACKUP, STOP — step 2 did not take effect.
   If it says ACTIVE_TRANSACTION, wait for the open transaction to finish. */
SELECT  name, recovery_model_desc, log_reuse_wait_desc
FROM    sys.databases
WHERE   name = 'damanat_pms';
GO

/* Shrink to a working size. 128 MB suits this workload (data file ~72 MB,
   ~280 camera events/day). Do NOT shrink to 0 — the log would just autogrow
   again and fragment.

   This is the one legitimate use of SHRINKFILE. Run it ONCE, here, after the
   recovery model is corrected. Never put a shrink on a schedule. */
DBCC SHRINKFILE (damanat_pms_log, 128);
GO

/* Confirm. Expect the log at ~128-136 MB with a low used percentage. */
SELECT  name, type_desc, size / 128.0 AS size_mb
FROM    sys.database_files;
GO
SELECT  total_log_size_in_bytes / 1048576.0 AS log_mb,
        used_log_space_in_percent          AS pct_used
FROM    sys.dm_db_log_space_usage;
GO


/* -- 4. Take a FULL backup NOW, then schedule one nightly ---------------- */
/* Step 2 broke the log chain, so every prior log backup is now unusable for
   recovery. Until a fresh full backup exists there is no recovery point at
   all. Adjust the path to the real backup target. */
BACKUP DATABASE damanat_pms
TO DISK = N'<BACKUP_PATH>\damanat_pms_post_simple_switch.bak'
WITH INIT, COMPRESSION, CHECKSUM, STATS = 10;
GO


/* ============================================================================
   5. SCHEDULING THE NIGHTLY FULL — read this before assuming SQL Agent
   ============================================================================
   If production SQL Server is EXPRESS edition, SQL Server Agent DOES NOT
   EXIST and creating a job will fail. The docker-compose used for local work
   sets MSSQL_PID=Express, so this is a live possibility. Check first:

       SELECT CAST(SERVERPROPERTY('Edition') AS nvarchar(100));

   Standard / Developer / Enterprise  → use a SQL Agent job.
   Express                            → use an external scheduler instead:
                                        a Kubernetes CronJob running sqlcmd,
                                        or Windows Task Scheduler.

   Example for an external scheduler (one line, runs nightly):

       sqlcmd -S <server> -Q "BACKUP DATABASE damanat_pms TO DISK=N'<path>\damanat_pms_$(date +%%F).bak' WITH INIT, COMPRESSION, CHECKSUM"

   ============================================================================
   6. VERIFY IT STAYED FIXED — check a week later
   ============================================================================
   Expect SIMPLE / NOTHING, and a log that is NOT larger than the data file. */

SELECT  d.name,
        d.recovery_model_desc,
        d.log_reuse_wait_desc,
        (SELECT size / 128.0 FROM sys.master_files
          WHERE database_id = d.database_id AND type_desc = 'LOG')  AS log_mb,
        (SELECT size / 128.0 FROM sys.master_files
          WHERE database_id = d.database_id AND type_desc = 'ROWS') AS data_mb
FROM    sys.databases d
WHERE   d.name = 'damanat_pms';
GO
