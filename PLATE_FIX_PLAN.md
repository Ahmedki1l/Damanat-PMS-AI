# Plate-Number Remediation Plan — PMS-AI (System 1)

> Companion to `Plate_Number_Wiring_Review.pdf`. This repo owns the **canonical
> plate identity**: it ingests ANPR, creates `vehicles` rows, and opens/closes
> `parking_sessions`. Most of the root-cause fixes start here, so **do this repo
> first**.

## Cross-project sequencing (read once)

The three repos share one database and are joined only by the plate string. Fix
in this order so nothing breaks mid-flight:

1. **PMS-AI (this file)** — define canonical plate normalization, return
   `vehicle_id` from bind-slot, and let a bind open a session. *(unblocks the
   others)*
2. **Video Analytics** — adopt the *identical* normalization, fix silent
   bind/unbind failures, stop double-writing the plate.
3. **API Gateway** — align the timezone convention, pick one slot-identity
   column, migrate joins to `vehicle_id`, regenerate the client.

Two decisions must be locked before any code (owner: whoever leads the
integration):

- **D1 — Plate normalization spec.** Exact, byte-for-byte algorithm (case,
  whitespace, separators, Arabic↔Latin digits, allowed charset). PMS-AI hosts
  the reference implementation and the shared test-vector file; VA copies it
  verbatim.
- **D2 — Timezone convention (DECIDED).** The facility is in **KSA (Riyadh) =
  UTC+3, no DST**. All timestamp columns store **naive facility-local (UTC+3
  wall clock)** — which is what this repo already writes. Every service runs
  with `FACILITY_TIMEZONE_OFFSET_HOURS=3.0`. The Gateway aligns to this.

---

## Step 1 — Canonical plate normalization (fixes I-1, core)

**Problem:** VA's OCR plate and this repo's ANPR plate are compared as raw
strings. Any difference in spacing/case/digits silently breaks the link.

**Do:**

- [ ] Add `app/utils/plate.py` with a single `normalize_plate(raw: str) -> str`
      implementing **D1**. Keep it dependency-free and pure.
- [ ] Add `tests/test_plate_normalize.py` with a shared **test-vector table**
      (input → expected). This exact table is the contract VA must also pass.
- [ ] Apply `normalize_plate()` at every plate entry point:
  - [ ] `services/event_parser.py` — normalize `plate_number` the moment it is
        parsed from the ANPR payload, so everything downstream is canonical.
  - [ ] `services/vehicle_service.py` — `lookup_vehicle`, `register_vehicle`,
        `ensure_unregistered_vehicle` (normalize before the `plate_number ==`
        query and before insert; lines 36-40, 64-92, 95-122).
  - [ ] `services/parking_session_service.py` — `get_latest_open_session`,
        `open_session`, `bind_slot`, `unbind_slot`, `close_session` (normalize
        the incoming `plate_number` argument).
- [ ] One-time backfill: normalize existing `vehicles.plate_number`,
      `entry_exit_log.plate_number`, `parking_sessions.plate_number` and dedupe
      any `vehicles` rows that collapse to the same normalized plate (keep the
      registered row; repoint FKs).

**Acceptance:**

- `pytest tests/test_plate_normalize.py` passes on the shared vector table.
- `SELECT plate_number FROM vehicles` contains no value that differs from its
  own `normalize_plate()` output.

---

## Step 2 — Return `vehicle_id` from bind/unbind (fixes I-1, enables Gateway)

**Problem:** VA never learns the numeric id, so every downstream join stays
string-based.

**Do:**

- [ ] `schemas/parking_session.py` — add `vehicle_id: int | None` to
      `ParkingSessionActionResponse`.
- [ ] `services/parking_session_service.py` — `bind_slot` / `unbind_slot`
      already resolve the vehicle via `_resolve_vehicle`; return it so the
      router can include `vehicle_id`.
- [ ] `routers/parking_sessions_internal.py` — populate `vehicle_id` in both
      responses (lines 40-49, 72-81).

**Acceptance:** a successful `POST /api/v1/internal/parking-sessions/bind-slot`
returns a non-null `vehicle_id` for a plate that exists in `vehicles`.

---

## Step 3 — Let a bind open a session instead of 404 (fixes I-3)

**Problem:** `bind_slot` raises `LookupError → 404` when no open session exists
(e.g. ANPR missed the entry / burst was dropped at
`entry_exit_service.py:311-316`). VA physically confirms the car is parked, so a
404 loses real data.

**Do (pick one, recommend 3a):**

- [ ] **3a — Slot-originated session.** In `bind_slot`, when
      `get_latest_open_session` returns `None`, open a new session from the bind
      payload (`open_session`-style, `entry_camera_id = slot camera`,
      `entry_time = parked_at`, mark `origin = 'slot'` so it is distinguishable
      from a gate entry). Then continue binding.
- [ ] **3b — Reconciler (alternative/additional).** Add a periodic task that
      scans `parking_slots.current_plate` (written by VA) and opens sessions for
      plates with no open session. Lower coupling, higher latency.
- [ ] Add an `origin` column (`'gate'` / `'slot'`) to `parking_sessions` if 3a
      is chosen, so the dashboard can distinguish confirmed-by-ANPR from
      confirmed-by-slot.

**Acceptance:** with the ANPR entry disabled, a VA bind for a new plate creates
an open `parking_sessions` row (origin `slot`) instead of returning 404.

---

## Step 4 — Lock the timezone convention (fixes I-5, this repo's half)

**Convention (DECIDED):** KSA/Riyadh = **UTC+3, no DST**. Columns store **naive
facility-local (UTC+3 wall clock)** — this repo already writes that way
(`parking_session_service.py:18-28`, `entry_exit_service.py:637-643`). The
Gateway still documents UTC-naive and must be corrected (its Step 1).

**Do:**

- [ ] Confirm every service runs with `FACILITY_TIMEZONE_OFFSET_HOURS=3.0` (this
      repo, VideoAnalytics, and the Gateway must all use the same value).
- [ ] Spot-check a known gate event on the live DB: stored `event_time` should
      equal the operator's UTC+3 wall clock (no ±3h offset).
- [ ] Record the locked convention in this repo's `CLAUDE.md` and in a
      one-paragraph `DB_TIME_CONVENTION.md` shared with the Gateway team:
      "all timestamp columns are naive facility-local, UTC+3".
- [ ] No timestamp-write code change here — this repo is already correct; the
      Gateway aligns to it.

**Acceptance:** all three services report `FACILITY_TIMEZONE_OFFSET_HOURS=3.0` at
boot; a gate event's stored `event_time` matches the UTC+3 wall clock.

---

## Step 5 — `vehicles` placeholder hygiene (fixes I-6)

**Problem:** `ensure_unregistered_vehicle` inserts an "Unknown /
`is_registered=False`" row per new plate, so `vehicles` is "plates seen", not a
registry.

**Do:**

- [ ] Keep the placeholder behavior (it is intentional) but document it at the
      top of `vehicle_service.py`.
- [ ] Confirm every "known vehicles" count in this repo filters
      `is_registered = 1`; add the filter where missing.

**Acceptance:** no query labeled "registered/known vehicles" counts placeholder
rows.

---

## Verification (whole repo)

- [ ] `python -m pytest tests/ -v` green.
- [ ] Fire a simulated ANPR entry (`scripts/test/simulate_event.py --event anpr
      --plate "ABC 1234"`), then a bind for the normalized plate → session opens,
      binds, and `vehicle_id` is returned.
- [ ] Confirm branch: this repo is currently on
      `fix/entry-anpr-gateway-nat-rescue`. Merge these fixes into the branch you
      actually deploy from and confirm that is `main`.
