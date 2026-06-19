"""Tests for Layer V — the hierarchical Diary via the handoff outbox.

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

The ten tests cover the required diary-outbox cases.
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

# Diary parameters: N=5, diary_window=20, max_sentences=50, SECTION_SIZE=7, M=8, K=1.
N = 5
WINDOW = 20  # diary_window (turns); the daily window LIMIT is WINDOW * 2 rows.
MAX_SENTENCES = 50
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
    """After N=5 interactions a daily job exists: empty prior_summary, 10 turns, advance_to.

    Counts are N-driven only (the day holds 10 rows, far below the WINDOW*2=40
    row LIMIT, so the window returns every row).
    """
    _episodic_ddl(db)
    store = DiaryStore(db)
    scheduler = DiaryScheduler(store, db, DiaryConfig(enabled=True), today=lambda: "2026-06-10")
    day = "2026-06-10"

    # N-1 interactions: no job yet (counts 1..4 are not multiples of N).
    for i in range(1, N):
        _interaction(db, scheduler, day, i)
    assert store.pending_jobs() == []

    # The Nth interaction crosses the N boundary -> exactly one pending daily job.
    _interaction(db, scheduler, day, N)
    jobs = store.pending_jobs()
    assert len(jobs) == 1
    job = jobs[0]
    assert job["kind"] == "daily"
    assert job["target"] == day

    # prior_summary is empty on the first roll; input is the 10 turns (5 x user+assistant).
    assert (job["input_obj"]["prior_summary"] or "") == ""
    items = job["input_obj"]["items"]
    assert len(items) == 2 * N
    assert [it["role"] for it in items] == ["user", "assistant"] * N
    # advance_to == the id of the last (newest) turn.
    assert job["advance_to"] == max(it["id"] for it in items)


# --------------------------------------------------------------------------- #
# 3. apply daily (rolling)
# --------------------------------------------------------------------------- #
def test_apply_daily_then_rolling_uses_prior_summary_and_overlapping_window(db, config):
    """submit_summary sets summary+covered_through; the next N-tick rolls an OVERLAPPING window.

    The window no longer sends a strict delta: with diary_window=20 (LIMIT 40)
    the day holds <40 rows, so the second job re-sends ALL rows of the day so far
    (overlap by design, reconciled via prior_summary + the prompt). The prior
    summary still flows; covered_through still advances to the day's max.
    """
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

    # prior_summary == the stored summary; the window OVERLAPS — it re-sends the
    # whole day (all 2*N*2 = 20 rows, < 40 LIMIT), starting from id 1.
    assert second["input_obj"]["prior_summary"] == "Day-one narrative."
    new_items = second["input_obj"]["items"]
    expected_rows = min(WINDOW * 2, 2 * (2 * N))
    assert len(new_items) == expected_rows
    assert min(it["id"] for it in new_items) == 1
    # advance_to is the day's newest id, strictly past the first roll's coverage.
    assert second["advance_to"] > first_advance

    # Applying the second job advances covered_through to the day's max id.
    scheduler.submit(second["job_id"], "Day-one narrative, continued.")
    assert store.get_day(day)["covered_through"] == max(it["id"] for it in new_items)


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


# --------------------------------------------------------------------------- #
# 11. activation via the bool shorthand
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "diary_arg, active",
    [
        (True, True),                       # bool shorthand → DiaryConfig(enabled=True)
        (DiaryConfig(enabled=True), True),  # explicit enabled config
        (False, False),                     # bool shorthand → off
        (None, False),                      # default → off
        (DiaryConfig(enabled=False), False),  # explicit disabled config
    ],
)
def test_diary_arg_activation(tmp_path, diary_arg, active):
    """`diary=True` activates Layer V exactly like `DiaryConfig(enabled=True)`."""
    mem = NexusMemory(db_path=str(tmp_path / "a.db"), diary=diary_arg)
    try:
        assert (mem._diary is not None) is active
        # When off, the convenience wrappers report the layer is not enabled.
        if not active:
            assert mem.pending_summaries()["status"] == "error"
    finally:
        mem.close()


def test_diary_true_matches_default_config_knobs(tmp_path):
    """The bool shorthand builds the layer with the documented default knobs."""
    mem = NexusMemory(db_path=str(tmp_path / "b.db"), diary=True)
    try:
        cfg = mem._diary.config
        assert (
            cfg.update_every,
            cfg.diary_window,
            cfg.max_sentences,
            cfg.section_size,
            cfg.max_sections,
            cfg.inject_days,
        ) == (5, 20, 50, 7, 8, 1)
    finally:
        mem.close()


# --------------------------------------------------------------------------- #
# 12. drain_diary — host-model outbox helper
# --------------------------------------------------------------------------- #
def test_drain_diary_enabled_applies_pending_jobs(db_path):
    """With the diary ON, drain_diary runs a host model over the enqueued daily job.

    Ingest N=5 interactions (the consolidator ticks the scheduler on each, using
    the real UTC day that the freshly inserted turns carry), wait so the writes +
    the diary tick complete, then drain. The deterministic model returns a derived
    non-empty string per job; drain_diary applies it and reports the count.
    """
    mem = NexusMemory(db_path=db_path, diary=True)
    try:
        # N interactions cross the daily cadence boundary -> one pending daily job.
        for i in range(N):
            mem.process(
                {
                    "action": "ingest",
                    "interaction": {"query": f"q{i}", "response": f"r{i}"},
                }
            )
        mem.wait()

        # A pending daily job exists before draining.
        jobs = mem.pending_summaries()
        assert any(j["kind"] == "daily" for j in jobs)
        daily = next(j for j in jobs if j["kind"] == "daily")
        day = daily["period"]

        # Deterministic host model: derive a non-empty narrative from the job.
        def model(job: dict) -> str:
            return f"Narrative for {job['period']} ({job['job_id']})."

        applied = mem.drain_diary(model)
        assert applied >= 1

        # The day's summary was actually set (via inspect + the diary store).
        state = mem.inspect(type="diary")
        assert state["status"] == "success"
        day_row = next(d for d in state["data"]["days"] if d["period"] == day)
        assert day_row["summary"] == f"Narrative for {day} ({daily['job_id']})."
        assert mem._diary.store.get_day(day)["summary"] == day_row["summary"]

        # Nothing is left pending after a successful drain.
        assert mem.pending_summaries() == []
    finally:
        mem.close()


def test_drain_diary_off_returns_zero_and_applies_nothing(db_path):
    """With the diary OFF, drain_diary is a no-op returning 0 (never raises)."""
    mem = NexusMemory(db_path=db_path)
    try:
        assert mem._diary is None

        calls = []

        def model(job: dict) -> str:
            calls.append(job)
            return "x"

        assert mem.drain_diary(model) == 0
        # The host model was never invoked because there is no diary to drain.
        assert calls == []
        # The lambda form from the task brief is likewise a no-op returning 0.
        assert mem.drain_diary(lambda j: "x") == 0
    finally:
        mem.close()


# --------------------------------------------------------------------------- #
# 13. window — caps at diary_window * 2 rows
# --------------------------------------------------------------------------- #
def test_window_caps_at_diary_window_times_two_rows(db, config):
    """When more turns arrive than diary_window AFTER an apply, the window caps at diary_window*2.

    The window's lower edge is ``min(covered_through+1, newest-diary_window*2+1)``:
    completeness (covered_through+1) wins until a summary is applied, then the cap
    (diary_window*2) bounds the overlap. So we apply once, then drive more turns.
    """
    _episodic_ddl(db)
    store = DiaryStore(db)
    # Small knobs so the cap bites: diary_window=3 (6 rows), tick every interaction.
    cfg = DiaryConfig(enabled=True, update_every=1, diary_window=3)
    scheduler = DiaryScheduler(store, db, cfg, today=lambda: "2026-07-01")
    day = "2026-07-01"

    # Drive several interactions, applying each time so covered_through stays at
    # the day's max (the host is keeping up). The cap bites only when the number
    # of UNcovered rows is below diary_window*2 — then the window pulls back
    # diary_window*2 rows for overlap rather than just the few uncovered ones.
    for i in range(1, 6):
        _interaction(db, scheduler, day, i)
        job = store.pending_jobs()[0]
        scheduler.submit(job["job_id"], f"Covered through {job['advance_to']}.")

    # One more interaction -> only 2 uncovered rows, but the window pulls back the
    # last diary_window*2 = 6 rows for reconciliation overlap.
    _interaction(db, scheduler, day, 6)
    job = store.pending_jobs()[0]
    items = job["input_obj"]["items"]
    assert len(items) == cfg.diary_window * 2  # 6 — the overlap cap bites
    # It is the MOST RECENT 6 rows and starts on a turn boundary (user-first).
    assert items[0]["role"] == "user"
    assert job["advance_to"] == max(it["id"] for it in items)


# --------------------------------------------------------------------------- #
# 14. window — includes assistant turns
# --------------------------------------------------------------------------- #
def test_window_includes_assistant_turns(db, config):
    """The daily window carries both roles (user AND assistant), not user-only."""
    _episodic_ddl(db)
    store = DiaryStore(db)
    scheduler = DiaryScheduler(store, db, DiaryConfig(enabled=True), today=lambda: "2026-07-02")
    day = "2026-07-02"
    for i in range(1, N + 1):
        _interaction(db, scheduler, day, i)

    items = store.pending_jobs()[0]["input_obj"]["items"]
    roles = {it["role"] for it in items}
    assert roles == {"user", "assistant"}
    assert any(it["role"] == "assistant" for it in items)


# --------------------------------------------------------------------------- #
# 15. window — sends all rows when fewer than the cap
# --------------------------------------------------------------------------- #
def test_window_sends_all_when_fewer_than_cap(db, config):
    """With fewer than diary_window turns, the window returns every row of the day."""
    _episodic_ddl(db)
    store = DiaryStore(db)
    scheduler = DiaryScheduler(store, db, DiaryConfig(enabled=True), today=lambda: "2026-07-03")
    day = "2026-07-03"
    for i in range(1, N + 1):
        _interaction(db, scheduler, day, i)

    items = store.pending_jobs()[0]["input_obj"]["items"]
    # N interactions = 2*N rows, all below the WINDOW*2 cap -> all returned, from id 1.
    assert len(items) == 2 * N
    assert min(it["id"] for it in items) == 1


# --------------------------------------------------------------------------- #
# 16. covered_through / advance_to advance across two rolls
# --------------------------------------------------------------------------- #
def test_covered_through_advances_across_two_rolls(db, config):
    """advance_to >= covered_through after each tick; covered_through climbs across rolls."""
    _episodic_ddl(db)
    store = DiaryStore(db)
    scheduler = DiaryScheduler(store, db, DiaryConfig(enabled=True), today=lambda: "2026-07-04")
    day = "2026-07-04"

    for i in range(1, N + 1):
        _interaction(db, scheduler, day, i)
    first = store.pending_jobs()[0]
    # Invariant: advance_to is always >= the day's covered_through.
    assert first["advance_to"] >= store.get_day(day)["covered_through"]
    scheduler.submit(first["job_id"], "Roll one.")
    covered_1 = store.get_day(day)["covered_through"]
    assert covered_1 == first["advance_to"]

    for i in range(N + 1, 2 * N + 1):
        _interaction(db, scheduler, day, i)
    second = store.pending_jobs()[0]
    assert second["advance_to"] >= covered_1
    scheduler.submit(second["job_id"], "Roll two.")
    covered_2 = store.get_day(day)["covered_through"]
    assert covered_2 > covered_1  # the high-water mark climbs


# --------------------------------------------------------------------------- #
# 17. window — a custom diary_window=K is respected
# --------------------------------------------------------------------------- #
def test_custom_diary_window_respected(db, config):
    """diary_window=K caps the overlap window at K*2 rows once a summary is applied."""
    _episodic_ddl(db)
    store = DiaryStore(db)
    cfg = DiaryConfig(enabled=True, update_every=1, diary_window=2)
    scheduler = DiaryScheduler(store, db, cfg, today=lambda: "2026-07-05")
    day = "2026-07-05"

    # Keep applying so covered_through tracks the day's max; then one fresh tick
    # leaves few uncovered rows and the K*2 overlap cap bounds the window.
    for i in range(1, 5):
        _interaction(db, scheduler, day, i)
        job = store.pending_jobs()[0]
        scheduler.submit(job["job_id"], f"Covered through {job['advance_to']}.")

    _interaction(db, scheduler, day, 5)
    items = store.pending_jobs()[0]["input_obj"]["items"]
    assert len(items) == 2 * 2  # diary_window=2 -> 4 rows


# --------------------------------------------------------------------------- #
# 18. max_sentences is formatted into the daily prompt
# --------------------------------------------------------------------------- #
def test_max_sentences_formatted_into_prompt(db, config):
    """The job's prompt carries the configured 2-N sentence range (no leftover braces)."""
    _episodic_ddl(db)
    store = DiaryStore(db)
    cfg = DiaryConfig(enabled=True, update_every=1, max_sentences=7)
    scheduler = DiaryScheduler(store, db, cfg, today=lambda: "2026-07-06")
    day = "2026-07-06"
    _interaction(db, scheduler, day, 1)

    prompt = store.pending_jobs()[0]["prompt"]
    assert "2-7 sentences" in prompt
    assert "{max_sentences}" not in prompt


# --------------------------------------------------------------------------- #
# 19. invalid config raises ValueError
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "kwargs",
    [
        {"update_every": 0},
        {"diary_window": 0},
        {"section_size": 0},
        {"max_sections": 0},
        {"max_sentences": 1},   # floor is 2
        {"inject_days": -1},    # 0 is allowed, negative is not
    ],
)
def test_invalid_diary_config_raises_value_error(kwargs):
    """__post_init__ validates the knobs regardless of enabled."""
    with pytest.raises(ValueError):
        DiaryConfig(**kwargs)


def test_inject_days_zero_is_allowed():
    """inject_days=0 (inject nothing) is a valid config (B5)."""
    cfg = DiaryConfig(inject_days=0)
    assert cfg.inject_days == 0


# --------------------------------------------------------------------------- #
# 20. finalized-but-unfolded day still folds (force guard, B3)
# --------------------------------------------------------------------------- #
def test_finalized_unfolded_day_still_folds(db, config):
    """A finalize() on an already-covered day is NOT short-circuited by the empty-tick guard."""
    _episodic_ddl(db)
    store = DiaryStore(db)
    scheduler = DiaryScheduler(store, db, DiaryConfig(enabled=True), today=lambda: "2026-07-07")
    day = "2026-07-07"

    for i in range(1, N + 1):
        _interaction(db, scheduler, day, i)
    # Apply the cadence job so covered_through == the day's max (nothing "new").
    job = store.pending_jobs()[0]
    scheduler.submit(job["job_id"], "Covered narrative.")
    assert store.get_day(day)["covered_through"] == job["advance_to"]
    assert store.pending_jobs() == []

    # finalize() must still enqueue a daily job (force=True), despite no new turns,
    # so the finalized-but-unfolded day is never stranded.
    scheduler.finalize()
    pend = store.pending_jobs()
    assert len(pend) == 1 and pend[0]["kind"] == "daily"
    assert store.get_day(day)["finalized"] == 1


# --------------------------------------------------------------------------- #
# 21. lone trailing user row does not crash (B7 turn-boundary trim)
# --------------------------------------------------------------------------- #
def test_lone_trailing_user_row_no_crash(db, config):
    """A direct user row with no assistant reply does not break the window read."""
    _episodic_ddl(db)
    store = DiaryStore(db)
    scheduler = DiaryScheduler(store, db, DiaryConfig(enabled=True, update_every=1),
                               today=lambda: "2026-07-08")
    day = "2026-07-08"

    # A full interaction, then a LONE trailing user row (odd row count).
    _interaction(db, scheduler, day, 1)
    _insert_turn(db, "user", "lone user", day, "05:00:00")
    scheduler.on_interaction(day=day)  # must not raise

    job = store.pending_jobs()[0]
    items = job["input_obj"]["items"]
    # The window still starts on a turn boundary and includes the lone user row.
    assert items[0]["role"] == "user"
    assert any(it["content"] == "lone user" for it in items)
    assert job["advance_to"] == max(it["id"] for it in items)
