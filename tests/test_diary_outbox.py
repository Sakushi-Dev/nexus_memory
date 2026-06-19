"""Tests for Layer V — the session-scoped Diary via the handoff outbox.

These exercise the optional, provider-agnostic diary subsystem that lives entirely
in ``layers/diary/``. Everything here is offline and deterministic:

* the conftest ``db``/``config`` fixtures use a ``tmp_path`` SQLite file and the
  default :class:`HashingEmbedder`;
* the outbox is driven MANUALLY with deterministic text — no LLM is ever called;
* sessions are simulated by tagging ``episodic_turns`` rows with an explicit
  ``session_id`` and by injecting the scheduler/provider with a ``session``
  callable (the scheduler reads the session's turns directly from the shared
  connection, scoped by ``episodic_turns.session_id``).

Unit-level tests build :class:`DiaryStore`/:class:`DiaryScheduler` directly on the
``db`` fixture with an injected ``session`` callable. Integration tests use the
full ``NexusMemory(diary=DiaryConfig(enabled=True))``.

The tests cover the required session-diary cases (plan §Tests):

1. off-by-default — no ``diary_sessions``/``persistent_summary`` tables.
2. session cadence — a ``session`` job after ``update_every`` interactions.
3. rolling — the second job carries ``prior_summary`` + an overlapping window.
4. supersede — two ticks before submit leave one pending, one superseded.
5. rollover — turns under ``s-1`` then ``s-2`` finalize ``s-1`` + a final job.
6. 6-session fold — one ``summary`` job; apply fills the single
   ``persistent_summary`` row and marks the 6 ``folded=1``.
7. extension — a further 6 sessions EXTEND the same row (``prior_summary``
   passed, ``session_count`` == 12).
8. cap — ``summary_max_sentences`` (300) formatted into the summary prompt.
9. injection — ``<diary session="current">`` + ``inject_sessions`` previous +
   exactly one ``<persistent_summary>``, with NO ``id="..."``.
10. validation — ``inject_sessions`` outside ``0..6`` and ``summary_max_sentences``
    < 2 raise ``ValueError``.
11. persistence — ``diary_sessions`` + ``persistent_summary`` survive a fresh
    ``NexusDB`` reopen on the same path.
"""

from __future__ import annotations

import re

import pytest

from nexus_memory.core.db import NexusDB
from nexus_memory.core.orchestrator import NexusMemory
from nexus_memory.layers.diary.config import DiaryConfig
from nexus_memory.layers.diary.scheduler import DiaryScheduler
from nexus_memory.layers.diary.store import DiaryStore

# Diary parameters: N=5, diary_window=20, max_sentences=50,
# sessions_per_summary=6, inject_sessions=1, summary_max_sentences=300.
N = 5
WINDOW = 20  # diary_window (turns); the session window LIMIT is WINDOW * 2 rows.
MAX_SENTENCES = 50
SESSIONS_PER_SUMMARY = 6
INJECT_SESSIONS = 1
SUMMARY_MAX_SENTENCES = 300

_FACT_NEEDLE_RE = re.compile(r'<fact id="(\d+)"')


# --------------------------------------------------------------------------- #
# helpers (deterministic, offline)
# --------------------------------------------------------------------------- #
def _insert_turn(db: NexusDB, session_id: str, role: str, content: str, ts: str) -> int:
    """Insert one ``episodic_turns`` row tagged with ``session_id``; return its id."""
    with db.lock:
        cur = db.conn.execute(
            "INSERT INTO episodic_turns (session_id, role, content, timestamp, metadata) "
            "VALUES (?, ?, ?, ?, NULL)",
            (session_id, role, content, ts),
        )
        db.conn.commit()
        return int(cur.lastrowid)


def _episodic_ddl(db: NexusDB) -> None:
    """Ensure the ``episodic_turns`` table exists for raw inserts (unit tests).

    The scheduler reads ``episodic_turns`` directly, scoped by ``session_id``. In
    the unit-level tests we do not build an EpisodicStore, so create the table
    here exactly as the episodic layer would (idempotent).
    """
    with db.lock:
        db.conn.execute(
            "CREATE TABLE IF NOT EXISTS episodic_turns ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT NOT NULL, "
            "content TEXT NOT NULL, timestamp TEXT NOT NULL, metadata TEXT)"
        )
        db.conn.commit()


# A monotonic clock so inserted turns always order by id == by time.
_CLOCK = [0]


def _next_ts() -> str:
    """Return a fresh, strictly increasing UTC-ish timestamp string."""
    _CLOCK[0] += 1
    secs = _CLOCK[0]
    hh = (secs // 3600) % 24
    mm = (secs // 60) % 60
    ss = secs % 60
    return f"2026-06-10 {hh:02d}:{mm:02d}:{ss:02d}"


def _interaction(db: NexusDB, scheduler: DiaryScheduler, session_id: str, idx: int) -> None:
    """Simulate one ingested interaction in ``session_id``: user + assistant + tick."""
    _insert_turn(db, session_id, "user", f"u{idx} in {session_id}", _next_ts())
    _insert_turn(db, session_id, "assistant", f"a{idx} in {session_id}", _next_ts())
    scheduler.on_interaction(session_id=session_id)


def _table_names(db: NexusDB) -> set[str]:
    """Return the set of table names in ``sqlite_master``."""
    rows = db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {r["name"] for r in rows}


def _run_session(
    db: NexusDB, scheduler: DiaryScheduler, session_id: str, *, summary: str
) -> None:
    """Drive one full session: N interactions, then submit its cadence job.

    Leaves the session with a non-empty summary and ``covered_through`` at its
    newest turn. The session is finalized later by a rollover (a turn in a newer
    session) or by ``finalize()``.
    """
    for i in range(1, N + 1):
        _interaction(db, scheduler, session_id, i)
    job = next(
        j
        for j in scheduler.store.pending_jobs()
        if j["kind"] == "session" and j["target"] == session_id
    )
    scheduler.submit(job["job_id"], summary)


# --------------------------------------------------------------------------- #
# 1. off-by-default
# --------------------------------------------------------------------------- #
def test_off_by_default_no_diary_tables_or_jobs(db_path):
    """Without a DiaryConfig the layer is never built: no tables, no jobs."""
    mem = NexusMemory(db_path=db_path)
    try:
        # The diary layer object was never constructed.
        assert mem._diary is None

        # The diary tables do not exist in sqlite_master.
        names = _table_names(mem.db)
        assert "diary_sessions" not in names
        assert "persistent_summary" not in names
        assert "summarization_jobs" not in names

        # A normal ingest + assemble still works and produces no diary output.
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
# 2. session cadence
# --------------------------------------------------------------------------- #
def test_session_cadence_enqueues_after_n_interactions(db, config):
    """After N=5 interactions a session job exists: empty prior_summary, 10 turns."""
    _episodic_ddl(db)
    store = DiaryStore(db)
    scheduler = DiaryScheduler(store, db, DiaryConfig(enabled=True), session=lambda: "s-1")
    sid = "s-1"

    # N-1 interactions: no job yet (counts 1..4 are not multiples of N).
    for i in range(1, N):
        _interaction(db, scheduler, sid, i)
    assert store.pending_jobs() == []

    # The Nth interaction crosses the N boundary -> exactly one pending session job.
    _interaction(db, scheduler, sid, N)
    jobs = store.pending_jobs()
    assert len(jobs) == 1
    job = jobs[0]
    assert job["kind"] == "session"
    assert job["target"] == sid

    # prior_summary is empty on the first roll; input is the 10 turns (5 x user+assistant).
    assert (job["input_obj"]["prior_summary"] or "") == ""
    items = job["input_obj"]["items"]
    assert len(items) == 2 * N
    assert [it["role"] for it in items] == ["user", "assistant"] * N
    # advance_to == the id of the last (newest) turn.
    assert job["advance_to"] == max(it["id"] for it in items)


# --------------------------------------------------------------------------- #
# 3. apply session (rolling)
# --------------------------------------------------------------------------- #
def test_apply_session_then_rolling_uses_prior_summary_and_overlapping_window(db, config):
    """submit sets summary+covered_through; the next N-tick rolls an OVERLAPPING window.

    With diary_window=20 (LIMIT 40) the session holds < 40 rows, so the second job
    re-sends ALL rows of the session so far (overlap by design, reconciled via
    prior_summary + the prompt). The prior summary still flows; covered_through
    still advances to the session's max.
    """
    _episodic_ddl(db)
    store = DiaryStore(db)
    scheduler = DiaryScheduler(store, db, DiaryConfig(enabled=True), session=lambda: "s-1")
    sid = "s-1"

    for i in range(1, N + 1):
        _interaction(db, scheduler, sid, i)
    first = store.pending_jobs()[0]
    first_advance = first["advance_to"]

    # Apply the first session summary.
    res = scheduler.submit(first["job_id"], "Session-one narrative.")
    assert res == {"status": "success", "applied": "session"}

    row = store.get_session(sid)
    assert row["summary"] == "Session-one narrative."
    assert row["covered_through"] == first_advance

    # A second N-tick of fresh interactions -> a rolling session job.
    for i in range(N + 1, 2 * N + 1):
        _interaction(db, scheduler, sid, i)
    second = store.pending_jobs()[0]
    assert second["kind"] == "session"

    # prior_summary == the stored summary; the window OVERLAPS — it re-sends the
    # whole session (all 2*N*2 = 20 rows, < 40 LIMIT), starting from the first id.
    assert second["input_obj"]["prior_summary"] == "Session-one narrative."
    new_items = second["input_obj"]["items"]
    expected_rows = min(WINDOW * 2, 2 * (2 * N))
    assert len(new_items) == expected_rows
    assert min(it["id"] for it in new_items) == 1
    # advance_to is the session's newest id, strictly past the first roll's coverage.
    assert second["advance_to"] > first_advance

    # Applying the second job advances covered_through to the session's max id.
    scheduler.submit(second["job_id"], "Session-one narrative, continued.")
    assert store.get_session(sid)["covered_through"] == max(
        it["id"] for it in new_items
    )


# --------------------------------------------------------------------------- #
# 4. supersede
# --------------------------------------------------------------------------- #
def test_two_ticks_before_submit_supersede_leaves_one_pending(db, config):
    """Two N-ticks before a submit: the first session job is superseded, one remains."""
    _episodic_ddl(db)
    store = DiaryStore(db)
    scheduler = DiaryScheduler(store, db, DiaryConfig(enabled=True), session=lambda: "s-1")
    sid = "s-1"

    for i in range(1, N + 1):
        _interaction(db, scheduler, sid, i)
    first_id = store.pending_jobs()[0]["job_id"]

    # A second N-tick (no submit in between) enqueues a newer session job.
    for i in range(N + 1, 2 * N + 1):
        _interaction(db, scheduler, sid, i)

    pending = store.pending_jobs()
    assert len(pending) == 1
    assert pending[0]["job_id"] != first_id

    # The earlier job is now 'superseded'.
    assert store.get_job(first_id)["status"] == "superseded"


# --------------------------------------------------------------------------- #
# 5. rollover finalizes the previous session
# --------------------------------------------------------------------------- #
def test_rollover_finalizes_previous_session_and_enqueues_final_job(db, config):
    """Turns under s-1 then a turn under s-2 finalize s-1 + enqueue its final job."""
    _episodic_ddl(db)
    store = DiaryStore(db)
    # A mutable current-session pointer the scheduler reads each tick.
    current = ["s-1"]
    scheduler = DiaryScheduler(
        store, db, DiaryConfig(enabled=True), session=lambda: current[0]
    )

    # N interactions under s-1 -> a session job for s-1 (s-1 not yet finalized).
    for i in range(1, N + 1):
        _interaction(db, scheduler, "s-1", i)
    assert store.get_session("s-1")["finalized"] == 0

    # Submit s-1's cadence job so it carries a non-empty summary (not required for
    # the rollover, but mirrors a real run).
    s1_job = next(
        j for j in store.pending_jobs() if j["kind"] == "session" and j["target"] == "s-1"
    )
    scheduler.submit(s1_job["job_id"], "Session one narrative.")

    # The first interaction under s-2 triggers the rollover: s-1 becomes finalized.
    current[0] = "s-2"
    _interaction(db, scheduler, "s-2", 1)
    assert store.get_session("s-1")["finalized"] == 1

    # A final (force) session job for s-1 was enqueued by the rollover.
    final = next(
        (j for j in store.pending_jobs() if j["kind"] == "session" and j["target"] == "s-1"),
        None,
    )
    assert final is not None
    # s-2 exists and is still open.
    assert store.get_session("s-2")["finalized"] == 0


# --------------------------------------------------------------------------- #
# 6. 6-session fold -> single persistent_summary, 6 folded
# --------------------------------------------------------------------------- #
def test_six_sessions_fold_into_single_persistent_summary(db, config):
    """6 finalized sessions -> one summary job; apply fills the single row, 6 folded."""
    _episodic_ddl(db)
    store = DiaryStore(db)
    current = ["s-1"]
    scheduler = DiaryScheduler(
        store, db, DiaryConfig(enabled=True), session=lambda: current[0]
    )

    # Drive SESSIONS_PER_SUMMARY + 1 sessions so the first 6 finalize (each by the
    # rollover into the next session) and a fold triggers.
    n_sessions = SESSIONS_PER_SUMMARY + 1
    for s in range(1, n_sessions + 1):
        sid = f"s-{s}"
        current[0] = sid
        _run_session(db, scheduler, sid, summary=f"Narrative for {sid}.")

    # The first SESSIONS_PER_SUMMARY sessions are finalized + unfolded -> exactly one
    # pending summary job exists.
    finalized = store.finalized_unfolded_sessions()
    assert len(finalized) >= SESSIONS_PER_SUMMARY

    summary_job = store.pending_summary_job()
    assert summary_job is not None
    assert summary_job["kind"] == "summary"
    assert summary_job["target"] == "1"
    # Its batch is exactly the oldest SESSIONS_PER_SUMMARY sessions.
    items = summary_job["input_obj"]["items"]
    assert len(items) == SESSIONS_PER_SUMMARY
    assert [it["session_id"] for it in items] == [
        f"s-{i}" for i in range(1, SESSIONS_PER_SUMMARY + 1)
    ]
    # First fold has an empty prior_summary.
    assert (summary_job["input_obj"]["prior_summary"] or "") == ""

    # Apply the summary fold -> the single persistent_summary row is created.
    scheduler.submit(summary_job["job_id"], "Persistent summary across the first six.")
    ps = store.get_summary()
    assert ps is not None
    assert ps["summary"] == "Persistent summary across the first six."
    assert ps["session_count"] == SESSIONS_PER_SUMMARY
    assert ps["first_session"] == "s-1"
    assert ps["last_session"] == f"s-{SESSIONS_PER_SUMMARY}"

    # The 6 folded sessions are marked folded=1.
    for i in range(1, SESSIONS_PER_SUMMARY + 1):
        assert store.get_session(f"s-{i}")["folded"] == 1


# --------------------------------------------------------------------------- #
# 7. a further 6 sessions EXTEND the same row (session_count == 12)
# --------------------------------------------------------------------------- #
def test_further_six_sessions_extend_same_persistent_summary(db, config):
    """A second batch of 6 extends the same singleton row; prior_summary flows."""
    _episodic_ddl(db)
    store = DiaryStore(db)
    current = ["s-1"]
    scheduler = DiaryScheduler(
        store, db, DiaryConfig(enabled=True), session=lambda: current[0]
    )

    # First batch: 6 sessions finalize and fold (drive a 7th to finalize the 6th).
    for s in range(1, SESSIONS_PER_SUMMARY + 2):
        sid = f"s-{s}"
        current[0] = sid
        _run_session(db, scheduler, sid, summary=f"Narrative for {sid}.")

    first_job = store.pending_summary_job()
    assert first_job is not None
    scheduler.submit(first_job["job_id"], "Summary v1 (first six).")
    assert store.get_summary()["session_count"] == SESSIONS_PER_SUMMARY

    # Second batch: drive enough further sessions that another full batch of 6
    # finalized-unfolded sessions accumulates (s-7 was already finalized; add up to
    # s-14 so >= 6 finalized-unfolded remain after the first fold).
    for s in range(SESSIONS_PER_SUMMARY + 2, 2 * SESSIONS_PER_SUMMARY + 3):
        sid = f"s-{s}"
        current[0] = sid
        _run_session(db, scheduler, sid, summary=f"Narrative for {sid}.")

    second_job = store.pending_summary_job()
    assert second_job is not None
    # The prior persistent summary is handed back into the extension job.
    assert second_job["input_obj"]["prior_summary"] == "Summary v1 (first six)."
    items = second_job["input_obj"]["items"]
    assert len(items) == SESSIONS_PER_SUMMARY

    scheduler.submit(second_job["job_id"], "Summary v2 (extended).")

    # SAME singleton row, extended in place: count is now 12, text is the v2.
    ps = store.get_summary()
    assert ps["summary"] == "Summary v2 (extended)."
    assert ps["session_count"] == 2 * SESSIONS_PER_SUMMARY  # 12
    assert ps["first_session"] == "s-1"  # preserved across the extension


# --------------------------------------------------------------------------- #
# 8. summary_max_sentences is formatted into the summary prompt
# --------------------------------------------------------------------------- #
def test_summary_max_sentences_formatted_into_summary_prompt(db, config):
    """The fold job's prompt carries the 300-sentence cap (no leftover braces)."""
    _episodic_ddl(db)
    store = DiaryStore(db)
    current = ["s-1"]
    scheduler = DiaryScheduler(
        store, db, DiaryConfig(enabled=True), session=lambda: current[0]
    )

    for s in range(1, SESSIONS_PER_SUMMARY + 2):
        sid = f"s-{s}"
        current[0] = sid
        _run_session(db, scheduler, sid, summary=f"Narrative for {sid}.")

    summary_job = store.pending_summary_job()
    assert summary_job is not None
    prompt = summary_job["prompt"]
    assert f"up to {SUMMARY_MAX_SENTENCES} sentences" in prompt
    assert "{summary_max_sentences}" not in prompt


# --------------------------------------------------------------------------- #
# 9. context injection
# --------------------------------------------------------------------------- #
def test_context_injection_emits_current_previous_and_persistent_summary_no_ids(db_path):
    """assemble emits <diary session="current"> + inject_sessions previous + one
    <persistent_summary>, with no id="..." inside any fragment."""
    mem = NexusMemory(db_path=db_path, diary=DiaryConfig(enabled=True))
    try:
        store = mem._diary.store
        # The provider reads the orchestrator's real session_id as "current".
        current_id = mem.session_id

        with mem.db.lock:
            # A finalized PREVIOUS session with a non-empty summary (drives the
            # previous <diary>). seq 1 (older than the current session).
            mem.db.conn.execute(
                "INSERT INTO diary_sessions "
                "(session_id, seq, summary, covered_through, interaction_count, "
                " finalized, folded, created_at, updated_at) "
                "VALUES ('prev-sess', 1, ?, 0, 6, 1, 0, ?, ?)",
                (
                    "Last session the user planned the release.",
                    "2026-06-10 00:00:00",
                    "2026-06-10 00:00:00",
                ),
            )
            # The CURRENT session row (not finalized) with a non-empty summary,
            # higher seq so previous_finalized_sessions orders it after prev-sess.
            mem.db.conn.execute(
                "INSERT INTO diary_sessions "
                "(session_id, seq, summary, covered_through, interaction_count, "
                " finalized, folded, created_at, updated_at) "
                "VALUES (?, 2, ?, 0, 2, 0, 0, ?, ?)",
                (
                    current_id,
                    "This session we are wiring the diary.",
                    "2026-06-11 00:00:00",
                    "2026-06-11 00:00:00",
                ),
            )
            # The single growing persistent summary (drives <persistent_summary>).
            mem.db.conn.execute(
                "INSERT INTO persistent_summary "
                "(id, summary, session_count, first_session, last_session, updated_at) "
                "VALUES (1, ?, 6, 'old-a', 'old-f', ?)",
                ("An epoch summary across the first six sessions.", "2026-06-11 00:00:00"),
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

        # The current session diary is present, keyed session="current".
        assert '<diary session="current"' in xml
        # The previous finalized session diary is present (inject_sessions=1).
        assert '<diary session="prev-sess"' in xml
        # Exactly one persistent_summary.
        assert "<persistent_summary>" in xml
        assert "</persistent_summary>" in xml
        assert xml.count("<persistent_summary>") == 1

        # inject_sessions=1: exactly two <diary ...> elements (current + 1 previous).
        assert len(re.findall(r"<diary session=", xml)) == 1 + INJECT_SESSIONS

        # No id="..." appears INSIDE any diary / persistent_summary fragment.
        for frag in re.findall(r"<diary .*?</diary>", xml, re.DOTALL):
            assert 'id="' not in frag
        persistent_frag = re.search(
            r"<persistent_summary>.*?</persistent_summary>", xml, re.DOTALL
        ).group(0)
        assert 'id="' not in persistent_frag

        # The needle invariant is preserved: at most top_k <fact id="\d+"> elements.
        assert len(_FACT_NEEDLE_RE.findall(xml)) <= top_k

        # The additive response superset keys are present and well-shaped.
        diary_resp = result["diary"]
        assert isinstance(diary_resp, list)
        # The current session is flagged; the previous one is present too.
        assert any(d.get("current") and d["session"] == current_id for d in diary_resp)
        assert any(d["session"] == "prev-sess" for d in diary_resp)
        # persistent_summary is a single object (not a list).
        assert isinstance(result["persistent_summary"], dict)
        assert result["persistent_summary"]["session_count"] == 6
        assert result["meta"]["session_diary_count"] == 1 + INJECT_SESSIONS
    finally:
        mem.close()


# --------------------------------------------------------------------------- #
# 10. invalid config raises ValueError
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "kwargs",
    [
        {"inject_sessions": -1},  # below the 0..6 range
        {"inject_sessions": 7},   # above the 0..6 range
        {"summary_max_sentences": 1},  # floor is 2
        {"max_sentences": 1},     # floor is 2
        {"update_every": 0},
        {"diary_window": 0},
        {"sessions_per_summary": 0},
    ],
)
def test_invalid_diary_config_raises_value_error(kwargs):
    """__post_init__ validates the knobs regardless of enabled."""
    with pytest.raises(ValueError):
        DiaryConfig(**kwargs)


@pytest.mark.parametrize("k", [0, 6])
def test_inject_sessions_bounds_are_inclusive(k):
    """inject_sessions=0 (inject nothing) and =6 (the max) are both valid."""
    cfg = DiaryConfig(inject_sessions=k)
    assert cfg.inject_sessions == k


# --------------------------------------------------------------------------- #
# 11. persistence across a fresh NexusDB on the same db_path
# --------------------------------------------------------------------------- #
def test_sessions_and_persistent_summary_survive_reopen(config, db_path):
    """diary_sessions + persistent_summary survive a fresh NexusDB on the same path."""
    diary_cfg = DiaryConfig(enabled=True)

    # Session 1: build diary state directly on a NexusDB, then close.
    db1 = NexusDB(config)
    try:
        _episodic_ddl(db1)
        store1 = DiaryStore(db1)
        scheduler1 = DiaryScheduler(store1, db1, diary_cfg, session=lambda: "s-1")
        for i in range(1, N + 1):
            _interaction(db1, scheduler1, "s-1", i)

        # One pending session job + a diary_sessions row exist.
        assert len(store1.pending_jobs()) == 1
        assert store1.get_session("s-1") is not None

        # Also stamp the persistent_summary so both diary tables carry rows.
        folded = [{"session_id": "s-1", "summary": "x"}]
        store1.upsert_summary("A persistent summary.", folded)
        pending_id = store1.pending_jobs()[0]["job_id"]
    finally:
        db1.close()

    # Session 2: a brand-new NexusDB on the SAME file path.
    assert config.db_path == db_path
    db2 = NexusDB(config)
    try:
        store2 = DiaryStore(db2)  # CREATE TABLE IF NOT EXISTS -> finds existing rows.

        # The pending job survived intact.
        jobs = store2.pending_jobs()
        assert len(jobs) == 1
        assert jobs[0]["job_id"] == pending_id
        assert jobs[0]["kind"] == "session"

        # The diary_sessions row survived.
        row = store2.get_session("s-1")
        assert row is not None
        assert row["interaction_count"] == N

        # The persistent_summary singleton survived.
        ps = store2.get_summary()
        assert ps is not None
        assert ps["summary"] == "A persistent summary."
        assert ps["session_count"] == 1
    finally:
        db2.close()


# --------------------------------------------------------------------------- #
# 12. idempotent submit
# --------------------------------------------------------------------------- #
def test_resubmitting_done_job_is_safe_no_op(db, config):
    """Re-submitting a 'done' job is a safe no-op (no raise); unknown id -> not_found."""
    _episodic_ddl(db)
    store = DiaryStore(db)
    scheduler = DiaryScheduler(store, db, DiaryConfig(enabled=True), session=lambda: "s-1")
    sid = "s-1"

    for i in range(1, N + 1):
        _interaction(db, scheduler, sid, i)
    job_id = store.pending_jobs()[0]["job_id"]

    # First submit applies and marks the job done.
    first = scheduler.submit(job_id, "Applied once.")
    assert first["status"] == "success"
    assert store.get_job(job_id)["status"] == "done"

    # Re-submitting the SAME (now done) job is a safe no-op (no raise).
    again = scheduler.submit(job_id, "Should be ignored.")
    assert again["status"] == "success"
    # The stored summary did not change on the redundant submit.
    assert store.get_session(sid)["summary"] == "Applied once."

    # An unknown job id is also a safe no-op, not a raise.
    unknown = scheduler.submit("does-not-exist", "whatever")
    assert unknown["status"] == "not_found"


# --------------------------------------------------------------------------- #
# 13. finalize() finalizes the current session + enqueues a force job (B3)
# --------------------------------------------------------------------------- #
def test_finalize_finalizes_current_session_and_force_enqueues(db, config):
    """finalize() must still enqueue a session job (force=True) despite no new turns."""
    _episodic_ddl(db)
    store = DiaryStore(db)
    scheduler = DiaryScheduler(store, db, DiaryConfig(enabled=True), session=lambda: "s-1")
    sid = "s-1"

    for i in range(1, N + 1):
        _interaction(db, scheduler, sid, i)
    job = store.pending_jobs()[0]
    scheduler.submit(job["job_id"], "Covered narrative.")
    assert store.get_session(sid)["covered_through"] == job["advance_to"]
    assert store.pending_jobs() == []

    # finalize() must still enqueue a session job (force=True), despite no new
    # turns, so the finalized-but-unfolded session is never stranded.
    scheduler.finalize()
    pend = store.pending_jobs()
    assert len(pend) == 1 and pend[0]["kind"] == "session"
    assert store.get_session(sid)["finalized"] == 1
