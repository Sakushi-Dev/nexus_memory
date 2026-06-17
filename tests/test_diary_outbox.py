"""Tests for Layer V — the hierarchical Diary via the handoff outbox (CONTRACT-v3 §10).

These exercise the optional, provider-agnostic diary subsystem that lives entirely
in ``layers/diary/``. Everything here is offline and deterministic:

* the conftest ``db``/``config`` fixtures use a ``tmp_path`` SQLite file and the
  default :class:`HashingEmbedder`;
* the outbox is driven MANUALLY with deterministic text — no LLM is ever called;
* multiple UTC days are simulated by inserting ``episodic_turns`` rows with explicit
  ``YYYY-MM-DD HH:MM:SS`` timestamps and by passing an explicit ``day`` to
  :meth:`DiaryScheduler.on_interaction` (the scheduler reads the day's turns directly
  from the shared connection).

Unit-level tests (2–7) build :class:`DiaryStore`/:class:`DiaryScheduler` directly on
the ``db`` fixture. Integration tests (1, 8, 9, 10) use the full
``NexusMemory(diary=DiaryConfig(enabled=True))``.

The ten tests map one-to-one to the CONTRACT-v3 §10 required cases.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from nexus_memory.core.config import NexusConfig
from nexus_memory.core.db import NexusDB
from nexus_memory.core.orchestrator import NexusMemory
from nexus_memory.layers.diary.config import DiaryConfig
from nexus_memory.layers.diary.scheduler import DiaryScheduler
from nexus_memory.layers.diary.store import DiaryStore

# CONTRACT-v3 parameters: N=3, SECTION_SIZE=7, M=8, K=1.
N = 3
SECTION_SIZE = 7
M = 8
K = 1

_FACT_NEEDLE_RE = re.compile(r'<fact id="(\d+)"')


# --------------------------------------------------------------------------- #
# helpers (deterministic, offline)
# --------------------------------------------------------------------------- #
def _insert_turn(db: NexusDB, role: str, content: str, day: str, hms: str) -> int:
    """Insert one ``episodic_turns`` row at an explicit UTC timestamp; return its id."""
    with db.lock:
        cur = db.conn.execute(
            "INSERT INTO episodic_turns (session_id, role, content, timestamp, metadata) "
            "VALUES (?, ?, ?, ?, NULL)",
            ("sess", role, content, f"{day} {hms}"),
        )
        db.conn.commit()
        return int(cur.lastrowid)


def _episodic_ddl(db: NexusDB) -> None:
    """Ensure the ``episodic_turns`` table exists for raw inserts (unit tests).

    The scheduler reads ``episodic_turns`` directly. In the unit-level tests we
    do not build an EpisodicStore, so create the table here exactly as the
    episodic layer would (idempotent).
    """
    with db.lock:
        db.conn.execute(
            "CREATE TABLE IF NOT EXISTS episodic_turns ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT NOT NULL, "
            "content TEXT NOT NULL, timestamp TEXT NOT NULL, metadata TEXT)"
        )
        db.conn.commit()


def _interaction(db: NexusDB, scheduler: DiaryScheduler, day: str, idx: int) -> None:
    """Simulate one ingested interaction on ``day``: a user + assistant turn, then a tick.

    ``idx`` only spaces the timestamps apart so ordering is unambiguous.
    """
    hh = f"{(idx % 24):02d}"
    _insert_turn(db, "user", f"u{idx} on {day}", day, f"{hh}:00:00")
    _insert_turn(db, "assistant", f"a{idx} on {day}", day, f"{hh}:00:01")
    scheduler.on_interaction(day=day)


def _table_names(db: NexusDB) -> set[str]:
    """Return the set of table names in ``sqlite_master``."""
    rows = db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {r["name"] for r in rows}


def _drain_daily(mem: NexusMemory, day: str, text: str) -> dict:
    """Find the pending daily job for ``day`` and submit ``text``; return submit result."""
    jobs = mem.pending_summaries()
    job = next(j for j in jobs if j["kind"] == "daily" and j["period"] == day)
    return mem.submit_summary(job["job_id"], text)


# --------------------------------------------------------------------------- #
# 1. off-by-default
# --------------------------------------------------------------------------- #
def test_off_by_default_no_diary_tables_or_jobs(db_path):
    """Without a DiaryConfig the layer is never built: no tables, no jobs, no sections."""
    mem = NexusMemory(db_path=db_path)
    try:
        # The diary layer object was never constructed.
        assert mem._diary is None

        # The three diary tables do not exist in sqlite_master.
        names = _table_names(mem.db)
        assert "diary_days" not in names
        assert "persistent_sections" not in names
        assert "summarization_jobs" not in names

        # A normal ingest + assemble still works and produces no diary sections.
        mem.process(
            {"action": "ingest", "interaction": {"query": "hi", "response": "hello"}}
        )
        mem.wait()
        result = mem.process({"action": "assemble", "query": "hi"})
        assert result["status"] == "success"
        assert "<diary" not in result["context_xml"]
        assert "<persistent_summary>" not in result["context_xml"]
        # The additive response keys are absent when the layer is off.
        assert "diary" not in result
        assert "persistent_summary" not in result

        # The diary actions are unknown -> normal validation error.
        unknown = mem.process({"action": "pending_summaries"})
        assert unknown["status"] == "error"

        # The convenience wrappers report the layer is not enabled.
        assert mem.pending_summaries()["status"] == "error"
        assert mem.submit_summary("x", "y")["status"] == "error"
    finally:
        mem.close()


# --------------------------------------------------------------------------- #
# 2. daily cadence
# --------------------------------------------------------------------------- #
def test_daily_cadence_enqueues_after_n_interactions(db, config):
    """After N=3 interactions a daily job exists: empty prior_summary, 6 turns, advance_to."""
    _episodic_ddl(db)
    store = DiaryStore(db)
    scheduler = DiaryScheduler(store, db, DiaryConfig(enabled=True), today=lambda: "2026-06-10")
    day = "2026-06-10"

    # Two interactions: no job yet (count 1, 2 are not multiples of N).
    _interaction(db, scheduler, day, 1)
    _interaction(db, scheduler, day, 2)
    assert store.pending_jobs() == []

    # Third interaction crosses the N boundary -> exactly one pending daily job.
    _interaction(db, scheduler, day, 3)
    jobs = store.pending_jobs()
    assert len(jobs) == 1
    job = jobs[0]
    assert job["kind"] == "daily"
    assert job["target"] == day

    # prior_summary is empty on the first roll; input is the 6 turns (3 x user+assistant).
    assert (job["input_obj"]["prior_summary"] or "") == ""
    items = job["input_obj"]["items"]
    assert len(items) == 6
    assert [it["role"] for it in items] == [
        "user", "assistant", "user", "assistant", "user", "assistant",
    ]
    # advance_to == the id of the last (newest) turn.
    assert job["advance_to"] == max(it["id"] for it in items)


# --------------------------------------------------------------------------- #
# 3. apply daily (rolling)
# --------------------------------------------------------------------------- #
def test_apply_daily_then_rolling_uses_prior_summary_and_new_turns(db, config):
    """submit_summary sets summary+covered_through; the next N-tick rolls only NEW turns."""
    _episodic_ddl(db)
    store = DiaryStore(db)
    scheduler = DiaryScheduler(store, db, DiaryConfig(enabled=True), today=lambda: "2026-06-11")
    day = "2026-06-11"

    for i in range(1, N + 1):
        _interaction(db, scheduler, day, i)
    first = store.pending_jobs()[0]
    first_advance = first["advance_to"]

    # Apply the first daily summary.
    res = scheduler.submit(first["job_id"], "Day-one narrative.")
    assert res == {"status": "success", "applied": "daily"}

    row = store.get_day(day)
    assert row["summary"] == "Day-one narrative."
    assert row["covered_through"] == first_advance

    # A second N-tick of fresh interactions -> a rolling daily job.
    for i in range(N + 1, 2 * N + 1):
        _interaction(db, scheduler, day, i)
    second = store.pending_jobs()[0]
    assert second["kind"] == "daily"

    # prior_summary == the stored summary; input == only the NEW turns (the 6 fresh ones).
    assert second["input_obj"]["prior_summary"] == "Day-one narrative."
    new_items = second["input_obj"]["items"]
    assert len(new_items) == 6
    assert min(it["id"] for it in new_items) > first_advance


# --------------------------------------------------------------------------- #
# 4. supersede
# --------------------------------------------------------------------------- #
def test_two_ticks_before_submit_supersede_leaves_one_pending(db, config):
    """Two N-ticks before a submit: the first daily job is superseded, one pending remains."""
    _episodic_ddl(db)
    store = DiaryStore(db)
    scheduler = DiaryScheduler(store, db, DiaryConfig(enabled=True), today=lambda: "2026-06-12")
    day = "2026-06-12"

    for i in range(1, N + 1):
        _interaction(db, scheduler, day, i)
    first_id = store.pending_jobs()[0]["job_id"]

    # A second N-tick (no submit in between) enqueues a newer daily job.
    for i in range(N + 1, 2 * N + 1):
        _interaction(db, scheduler, day, i)

    pending = store.pending_jobs()
    assert len(pending) == 1
    assert pending[0]["job_id"] != first_id

    # The earlier job is now 'superseded'.
    assert store.get_job(first_id)["status"] == "superseded"


# --------------------------------------------------------------------------- #
# 5. rollover + fold
# --------------------------------------------------------------------------- #
def test_rollover_finalizes_day_then_fold_enqueues_and_applies_section(db, config):
    """Turns on D then D+1 -> D finalized; D's daily submit yields a section job; fold -> count 1."""
    _episodic_ddl(db)
    store = DiaryStore(db)
    scheduler = DiaryScheduler(store, db, DiaryConfig(enabled=True))
    D = "2026-06-13"
    D1 = "2026-06-14"

    # N interactions on D -> a daily job for D (D not yet finalized).
    for i in range(1, N + 1):
        _interaction(db, scheduler, D, i)
    assert store.get_day(D)["finalized"] == 0

    # First interaction on D+1 triggers the rollover: D becomes finalized.
    _interaction(db, scheduler, D1, 100)
    assert store.get_day(D)["finalized"] == 1

    # Submit D's daily summary -> because D is finalized+unfolded, a section job appears.
    daily = next(
        j for j in store.pending_jobs() if j["kind"] == "daily" and j["target"] == D
    )
    scheduler.submit(daily["job_id"], "Narrative for day D.")

    section_job = store.pending_section_job()
    assert section_job is not None
    assert section_job["kind"] == "section"
    # The section job carries exactly the one finalized day D.
    items = section_job["input_obj"]["items"]
    assert len(items) == 1 and items[0]["period"] == D

    # Apply the section -> open section diary_count == 1 and D.folded == 1.
    scheduler.submit(section_job["job_id"], "Section summary covering D.")
    sec = store.open_section()
    assert sec is not None
    assert sec["diary_count"] == 1
    assert sec["first_day"] == D and sec["last_day"] == D
    assert store.get_day(D)["folded"] == 1


# --------------------------------------------------------------------------- #
# 6. section freeze
# --------------------------------------------------------------------------- #
def test_folding_section_size_days_freezes_section_and_allocates_fresh(db, config):
    """Folding SECTION_SIZE=7 finalized days freezes the section and opens a fresh one."""
    _episodic_ddl(db)
    store = DiaryStore(db)
    scheduler = DiaryScheduler(store, db, DiaryConfig(enabled=True))

    # Drive SECTION_SIZE + 1 distinct days so the first 7 fold (and freeze) the section.
    base = datetime(2026, 6, 1)
    days = [(base + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(SECTION_SIZE + 1)]

    for di, day in enumerate(days):
        for i in range(1, N + 1):
            _interaction(db, scheduler, day, di * 10 + i)

    # Fold the first SECTION_SIZE days strictly in chronological order. Each fold:
    # submit the day's daily (closes/covers it), then submit its section job.
    for day in days[:SECTION_SIZE]:
        # The day is finalized by the rollover into the next day; submit its final daily.
        daily = next(
            (j for j in store.pending_jobs() if j["kind"] == "daily" and j["target"] == day),
            None,
        )
        if daily is not None:
            scheduler.submit(daily["job_id"], f"Daily for {day}.")
        sec_job = store.pending_section_job()
        assert sec_job is not None, f"expected a section job while folding {day}"
        scheduler.submit(sec_job["job_id"], f"Section after {day}.")

    # Exactly one frozen section (diary_count == SECTION_SIZE) and one fresh open section.
    frozen = [s for s in store.sections() if s["frozen"] == 1]
    assert len(frozen) == 1
    assert frozen[0]["diary_count"] == SECTION_SIZE

    open_sec = store.open_section()
    assert open_sec is not None
    assert open_sec["frozen"] == 0
    assert open_sec["diary_count"] == 0
    assert open_sec["seq"] > frozen[0]["seq"]


# --------------------------------------------------------------------------- #
# 7. ring overwrite
# --------------------------------------------------------------------------- #
def test_ring_overwrites_oldest_section_beyond_capacity(db, config):
    """Driving > M*SECTION_SIZE days keeps only M sections; the oldest seq is overwritten."""
    _episodic_ddl(db)
    store = DiaryStore(db)
    scheduler = DiaryScheduler(store, db, DiaryConfig(enabled=True))

    total_days = M * SECTION_SIZE + SECTION_SIZE  # 56 + 7 -> forces a ring overwrite
    base = datetime(2026, 1, 1)
    days = [(base + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(total_days)]

    for di, day in enumerate(days):
        for i in range(1, N + 1):
            _interaction(db, scheduler, day, di * 10 + i)

    # Fold every finalized day in chronological order. The last day stays open (not
    # finalized) but its predecessors all fold; drain whatever section jobs appear.
    for day in days:
        daily = next(
            (j for j in store.pending_jobs() if j["kind"] == "daily" and j["target"] == day),
            None,
        )
        if daily is not None:
            scheduler.submit(daily["job_id"], f"Daily for {day}.")
        # Drain any section jobs that the daily-apply produced (one at a time).
        while True:
            sec_job = store.pending_section_job()
            if sec_job is None:
                break
            scheduler.submit(sec_job["job_id"], f"Section folding {day}.")

    # The physical ring never exceeds M slots.
    all_slots = store.db.conn.execute(
        "SELECT slot FROM persistent_sections"
    ).fetchall()
    assert len(all_slots) <= M

    # Live sections are bounded by M, each slot in range.
    live = store.sections()
    assert len(live) <= M
    for s in live:
        assert 0 <= s["slot"] < M

    # The oldest original section was overwritten: the smallest live seq is greater
    # than 1 (seq 1 has been recycled), and coverage windows are coherent.
    seqs = [s["seq"] for s in live]
    assert min(seqs) > 1
    for s in live:
        if s["first_day"] and s["last_day"]:
            assert s["first_day"] <= s["last_day"]


# --------------------------------------------------------------------------- #
# 8. context injection
# --------------------------------------------------------------------------- #
def test_context_injection_emits_diary_and_persistent_summary_no_ids(db_path):
    """assemble emits <diary day> (K=1, previous day) + <persistent_summary>, no id="..." inside."""
    mem = NexusMemory(db_path=db_path, diary=DiaryConfig(enabled=True))
    try:
        store = mem._diary.store

        # The provider compares against the REAL current UTC day, so build a
        # finalized "previous day" relative to actual now.
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        yesterday = (
            datetime.now(timezone.utc) - timedelta(days=1)
        ).strftime("%Y-%m-%d")

        # A finalized previous day with a non-empty summary (drives <diary>).
        with mem.db.lock:
            mem.db.conn.execute(
                "INSERT INTO diary_days "
                "(period, summary, covered_through, interaction_count, finalized, folded, updated_at) "
                "VALUES (?, ?, 0, 6, 1, 0, ?)",
                (yesterday, "Yesterday the user planned the release.", today),
            )
            # A live persistent section (drives <persistent_summary>).
            mem.db.conn.execute(
                "INSERT INTO persistent_sections "
                "(slot, seq, summary, diary_count, first_day, last_day, frozen, updated_at) "
                "VALUES (0, 1, ?, 3, '2026-05-01', '2026-05-03', 0, ?)",
                ("An epoch summary across early May.", today),
            )
            mem.db.conn.commit()

        # Ingest a couple of facts so the semantic needle exists, then assemble.
        mem.process(
            {
                "action": "ingest",
                "interaction": {
                    "query": "My name is Sam and I use Python.",
                    "response": "Noted, Sam.",
                },
            }
        )
        mem.wait()

        top_k = 5
        result = mem.process(
            {"action": "assemble", "query": "what is my name", "top_k": top_k}
        )
        assert result["status"] == "success"
        xml = result["context_xml"]

        # The two bounded sections are present.
        assert f'<diary day="{yesterday}">' in xml
        assert "<persistent_summary>" in xml
        assert "</persistent_summary>" in xml

        # K=1: exactly one <diary ...> element (only the previous day).
        assert len(re.findall(r"<diary day=", xml)) == K

        # No id="..." appears INSIDE the diary / persistent_summary fragments.
        diary_frag = re.search(r"<diary .*?</diary>", xml, re.DOTALL).group(0)
        persistent_frag = re.search(
            r"<persistent_summary>.*?</persistent_summary>", xml, re.DOTALL
        ).group(0)
        assert 'id="' not in diary_frag
        assert 'id="' not in persistent_frag

        # The needle invariant is preserved: at most top_k <fact id="\d+"> elements.
        assert len(_FACT_NEEDLE_RE.findall(xml)) <= top_k

        # The additive response superset keys are present.
        assert result["diary"]["day"] == yesterday
        assert result["persistent_summary"]
        assert result["meta"]["section_count"] >= 1
    finally:
        mem.close()


# --------------------------------------------------------------------------- #
# 9. persistence across a fresh NexusDB on the same db_path
# --------------------------------------------------------------------------- #
def test_jobs_days_sections_survive_reopen(config, db_path):
    """Jobs + diary_days + sections survive constructing a fresh NexusDB on the same path."""
    diary_cfg = DiaryConfig(enabled=True)

    # Session 1: build diary state directly on a NexusDB, then close.
    db1 = NexusDB(config)
    try:
        _episodic_ddl(db1)
        store1 = DiaryStore(db1)
        scheduler1 = DiaryScheduler(store1, db1, diary_cfg, today=lambda: "2026-06-15")
        day = "2026-06-15"
        for i in range(1, N + 1):
            _interaction(db1, scheduler1, day, i)

        # One pending daily job + a diary_days row exist.
        assert len(store1.pending_jobs()) == 1
        assert store1.get_day(day) is not None

        # Also stamp a persistent section so all three tables carry rows.
        store1.allocate_section(M)
        store1.apply_section(store1.open_section()["slot"], "A section.", day)
        pending_id = store1.pending_jobs()[0]["job_id"]
    finally:
        db1.close()

    # Session 2: a brand-new NexusDB on the SAME file path.
    assert config.db_path == db_path
    db2 = NexusDB(config)
    try:
        store2 = DiaryStore(db2)  # CREATE TABLE IF NOT EXISTS -> finds the existing rows.

        # The pending job survived intact.
        jobs = store2.pending_jobs()
        assert len(jobs) == 1
        assert jobs[0]["job_id"] == pending_id
        assert jobs[0]["kind"] == "daily"

        # The diary_days row survived.
        row = store2.get_day("2026-06-15")
        assert row is not None
        assert row["interaction_count"] == N

        # The persistent section survived.
        secs = store2.sections()
        assert len(secs) == 1
        assert secs[0]["diary_count"] == 1
    finally:
        db2.close()


# --------------------------------------------------------------------------- #
# 10. idempotent submit
# --------------------------------------------------------------------------- #
def test_resubmitting_done_job_is_safe_no_op(db_path):
    """Re-submitting a 'done' job is a safe no-op returning a status note (never raises)."""
    mem = NexusMemory(db_path=db_path, diary=DiaryConfig(enabled=True))
    try:
        store = mem._diary.store
        scheduler = mem._diary.scheduler

        # Pin the day so the turns and the scheduler agree.
        day = "2026-06-16"
        scheduler._today = lambda: day  # noqa: SLF001 - deterministic test injection
        for i in range(1, N + 1):
            _insert_turn(mem.db, "user", f"u{i}", day, f"{i:02d}:00:00")
            _insert_turn(mem.db, "assistant", f"a{i}", day, f"{i:02d}:00:01")
            scheduler.on_interaction(day=day)

        job = store.pending_jobs()[0]
        job_id = job["job_id"]

        # First submit applies and marks the job done.
        first = mem.submit_summary(job_id, "Applied once.")
        assert first["status"] == "success"
        assert store.get_job(job_id)["status"] == "done"

        # Re-submitting the SAME (now done) job is a safe no-op (no raise, status note).
        again = mem.submit_summary(job_id, "Should be ignored.")
        assert again["status"] == "success"
        # The stored summary did not change on the redundant submit.
        assert store.get_day(day)["summary"] == "Applied once."

        # An unknown job id is also a safe no-op, not an error/raise.
        unknown = mem.submit_summary("does-not-exist", "whatever")
        assert unknown["status"] == "not_found"
    finally:
        mem.close()
