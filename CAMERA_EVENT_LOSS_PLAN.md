# PMS-AI Camera Event Loss — Staged Remediation Plan

**Status:** Stage 4 + Stage 1 code complete; P1 resolved; P2 decided (prod run pending)
**Basis:** prod log `SPECTECH/ai.txt` (2026-09-09 → 2026-09-16, 53,148 lines) + production `damanat_pms` database (3,603 `entry_exit_log` rows, 2026-05-13 → 2026-08-04)
**Date:** 2026-09-18

Every stage is written as **Why it was built that way → What that produced → What it becomes**, with rationale quoted from the code rather than inferred.

---

## P3 is RESOLVED — there were no blackouts

Earlier drafts of this plan treated multi-hour gaps in the log as service outages and built two stages around fixing them. **That was wrong.** Three independent facts settle it.

**1. Operating hours — verified against 3,603 production rows.**

| Weekday | Events/day | | Hour (local) | Events |
|---|---|---|---|---|
| Sunday | 58 | | 06:00 | 92 |
| Monday | 62 | | 08:00 | **540** |
| Tuesday | 61 | | 09:00 | 403 |
| Wednesday | 61 | | 16:00 | **694** |
| Thursday | 54 | | 18:00 | 93 |
| **Friday** | **8** | | 19:00 | 19 |
| **Saturday** | **8** | | 22:00 | 1 |

Open ~06:00-18:00 local, two peaks (arrival 08-09, departure 15-17), effectively closed otherwise. Weekend is **Friday + Saturday** at ~8 events/day — a 7x drop. Exactly as operations described.

**2. PMS-AI emits no log line when idle.** Two things that look like heartbeats are not:
- `_pms_forward_drainer_loop` (`app/main.py:193-204`) logs only inside `except`. A successful idle tick is silent.
- The HikCentral sweep is **event-driven, not a timer**: `note_gate_event(camera_id)` (`entry_exit_service.py:961`) is called from the camera webhook and debounced. No camera events → no `crossRecords/page` polls.

So log silence is indistinguishable from a dead process. My earlier argument — "the Hik poller is a timer, so its silence proves PMS-AI froze" — was simply incorrect.

**3. The container never restarted.** `Starting gunicorn` appears **exactly once** in the whole week's log (09-09 09:37). The process ran continuously through every gap.

### Re-reading the gaps

| Gap | Local time | Verdict |
|---|---|---|
| 16.1h, 09-10 22:01 → 09-11 14:05 UTC | 01:01 → 17:05 **Friday** | **Weekend.** Not an outage. |
| All other multi-hour gaps | start 20:00-02:00 local | **Closed hours.** Normal. |
| Open-hours gaps, working days | 30-80 min | Quiet stretches. Mid-day trough is 96-114 events/hour-of-day vs 694 at peak. |

**Conclusion:** no outage occurred. `GUNICORN_TIMEOUT_SECONDS=90` (`Dockerfile:80`) independently confirms this — a loop blocked past 90s gets the worker killed and restarted, never silenced for hours.

**The one proven wedge:** `WORKER TIMEOUT (pid:9) ... code 134` on 09-12 17:14:22 — a single ~90-second event-loop stall costing a restart and ~19s of downtime, once in a week.

**What this changes:** Problem A is real code with a small, bounded blast radius. **Stages 2 and 3 are demoted to optional hardening.** Problem B — the deletes — is untouched, fully proven, and is the actual cause of your missed cars.

---

## The one problem that matters

| | Problem B — **the delete** |
|---|---|
| Mechanism | 5 code paths return 503 to a camera that never retries |
| Effect | **218 of 1,951 camera events (11.2%) permanently deleted** |
| Proof | 332 camera picture IDs in the log, 332 distinct, **0 repeats** — no event was ever re-delivered |
| Corroboration | All 79 confirmed entries that week came from `source=HIK-RECON` (the HikCentral poller). The live camera path confirmed **zero**. |
| Confidence | **High** |

Secondary, independent, and cheap to fix: **entry ANPR has no camera identity** (Stage 4) — 207 of 207 entry ANPR events arrive unattributed.

Payload sizing for the spool: mean 377 KB, p50 123 KB, max 1.2 MB, ~280 events/day → ~105 MB/day of backlog. A 5 GB volume holds over a month.

---

## Prerequisites

### P1. Spool persistence — **RESOLVED 2026-09-18: config-only, no infra work**

> **Answer:** the cluster has exactly **one** PersistentVolume — `detection_images`, the snapshot store. `/app/pms_forward_spool` is therefore **not** persistent today; it lives in the container layer and is wiped on every pod restart.
>
> **Fix is two env vars, no manifest change, no new PVC:**
> ```
> PMS_FORWARD_SPOOL_DIR=/app/detection_images/pms_forward_spool
> CAMERA_INGEST_SPOOL_DIR=/app/detection_images/camera_ingest_spool
> ```
>
> Checked before recommending it, because reusing another component's volume is usually a bad idea:
> 1. **Created automatically** — both spools call `os.makedirs(..., exist_ok=True)`.
> 2. **Nothing deletes it** — no retention, prune or cleanup job touches `detection_images` anywhere in the codebase; the only `os.remove` is a spool removing its own drained records.
> 3. **Not publicly readable** — `routers/snapshots.py:29` rejects any filename containing a path separator (`safe_name != filename` → 404), so spool records cannot be fetched through the snapshots endpoint.
> 4. **Config-only** — both paths are plain settings, set in the Deployment's env.
>
> **Residual risk: shared capacity.** Spool and snapshots now share a volume, and if it fills, both break. Mitigated in code by `CAMERA_INGEST_SPOOL_MIN_FREE_BYTES` (default 512 MB), below which the webhook refuses to spool rather than starve snapshot writes. The spool is near-empty in steady state — the ~105 MB/day figure is a full-outage worst case, not routine. Still worth checking the PVC's size (`kubectl get pvc -n <ns>`) when convenient; not blocking.
>
> **The service now verifies this itself** — see Stage 1's durability check. No kubectl needed.

#### Original investigation

`PMS_FORWARD_SPOOL_DIR = "./pms_forward_spool"` (`app/config.py:791`) resolves to `/app/pms_forward_spool` (`Dockerfile:7,45` set `WORKDIR /app`). The Dockerfile declares **no** `VOLUME` — it explicitly creates `logs` (line 85) but not the spool.

**However, `docker-compose.yml:58` already mounts a named volume for it:**

```yaml
backend:
  volumes:
    - ./logs:/app/logs
    - ./.env:/app/.env
    - pms_forward_spool:/app/pms_forward_spool
```

So the persistence requirement is understood and handled for compose. Production runs Kubernetes, and **no k8s manifest, Helm chart or kustomization exists in any repo in this tree** — the deployment config lives elsewhere.

**The task is therefore verification, not construction:** confirm the prod Deployment mounts a PVC at `/app/pms_forward_spool`. Given compose does, the chart plausibly does too. Size it at 5 GB (~105 MB/day of backlog, so over a month of headroom).

**Why it matters more under Stage 1:** today the spool holds legacy ANPR forwards — a retry convenience where losing a record loses an image, not a car. Under Stage 1 the spool becomes the **only** copy of a camera event. On ephemeral storage that converts "deleted at 503" into "deleted at restart" — rarer, same outcome.

### P2. SQL Server log backup — **decided and applied locally; PROD STILL PENDING**

> **Decision (2026-09-18):** point-in-time restore is not required → move to `SIMPLE`.
> **Done:** the local analysis copy on `Mohamed\localhost` is now `SIMPLE` / `log_reuse_wait = NOTHING`, log shrunk 648 MB → 136 MB (0.5% used).
> **⚠️ Not done:** production. The local copy is a restore taken 2026-08-22; it is **not** the production server, so nothing above has changed prod. Run `scripts/sql/fix_recovery_model_simple.sql` against the real instance.
> **Do not skip step 4 of that script.** Switching to `SIMPLE` breaks the log chain, so until a fresh FULL backup exists there is no recovery point at all.

#### Root cause (investigated 2026-09-18)

The production database copy was in this state:

```
database       recovery   log_reuse_wait   state
damanat_pms    FULL       LOG_BACKUP       ONLINE
```

In `FULL` recovery the transaction log is truncated **only by a log backup**. With no log backup schedule the log grows without bound until it hits its limit, at which point every write fails with error 9002 — *"The transaction log for database 'damanat_pms' is full due to 'LOG_BACKUP'"*. The holdup LSN `(400:14599:1)` staying frozen for 26 hours (09-14 12:55 → 09-15 15:10) is precisely what "nothing truncated the log" looks like.

Corroborating evidence:

| Check | Finding |
|---|---|
| Log vs data file | **648 MB log vs 72 MB data — 9x.** A healthy log is a fraction of the data. |
| Log backups ever taken | **Exactly one**, 2026-08-22 10:18 — and `create_date` here is 10:21, so this was almost certainly the backup used to **take this copy**, not a scheduled job |
| Full backups | 2026-04-13, 2026-04-14 (×2), 2026-08-04 — sporadic and manual |
| Autogrowth | 64 MB increments, max 2 TB → grows until the disk fills |

*Caveat on the backup rows: `msdb` history belongs to this local instance, so it says little about prod's own backup schedule — an earlier draft read the lone log backup as evidence of prior firefighting, and the 3-minute gap to `create_date` makes "this is how the copy was made" the better reading. The load-bearing evidence is not the history: it is the **recovery model and `log_reuse_wait`, which travel with the database**, plus the production error message, which independently proves prod was in the same state.*

**The fix is a decision, not a command — is point-in-time restore required?**

- **No** → set `SIMPLE` recovery. The log self-truncates at each checkpoint and the problem is gone permanently. A nightly full backup is still needed.
- **Yes** → keep `FULL` and schedule a log backup (15 minutes is typical). Your worst-case data loss equals the backup interval.

For a parking system, `SIMPLE` + a nightly full is the usual answer unless a compliance rule says otherwise — but it is a data-loss-window decision, so it belongs to whoever owns that risk.

**Two gotchas:**
1. If production SQL Server is **Express** (`docker-compose.yml:14` uses `MSSQL_PID=Express`), **SQL Agent does not exist**, so "schedule a job" will not work — it needs an external scheduler (a k8s CronJob, or Windows Task Scheduler). The local copy is Developer Edition, so prod's edition is unconfirmed.
2. After switching recovery model, the 648 MB log needs one `DBCC SHRINKFILE` to reclaim the space. Once only, and only after the model is corrected — never on a schedule.

**~~P3. Determine the blackout cause.~~ RESOLVED — see above. No blackouts occurred.**

---

## Stage 4 — Fix entry ANPR identity — ✅ CODE COMPLETE (2026-09-18)

> **Shipped:** `app/services/event_parser.py` — new `resolve_camera_identity()` shared by the XML and JSON paths, tolerant serial matching, and a diagnosable `UNKNOWN` placeholder.
> **Tests:** `tests/test_camera_identity_resolution.py`, 23 new tests, all passing. Full suite **737 passed / 1 failed**, the one failure being the pre-existing Windows-only `_fsync_directory` test (`core_backend_client.py:199` returns early when `os.name == "nt"`).
> **Remaining (ops, ~10 min):** deploy, wait for one entry car, read the new `[Identity]` log line, and set `CAM_ENTRY_SERIAL` to the exact value the camera reports. See "How to finish" below.

### What the fix actually does

**1. Identity resolution is now shared and tolerant.** Both `_parse_xml_event` and `_parse_json_event` call one `resolve_camera_identity(device_serial, declared_ip, client_ip)`. Serial matching tries, in order: exact → normalized (case/punctuation/whitespace insensitive) → length- and uniqueness-guarded containment. This alone fixes the case where `CAM_ENTRY_SERIAL="DS-TCG406-E 20250221AIFW6259223"` (model + space + serial) does not literally equal what the camera reports.

**2. The model prefix can never claim a camera.** For a multi-token configured serial only the *longest* token is indexed standalone. Both gate cameras are `DS-TCG406-E`; without this rule a camera reporting only its model could be resolved as either. A test pins this.

**3. `UNKNOWN` now names the value that failed.** The old fallback was `f"UNKNOWN-{camera_ip}"` — the NAT gateway every camera shares — which is why 207/207 events read `UNKNOWN-10.1.20.60` for a week without the log ever saying why. It is now `f"UNKNOWN-{declared_ip or client_ip}"`, plus a one-per-signature warning naming the reported `deviceSerial` and `ipAddress`:

```
[Identity] unresolved camera via gateway 10.1.20.60 — body declared
deviceSerial='...' ipAddress='...'. Neither is in CAMERA_SERIAL_MAP (2 entries)
or CAMERA_IP_MAP (28 entries).
```

Rate-limited per distinct `(serial, ip)` pair, so a busy gate logs once, not once per car. `startswith("UNKNOWN")` is unchanged, so the existing plate rescue in `event_dispatcher.py:101` still works untouched.

**4. A loose match warns that the config should be corrected**, once per camera, so tolerant matching never silently hides a stale `.env`.

### How to finish (the ops step)

Deploy, then watch for **one** entry vehicle:
- **If the camera reports a serial** → the new log names it. Set `CAM_ENTRY_SERIAL` to that exact string. The tolerant matcher has very likely already resolved it, in which case the `matched by normalized serial comparison` warning tells you so and the fix is only cosmetic.
- **If the body is silent** (`deviceSerial='<absent>' ipAddress='<absent>'`) → the payload genuinely carries no identity, and the fix is on the camera side: enable device info in its HTTP push, or add an `ENTRY_V2_CAMERA_ALIASES` entry. The log now distinguishes these two cases, which it previously could not.

### Original analysis

**Why it was built that way**
`app/services/event_parser.py:928-931` resolves identity as `CAMERA_SERIAL_MAP[deviceSerial]` → `CAMERA_IP_MAP[body ipAddress]` → `CAMERA_IP_MAP[client ip]` → `UNKNOWN-<ip>`. The comment states the intent: *"Prefer the `<ipAddress>` from XML body over the HTTP request's client IP. This allows manual testing from localhost while still correctly identifying the camera."* The design assumes each camera is identifiable by serial or by its own IP.

**What that produced**
That assumption does not hold here. Every camera HTTP event arrives from the relay/NVR at **10.1.20.60**, never from the camera's own address (VA pulls real cameras over RTSP at 10.1.13.x). Line-detection events carry a mapped `<ipAddress>`/serial and resolve fine. **ANPR events carry neither.**

- **207 of 207** entry ANPR events arrived as `camera=UNKNOWN-10.1.20.60`.
- `type=ANPR | camera=CAM-ENTRY` appears **zero** times in seven days.
- 194 are saved by a fallback: `[dispatch] Rescued ANPR UNKNOWN-10.1.20.60 -> CAM-ENTRY via recent vehicleMatchResult for plate=X`.
- That fallback needs a recent vehicleMatchResult carrying **the same plate** — so **it fails exactly when the plate is what failed to read**. The 13 events with `plate=None` were rejected with a 503 and lost.
- CAM-EXIT ANPR resolves normally (164 events), confirming this is specific to the entry ANPR camera.

**What it becomes**
- Capture the entry ANPR camera's `deviceSerial` from one live payload; populate `CAMERA_SERIAL_MAP`. Alternatively populate `ENTRY_V2_CAMERA_ALIASES`, which exists for exactly this and is **empty in prod today**.
- Identity resolves on **camera**, not on plate. The plate rescue stays as a fallback, demoted from primary.
- Any unmapped serial logged once per serial, so a new camera surfaces immediately.

### Scenarios
| # | Scenario | Expected |
|---|---|---|
| 1 | ANPR with mapped serial | resolves to `CAM-ENTRY` directly; no rescue needed |
| 2 | ANPR with `plate=None` | **still resolves** — this is the 13 currently rejected |
| 3 | ANPR from an unmapped camera | logged once by serial; degrades to today's behaviour |
| 4 | Exit ANPR | unchanged (already correct) |

### Tests
- Extend `tests/test_event_parser.py` — scenarios 1-4 against real captured payload fixtures.
- `tests/test_entry_cam03_rescue.py` — the plate rescue still works when the serial is absent.
- Assert `type=ANPR | camera=UNKNOWN-*` no longer occurs for the entry camera.

**Prod exit criteria:** `camera=UNKNOWN-10.1.20.60` → zero; `Rejecting unresolved ANPR` → zero.

**Effort:** ~0.5-1 day plus capturing one payload. **Risk: low.** **Best effort-to-value ratio in the plan.**

---

## Stage 0 — Make silence interpretable

**Why it was built that way**
No deliberate decision — observability was simply never added. Nothing logs on an idle tick.

**What that produced**
Exactly the confusion documented at the top of this file: a quiet Friday is byte-for-byte indistinguishable from a dead service. Two drafts of this plan proposed a week of work against a phantom outage. That is the real cost of missing observability.

**What it becomes**
- **A fixed-interval heartbeat line** (e.g. every 60s) with uptime and events-processed-since-last. Makes any future log gap unambiguous. *This is the single most valuable item in this stage.*
- An asyncio loop-lag watchdog: measure drift every 1s; WARN above 1s, ERROR above 10s. Confirms or bounds Problem A with data instead of inference.
- Counters on `/api/health`: `camera_events_received`, `camera_events_503` by reason, `spool_depth`, `spool_oldest_age_seconds`.

### Scenarios
| Scenario | Expected |
|---|---|
| Service idle overnight | heartbeat continues — silence becomes impossible |
| Service stopped | heartbeat absent — unambiguous |
| Loop blocked 5s | watchdog logs lag >= 5s |

### Tests
- `tests/test_loop_watchdog.py` — synthetic block raises the metric; idle loop does not.
- Heartbeat emits on schedule with no traffic.
- `tests/test_health.py` extension — new counters present, typed, monotonic.
- The watchdog never raises and never affects request handling (per "optional integrations must degrade, not raise").

**Flag/rollback:** `LOOP_WATCHDOG_ENABLED`, default on. Additive; rollback is a flag flip.

**Effort:** ~1 day. **Risk: very low.**

---

## Stage 1 — Never delete a camera event — ✅ CODE COMPLETE (2026-09-18)

> **Shipped:**
> - `app/services/camera_ingest_spool.py` — durable spool (temp → `fsync` → atomic rename → dir `fsync`), FIFO ordering by embedded receive time, capacity + free-space guards, quarantine, and a boot-time durability self-check.
> - `app/routers/events.py` — handler split into `process_camera_event()` returning a `CameraEventOutcome`, shared by the live webhook and the drainer. All five retryable exits now spool and answer **200**.
> - `app/main.py` — `_camera_ingest_drainer_loop()`, wired into startup and shutdown.
> - `app/routers/health.py` — `camera_ingest_spool` block (depth, bytes, backlog age, durability). Reported only; it never changes `status`.
> - `.env.example` — all six settings documented.
>
> **Tests:** `tests/test_camera_ingest_spool.py`, 24 new tests. Full suite **761 passed / 1 failed** — the one failure is the pre-existing Windows-only `_fsync_directory` test (`core_backend_client.py:199` returns early when `os.name == "nt"`).
>
> **Default is OFF.** `CAMERA_INGEST_SPOOL_ENABLED=false` reproduces today's exact behaviour byte for byte, including the 503 and its `Retry-After: 1`.

### Two things worth knowing before enabling

**The 503 fallback is retained on purpose.** If the spool cannot store the event — volume full, free-space floor breached, path unwritable — the webhook returns the old 503 and logs it as a loss. That is worse, but it is honest, and it never crash-loops: the spool degrades, it never raises.

**Ordering is enforced, and a blocked drain blocks the queue deliberately.** Records replay oldest-first by the `received_at` embedded in each record, not by mtime or directory order. When a record is still retryable the drainer stops that pass rather than skipping ahead — draining past it would reorder an entry burst (CAM-23, CAM-03 and the ANPR read for one car arrive seconds apart, and the correlation logic depends on their order).

### A bug the tests caught

The free-space guard ran *before* the spool directory was created, and `shutil.disk_usage` raises on a path that does not exist — so the floor protecting the shared snapshot volume silently never armed on the first event. `_free_bytes` now walks up to the nearest existing ancestor, which is the same volume and the same answer. Found by `test_free_space_floor_protects_the_shared_volume`, not by review.

### How to roll it out

1. Set `CAMERA_INGEST_SPOOL_DIR=/app/detection_images/camera_ingest_spool` (inside the one PV — see P1).
2. Deploy with `CAMERA_INGEST_SPOOL_ENABLED=false`. Harmless; it only starts the durability check on the next step.
3. Set `CAMERA_INGEST_SPOOL_ENABLED=true` and restart. Read the boot line:
   - `[IngestSpool] storage is DURABLE — marker from previous boot ... survived a restart` → done.
   - `no marker from a previous boot` → **restart once more.** A first-ever boot and ephemeral storage look identical until the second boot. If it still says this after a restart, the path is not on the PV.
4. Watch `/api/v1/health` → `camera_ingest_spool.depth`. It should sit at 0 and spike only during downstream trouble.
5. Success condition: retryable `camera-facing 503` lines stop appearing, and `OPENED missed entry` falls from its current 6-14/day.

### Original analysis

**Why it was built that way**
`app/routers/events.py:159-161` states it plainly:

> *"Authoritative V2 must remain the camera-facing retry boundary and run before any legacy transaction."*

The reasoning is sound in isolation: if VA is the only writer of entry state, PMS-AI must not commit a legacy entry VA never confirmed. Returning 503 with `Retry-After` pushes retry to the edge and keeps the two stores consistent. That is a correct instinct about consistency.

**What that produced**
It assumes the camera retries. **Hikvision HTTP listening-host push is fire-and-forget — it ignores `Retry-After` and never re-POSTs.** Verified: **332 camera picture IDs, 332 distinct, zero repeats.**

The retry boundary has no retrier behind it. The 503 does not defer the event — it **deletes** it:

- **218 of 1,951** camera events (11.2%) discarded in one week.
- Five return sites: `events.py:169, 180, 251, 261, 274`.
- Causes: 85 VA config-guard, 32 VA ingress-capacity, 10 confirmation-delivery, 18 VA timeout, 13 unresolved ANPR, ~84 database errors.
- All 79 confirmed entries came from `HIK-RECON`; the live path confirmed zero.

**What it becomes**
The consistency goal is preserved; only the retrier changes. PMS-AI becomes the retry boundary instead of the camera.

- New `app/services/camera_ingest_spool.py`, reusing the durable-write pattern already proven in `app/utils/core_backend_client.py:412-455`: temp file → `fsync` → atomic `os.replace` → directory `fsync`. Record = raw body + `camera_ip` + `content_type` + receive timestamp + attempt count.
- All five 503 sites: spool the raw body, return **200**.
- A drainer replays records through the normal handler path, FIFO by receive timestamp, with backoff and an attempt cap.
- `CameraPayloadRejected` records are quarantined, not retried forever (`_quarantine_spool_file` already exists).

Nothing commits that VA has not confirmed — the event waits on disk instead of being thrown away.

### Scenarios
| # | Scenario | Expected |
|---|---|---|
| 1 | VA 503 `ingress_capacity_exceeded` | 200 to camera; spooled; drained; entry opens |
| 2 | VA connect/pool timeout | same as 1 |
| 3 | `EntryStateLockUnavailable` | 200; spooled; replay acquires lock later |
| 4 | DB transaction log full (the 09-14 case) | 200; spooled 26h; **all events replay on recovery** |
| 5 | Malformed payload | 200 `rejected`; **not** spooled — do not poison the spool |
| 6 | Spool volume full | log ERROR, fall back to today's 503; never crash |
| 7 | Pod restart, 40 records pending | all 40 replay; none lost; none duplicated |
| 8 | Same event replayed twice | dedup holds; no double entry |
| 9 | Burst CAM-23 + CAM-03 + ANPR, VA at capacity | all three spooled, replayed **in order**, one entry produced |
| 10 | Overnight closure with a stale record | replay does not invent an entry outside operating hours |

### Tests
- `tests/test_camera_ingest_spool.py` — scenarios 1-8. `conftest.py` already redirects `PMS_FORWARD_SPOOL_DIR` to `tmp_path`, so isolation comes free.
- Crash-safety: kill mid-write; no `.tmp` is ever read as a record.
- FIFO: shuffle file mtimes; drain order follows the embedded receive timestamp, not directory order.
- Extend `tests/test_entry_v2_camera_pipeline.py` with scenario 9 — most likely to regress silently.
- Scenario 10 matters because of the operating hours above: a record spooled at 17:55 and replayed at 18:30 must still carry its **original** event time.
- Regression: full suite (715 tests) green.

**Flag/rollback:** `CAMERA_INGEST_SPOOL_ENABLED`, default **off**. Off = today's exact behaviour.

**Prod exit criteria:** retryable `camera_events_503` → zero. `spool_depth` returns to 0 between bursts. `OPENED missed entry` falls from its current 6-14/day.

**Effort:** ~2-3 days. **Risk: low-medium** — concentrated in replay ordering and dedup (scenarios 8, 9, 10).

---

## Stage 5 — Tune capacity

**Why it was built that way**
`Damanat-PMS-VideoAnalytics/src/entry/settings.py:90` sets `max_concurrent_ingest_requests: int = 2`. VA is pinned single-process in authoritative mode, so the cap protects one process from concurrent image decode and ReID work. A small cap is a reasonable default for a single-process CPU-bound service.

**What that produced**
The cap sits **below the floor for normal traffic**. One car entering generates CAM-23 + CAM-03 (1-3s apart) plus an ANPR/entry-attempts POST — three overlapping requests against a cap of two. Measured: **28 of 32** capacity-exceeded 503s had ≥2 camera events parsed in the preceding 5 seconds. Combined with Stage 1's finding that a 503 deletes the event, a routine single arrival could delete itself.

Separately, `duplicate` is not terminal in VA's confirmation retry: **8 cars produced 11,695 confirmation POSTs on 09-10** (one decision retried 2,557 times, ~66/min for hours). That storm consumed VA's single process and held the 2-slot gate full — which is why 09-10 carries 63 of the week's 97 entry-crossing 503s.

**What it becomes**
- `ENTRY_V2_MAX_CONCURRENT_INGEST_REQUESTS` raised to 8-16, watching VA CPU. With Stage 1 in place this trades 503s for latency, which the spool absorbs safely.
- `duplicate` becomes terminal — VA stops re-delivering an acknowledged decision.

**Do not set `VA_SINGLE_PROCESS=1`.** The settings.py docstring flags it as an engine switch that forces `VA_INFER=async` and collapses a deliberately multi-group deployment onto one queue.

**Effort:** ~1-2 days. **Risk: low** once Stage 1 is in.

---

## Stages 2 & 3 — OPTIONAL hardening (no longer recommended near-term)

Both were written to fix multi-hour blackouts. **Those blackouts did not happen.** They are retained here for the record, not proposed.

**What they would have addressed:** 45 sync pyodbc calls on one event loop (`entry_exit_service.py` 24, `occupancy_service.py` 12, `vehicle_service.py` 5, `alert_service.py` 3, `event_dispatcher.py` 1), producing request latency and one ~90s wedge on 09-12.

**Why they are not worth it now:**
- Gunicorn's 90s timeout already bounds the damage to a restart.
- The measured cost was **one restart in a week**, ~19s of downtime.
- `-w 1` is load-bearing. `Dockerfile:93-100`: *"Single worker is required: ANPR ↔ CAM-03 entry-confirmation handshake ... stores transient state in module-level dicts ... With multiple workers the ANPR event and the CAM-03 event can land in different processes that don't share that state, causing real entries to be dropped as 'ghosts'."* **`-w 1` was itself the fix for a previous entry-loss bug.**
- Moving processing to a thread means converting `_bursts_lock` (`entry_exit_service.py:81`, an `asyncio.Lock` with 7 use sites) and `_shadow_queue` (`entry_v2_forwarder.py:543`, an `asyncio.Queue`) to threading primitives — touching exactly the burst-correlation logic `-w 1` protects. 4-5 days at medium-high risk to fix ~19s/week of downtime.

**Revisit if** Stage 0's watchdog shows loop-lag ERROR events recurring, or `WORKER TIMEOUT` becomes frequent rather than a one-off.

**Documentation debt worth fixing regardless:** the `Dockerfile:93-100` comment names `_pending_entries` and `_cam03_pre_confirmations`, which **no longer exist**. The state today is `_entry_bursts`, `_pending_crossings`, `_recent_entries`, `_last_reconcile_at`, `_sweep_in_flight`. The constraint is real; its documentation is not. Fix the comment.

---

## What this plan deliberately does not do

**No Redis.** Not present anywhere in the codebase. A new stateful service on the hottest path; does not fix the loop unless the client is async; loses the queue on restart without AOF.

**No DB events table.** SQL Server is both the component that blocks and the component that failed for 26 hours. A queue inside `damanat_pms` would be unavailable during exactly the outage it exists to survive.

**No async SQLAlchemy migration.** 45 call sites across the most sensitive transaction logic, to fix a bounded 90s wedge.

**No removal of `-w 1`.** It is load-bearing and guards a known entry-loss bug.

---

## Risk register

| Risk | Stage | Mitigation |
|---|---|---|
| Replay reorders a burst → wrong plate | 1 | FIFO on embedded receive timestamp; scenario 9 |
| Replay stamps entries with replay time | 1 | Scenario 10; **silently corrupts durations if missed** |
| Replay creates an entry outside operating hours | 1 | Scenario 10, informed by the 06:00-18:00 profile |
| Spool fills the volume | 1 | Bounded depth + quarantine + fall back to 503; scenario 6 |
| Double entry on replay | 1 | Dedup test, scenario 8 |
| Raising the VA cap starves VA CPU | 5 | Raise incrementally, watch CPU; spool absorbs latency |

---

## Test-suite notes

- Baseline: **715 tests, collecting clean**.
- **Known trap:** local `.env:189` sets `DISABLED_ALERT_TYPES=silent_entry`, which makes a healthy suite look red. Use a clean env before judging results.
- `tests/conftest.py` already redirects `PMS_FORWARD_SPOOL_DIR` to `tmp_path` and disables outbound pushes — new spool tests inherit correct isolation free.
- The local `damanat_pms` copy holds 2026-05-13 → 2026-08-04 (3,603 `entry_exit_log` rows). It does **not** cover the 09-09..16 log window, so it is useful for traffic-profile questions, not for incident forensics.

---

## Revised order

| # | Item | Effort | Status |
|---|---|---|---|
| 1 | **Stage 4** | ~1 day | ✅ **code complete** — needs deploy + one entry car to confirm the serial |
| 2 | **P1** (spool on the PV) | ~5 min | ✅ **resolved** — config-only, two env vars, no infra work |
| 2b | **P2** (recovery model) | ~15 min | 🟡 decided (SIMPLE) + applied to the local copy; **prod run pending** — `scripts/sql/fix_recovery_model_simple.sql` |
| 3 | **Stage 1** | 2-3 days | ✅ **code complete** — 24 tests; deploy with the flag OFF, then enable |
| 4 | **Stage 0** | 1 day | ⬜ heartbeat so the "blackout" confusion cannot recur |
| 5 | **Stage 5** | 1-2 days | ⬜ cheap once Stage 1 is in |
| — | ~~Stages 2 & 3~~ | — | ⛔ deferred; no longer justified |

**Roughly one week of work**, down from three — and it targets everything that is actually proven.

---

## Open decisions

1. Who owns **P1** (spool PVC) and **P2** (SQL log backup)? Both are infrastructure; P1 blocks Stage 1 and P2 undermines it.
2. After Stage 4 is deployed and the real serial is known, go straight at **Stage 1**, or do **Stage 0** first so Stage 1's effect is measurable from day one?

---

## Test-run note

The suite cannot be judged with the repo `.env` loaded. Two alert tests fail solely because `.env:189` sets `DISABLED_ALERT_TYPES=silent_entry`; they pass with it cleared:

```bash
DISABLED_ALERT_TYPES="" python -m pytest tests/ -q --basetemp=<writable-dir>
```

`--basetemp` is required on this machine — pytest's default temp location raises `PermissionError` during collection, which makes every test in a file look like an error rather than a failure.
