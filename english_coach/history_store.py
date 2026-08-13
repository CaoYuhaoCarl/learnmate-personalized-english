"""Local SQLite history of student scores, mistakes, and reasons.

Reads/writes plain dicts shaped like ``StudentLearningProfile.model_dump(mode="json")``
(the same shape already written to ``training_inputs/*.json``) rather than importing the
Pydantic models from ``agent.py``, to avoid a circular import between the two modules.

Set ``LEARNMATE_DATA_ROOT`` to point the whole data workspace (``data/`` DB, ``reports/``,
``training_inputs/``, ``input/``) at a sandbox directory, so test runs never touch the
real student records.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

PACKAGE_DIR = Path(__file__).parent
DATA_ROOT_ENV = "LEARNMATE_DATA_ROOT"


def data_root() -> Path | None:
    """The sandbox workspace root, or None when running against the real data."""
    raw = os.environ.get(DATA_ROOT_ENV, "").strip()
    return Path(raw).expanduser() if raw else None


def _base_dir() -> Path:
    return data_root() or PACKAGE_DIR


def resolve_db_path() -> Path:
    return _base_dir() / "data" / "learnmate.db"


def resolve_reports_dir() -> Path:
    return _base_dir() / "reports"


def resolve_training_inputs_dir() -> Path:
    return _base_dir() / "training_inputs"


def resolve_writing_inputs_dir() -> Path:
    return _base_dir() / "input"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS submissions (
  id INTEGER PRIMARY KEY,
  student_id INTEGER NOT NULL REFERENCES students(id),
  category TEXT NOT NULL,
  filename TEXT NOT NULL,
  submission_date TEXT,
  recorded_at TEXT NOT NULL,
  overall_score REAL,
  content_score INTEGER,
  structure_score INTEGER,
  language_score REAL,
  handwriting_score INTEGER,
  prompt_summary TEXT,
  strengths_json TEXT,
  improvements_json TEXT,
  report_path TEXT,
  training_json_path TEXT
);

CREATE TABLE IF NOT EXISTS learning_needs (
  id INTEGER PRIMARY KEY,
  student_id INTEGER NOT NULL REFERENCES students(id),
  submission_id INTEGER REFERENCES submissions(id),
  source_type TEXT,
  skill_tag TEXT,
  evidence TEXT,
  suggested_fix TEXT,
  explanation TEXT,
  recorded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_submissions_student ON submissions(student_id);
CREATE INDEX IF NOT EXISTS idx_learning_needs_student ON learning_needs(student_id);
"""

# One row per essay: re-running the same file replaces the record instead of
# appending a duplicate (see _upsert_submission).
_SUBMISSION_IDENTITY_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_submissions_identity "
    "ON submissions(student_id, category, filename)"
)

_FILENAME_DATE_RE = re.compile(r"_(\d{4}-\d{2}-\d{2})(?:_[\d-]+)?\.[A-Za-z0-9]+$")


_STUDENT_COLUMNS = {
    "external_id": "INTEGER",
    "external_class": "TEXT",
}


def _migrate_student_columns(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(students)")}
    for column, sql_type in _STUDENT_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE students ADD COLUMN {column} {sql_type}")


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    if db_path is None:
        db_path = resolve_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _migrate_student_columns(conn)
    try:
        conn.execute(_SUBMISSION_IDENTITY_INDEX)
    except (sqlite3.IntegrityError, sqlite3.OperationalError):
        # Legacy duplicates block the unique index; `dedupe --apply` cleans them
        # up and creates it for real.
        pass
    return conn


def _date_from_filename(filename: str) -> str | None:
    match = _FILENAME_DATE_RE.search(filename)
    return match.group(1) if match else None


def _upsert_student(conn: sqlite3.Connection, name: str, *, at: str) -> int:
    conn.execute(
        """
        INSERT INTO students (name, first_seen_at, last_seen_at)
        VALUES (?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
          last_seen_at = excluded.last_seen_at
        WHERE excluded.last_seen_at > students.last_seen_at
        """,
        (name, at, at),
    )
    row = conn.execute("SELECT id FROM students WHERE name = ?", (name,)).fetchone()
    return row["id"]


def _upsert_submission(
    conn: sqlite3.Connection,
    *,
    student_id: int,
    category: str,
    filename: str,
    submission_date: str | None,
    recorded_at: str,
    report_path: str,
    training_json_path: str,
    overall_score: float | None = None,
    content_score: int | None = None,
    structure_score: int | None = None,
    language_score: float | None = None,
    handwriting_score: int | None = None,
    prompt_summary: str | None = None,
    strengths_json: str | None = None,
    improvements_json: str | None = None,
) -> int:
    """Insert or replace the submission identified by (student, category, filename).

    Replace semantics: the latest run wins. On replace, the row's previous
    learning needs are deleted so the caller can insert the fresh set. Against
    legacy duplicates (pre-unique-index rows) the newest row is targeted; the
    older ones are left for `dedupe` to clean up.
    """
    existing = conn.execute(
        """
        SELECT id FROM submissions
        WHERE student_id = ? AND category = ? AND filename = ?
        ORDER BY recorded_at DESC, id DESC
        LIMIT 1
        """,
        (student_id, category, filename),
    ).fetchone()
    values = (
        submission_date,
        recorded_at,
        overall_score,
        content_score,
        structure_score,
        language_score,
        handwriting_score,
        prompt_summary,
        strengths_json,
        improvements_json,
        report_path,
        training_json_path,
    )
    if existing is None:
        cursor = conn.execute(
            """
            INSERT INTO submissions (
              student_id, category, filename, submission_date, recorded_at,
              overall_score, content_score, structure_score, language_score,
              handwriting_score, prompt_summary, strengths_json,
              improvements_json, report_path, training_json_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (student_id, category, filename, *values),
        )
        return cursor.lastrowid

    submission_id = existing["id"]
    conn.execute(
        """
        UPDATE submissions SET
          submission_date = ?, recorded_at = ?, overall_score = ?,
          content_score = ?, structure_score = ?, language_score = ?,
          handwriting_score = ?, prompt_summary = ?, strengths_json = ?,
          improvements_json = ?, report_path = ?, training_json_path = ?
        WHERE id = ?
        """,
        (*values, submission_id),
    )
    conn.execute("DELETE FROM learning_needs WHERE submission_id = ?", (submission_id,))
    return submission_id


def record_student_profile(
    profile: dict[str, Any],
    *,
    report_path: Path,
    training_path: Path,
    recorded_at: datetime.datetime,
    db_path: Path | None = None,
) -> None:
    """Upsert one processed StudentLearningProfile dict into the history DB."""
    student_name = profile.get("student_name") or "unknown"
    if student_name == "unknown":
        return

    recorded_at_text = recorded_at.isoformat()
    conn = _connect(db_path)
    try:
        with conn:
            student_id = _upsert_student(conn, student_name, at=recorded_at_text)
            submission_ids: dict[str, int] = {}

            for feedback in profile.get("feedback_items", []):
                filename = feedback["filename"]
                dimensions = feedback.get("dimensions") or {}
                submission_ids[filename] = _upsert_submission(
                    conn,
                    student_id=student_id,
                    category="writing",
                    filename=filename,
                    submission_date=_date_from_filename(filename),
                    recorded_at=recorded_at_text,
                    overall_score=feedback.get("overall_score"),
                    content_score=dimensions.get("content"),
                    structure_score=dimensions.get("structure"),
                    language_score=dimensions.get("language"),
                    handwriting_score=dimensions.get("handwriting"),
                    prompt_summary=feedback.get("prompt_summary"),
                    strengths_json=json.dumps(
                        feedback.get("strengths", []), ensure_ascii=False
                    ),
                    improvements_json=json.dumps(
                        feedback.get("improvements", []), ensure_ascii=False
                    ),
                    report_path=str(report_path),
                    training_json_path=str(training_path),
                )

            grammar_filenames = {
                need["filename"]
                for need in profile.get("learning_needs", [])
                if need.get("source_type") == "grammar_training" and need.get("filename")
            }
            for filename in sorted(grammar_filenames - submission_ids.keys()):
                submission_ids[filename] = _upsert_submission(
                    conn,
                    student_id=student_id,
                    category="grammar_training",
                    filename=filename,
                    submission_date=_date_from_filename(filename),
                    recorded_at=recorded_at_text,
                    report_path=str(report_path),
                    training_json_path=str(training_path),
                )

            for need in profile.get("learning_needs", []):
                conn.execute(
                    """
                    INSERT INTO learning_needs (
                      student_id, submission_id, source_type, skill_tag,
                      evidence, suggested_fix, explanation, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        student_id,
                        submission_ids.get(need.get("filename")),
                        need.get("source_type"),
                        need.get("skill_tag"),
                        need.get("evidence"),
                        need.get("suggested_fix"),
                        need.get("explanation"),
                        recorded_at_text,
                    ),
                )
    finally:
        conn.close()


def list_students(db_path: Path | None = None) -> list[dict[str, Any]]:
    if db_path is None:
        db_path = resolve_db_path()
    if not db_path.exists():
        return []
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT
              s.name AS name,
              s.external_id AS external_id,
              s.external_class AS external_class,
              s.last_seen_at AS last_seen_at,
              COUNT(sub.id) AS submission_count,
              AVG(sub.overall_score) AS avg_overall_score
            FROM students s
            LEFT JOIN submissions sub ON sub.student_id = s.id
            GROUP BY s.id
            ORDER BY s.last_seen_at DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_student_history(name: str, db_path: Path | None = None) -> dict[str, Any] | None:
    if db_path is None:
        db_path = resolve_db_path()
    if not db_path.exists():
        return None
    conn = _connect(db_path)
    try:
        student = conn.execute(
            """
            SELECT id, name, external_id, external_class, first_seen_at, last_seen_at
            FROM students WHERE name = ?
            """,
            (name,),
        ).fetchone()
        if student is None:
            return None

        submissions = conn.execute(
            """
            SELECT category, filename, submission_date, recorded_at, overall_score,
                   content_score, structure_score, language_score, handwriting_score,
                   prompt_summary, report_path
            FROM submissions
            WHERE student_id = ?
            ORDER BY COALESCE(submission_date, recorded_at) ASC, recorded_at ASC
            """,
            (student["id"],),
        ).fetchall()

        needs = conn.execute(
            """
            SELECT skill_tag, evidence, suggested_fix, explanation, source_type, recorded_at
            FROM learning_needs
            WHERE student_id = ?
            ORDER BY recorded_at DESC
            """,
            (student["id"],),
        ).fetchall()

        grouped: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"count": 0, "examples": []}
        )
        for need in needs:
            tag = need["skill_tag"] or "other"
            bucket = grouped[tag]
            bucket["count"] += 1
            if len(bucket["examples"]) < 3:
                bucket["examples"].append(dict(need))

        mistakes_by_skill = [
            {"skill_tag": tag, **data}
            for tag, data in sorted(
                grouped.items(), key=lambda item: item[1]["count"], reverse=True
            )
        ]

        return {
            "name": student["name"],
            "external_id": student["external_id"],
            "external_class": student["external_class"],
            "first_seen_at": student["first_seen_at"],
            "last_seen_at": student["last_seen_at"],
            "submissions": [dict(row) for row in submissions],
            "mistakes_by_skill": mistakes_by_skill,
        }
    finally:
        conn.close()


def backfill_from_training_inputs(
    training_inputs_dir: Path | None = None,
    reports_dir: Path | None = None,
    db_path: Path | None = None,
) -> int:
    """One-time import of existing training_inputs/*.json files into the history DB."""
    if training_inputs_dir is None:
        training_inputs_dir = resolve_training_inputs_dir()
    if reports_dir is None:
        reports_dir = resolve_reports_dir()
    if not training_inputs_dir.is_dir():
        return 0

    count = 0
    for json_path in sorted(training_inputs_dir.glob("*.json")):
        try:
            profile = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        report_path = reports_dir / f"{json_path.stem}.md"
        recorded_at = datetime.datetime.fromtimestamp(
            json_path.stat().st_mtime
        ).astimezone()
        record_student_profile(
            profile,
            report_path=report_path,
            training_path=json_path,
            recorded_at=recorded_at,
            db_path=db_path,
        )
        count += 1
    return count


# Human-judgment calls, not a generic fuzzy-matching algorithm: these three pairs were
# confirmed to be the same real student with a spelling difference, and these two pairs
# were confirmed to be the same real student split into two rows in our own DB.
_ROSTER_NAME_ALIASES = {"bobby": "bobbyd", "hanson": "henson", "secnow": "secvow"}
_ROSTER_MERGE_INTO = {"ken": "Ken", "secnow": "Secvow"}


class ApplyRosterResult:
    def __init__(self) -> None:
        self.merged: list[tuple[str, str]] = []
        self.matched: list[tuple[str, int, str]] = []
        self.unmatched: list[str] = []

    def __str__(self) -> str:
        lines = [
            f"Merged {len(self.merged)} duplicate row(s):",
            *(f"  {loser} -> {survivor}" for loser, survivor in self.merged),
            f"Matched {len(self.matched)} student(s) to a roster id:",
            *(
                f"  {name} -> id={ext_id} class={ext_class}"
                for name, ext_id, ext_class in self.matched
            ),
            f"Unmatched ({len(self.unmatched)}): {', '.join(self.unmatched)}",
        ]
        return "\n".join(lines)


def _merge_student_rows(conn: sqlite3.Connection, loser_name: str, survivor_name: str) -> bool:
    loser = conn.execute(
        "SELECT id FROM students WHERE name = ?", (loser_name,)
    ).fetchone()
    survivor = conn.execute(
        "SELECT id FROM students WHERE name = ?", (survivor_name,)
    ).fetchone()
    if loser is None or survivor is None or loser["id"] == survivor["id"]:
        return False

    conn.execute(
        "UPDATE submissions SET student_id = ? WHERE student_id = ?",
        (survivor["id"], loser["id"]),
    )
    conn.execute(
        "UPDATE learning_needs SET student_id = ? WHERE student_id = ?",
        (survivor["id"], loser["id"]),
    )
    conn.execute(
        """
        UPDATE students SET
          first_seen_at = (SELECT MIN(first_seen_at) FROM students WHERE id IN (?, ?)),
          last_seen_at = (SELECT MAX(last_seen_at) FROM students WHERE id IN (?, ?))
        WHERE id = ?
        """,
        (survivor["id"], loser["id"], survivor["id"], loser["id"], survivor["id"]),
    )
    conn.execute("DELETE FROM students WHERE id = ?", (loser["id"],))
    return True


def apply_roster(csv_path: Path, db_path: Path | None = None) -> ApplyRosterResult:
    """One-time attach of each student's external roster id, resolving duplicate
    roster names by picking the largest id (joined training most recently)."""
    import csv as csv_module

    by_lower_name: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
    with csv_path.open(newline="", encoding="utf-8") as f:
        for row in csv_module.DictReader(f):
            by_lower_name[row["name"].strip().lower()].append(
                (int(row["id"]), row["name"], row["class"])
            )

    result = ApplyRosterResult()
    conn = _connect(db_path)
    try:
        with conn:
            for loser, survivor in _ROSTER_MERGE_INTO.items():
                loser_row = conn.execute(
                    "SELECT name FROM students WHERE LOWER(name) = ? AND name != ?",
                    (loser, survivor),
                ).fetchone()
                if loser_row is not None and _merge_student_rows(
                    conn, loser_row["name"], survivor
                ):
                    result.merged.append((loser_row["name"], survivor))

            students = conn.execute("SELECT id, name FROM students").fetchall()
            for student in students:
                key = student["name"].strip().lower()
                key = _ROSTER_NAME_ALIASES.get(key, key)
                candidates = by_lower_name.get(key, [])
                if not candidates:
                    result.unmatched.append(student["name"])
                    continue
                ext_id, _ext_name, ext_class = max(candidates, key=lambda c: c[0])
                conn.execute(
                    "UPDATE students SET external_id = ?, external_class = ? WHERE id = ?",
                    (ext_id, ext_class, student["id"]),
                )
                result.matched.append((student["name"], ext_id, ext_class))
    finally:
        conn.close()
    return result


# Rows written by unit tests before test isolation existed: their report_path
# points into a temp dir, which no real run ever does (real paths live under the
# project or the deploy volume).
_TEST_FIXTURE_PATH_PATTERNS = ("/var/folders/%", "/tmp/%", "/private/%")


class DedupeResult:
    def __init__(self) -> None:
        # Each group: student_name, category, filename, keep_id, drop_ids, need_count.
        self.groups: list[dict[str, Any]] = []
        # Each row: student_name, category, filename, submission_id, report_path,
        # need_count.
        self.test_fixture_rows: list[dict[str, Any]] = []
        self.orphan_need_count = 0
        self.backup_path: Path | None = None
        self.index_created = False
        self.applied = False

    @property
    def duplicate_submission_count(self) -> int:
        return sum(len(group["drop_ids"]) for group in self.groups)

    @property
    def duplicate_need_count(self) -> int:
        return sum(group["need_count"] for group in self.groups)

    @property
    def test_fixture_need_count(self) -> int:
        return sum(row["need_count"] for row in self.test_fixture_rows)

    def __str__(self) -> str:
        verb = "Deleted" if self.applied else "Would delete"
        lines = [
            f"{verb} {self.duplicate_submission_count} duplicate submission row(s) "
            f"and {self.duplicate_need_count} of their learning need(s) "
            f"across {len(self.groups)} group(s), keeping the newest per "
            "(student, category, filename):"
        ]
        for group in self.groups:
            drop_text = ", ".join(str(drop_id) for drop_id in group["drop_ids"])
            lines.append(
                f"  {group['student_name']} / {group['category']} / {group['filename']}: "
                f"keep id {group['keep_id']}, delete id(s) {drop_text} "
                f"({group['need_count']} need(s))"
            )
        lines.append(
            f"{verb} {len(self.test_fixture_rows)} test-fixture submission row(s) "
            f"(report_path in a temp dir) and {self.test_fixture_need_count} "
            "of their learning need(s):"
        )
        for row in self.test_fixture_rows:
            lines.append(
                f"  {row['student_name']} / {row['category']} / {row['filename']} "
                f"(id {row['submission_id']}): {row['report_path']}"
            )
        lines.append(f"Orphan learning needs (no submission link, kept): {self.orphan_need_count}")
        if self.applied:
            lines.append(f"Backed up database to {self.backup_path}")
            if self.index_created:
                lines.append("Created unique index idx_submissions_identity.")
        elif not self.groups and not self.test_fixture_rows:
            lines.append("Nothing to clean up.")
        else:
            lines.append("Dry run: nothing deleted. Re-run with --apply to clean up.")
        return "\n".join(lines)


def _compute_dedupe_plan(conn: sqlite3.Connection, result: DedupeResult) -> None:
    rows = conn.execute(
        """
        SELECT sub.id, sub.student_id, st.name AS student_name, sub.category,
               sub.filename, sub.report_path
        FROM submissions sub JOIN students st ON st.id = sub.student_id
        ORDER BY st.name, sub.category, sub.filename,
                 sub.recorded_at DESC, sub.id DESC
        """
    ).fetchall()

    def need_count(submission_ids: list[int]) -> int:
        if not submission_ids:
            return 0
        placeholders = ", ".join("?" for _ in submission_ids)
        return conn.execute(
            f"SELECT COUNT(*) AS n FROM learning_needs WHERE submission_id IN ({placeholders})",
            submission_ids,
        ).fetchone()["n"]

    grouped: dict[tuple[int, str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[(row["student_id"], row["category"], row["filename"])].append(row)

    dropped_ids: set[int] = set()
    for members in grouped.values():
        if len(members) < 2:
            continue
        keeper, *losers = members
        drop_ids = [loser["id"] for loser in losers]
        dropped_ids.update(drop_ids)
        result.groups.append(
            {
                "student_name": keeper["student_name"],
                "category": keeper["category"],
                "filename": keeper["filename"],
                "keep_id": keeper["id"],
                "drop_ids": drop_ids,
                "need_count": need_count(drop_ids),
            }
        )

    fixture_clause = " OR ".join(
        "sub.report_path LIKE ?" for _ in _TEST_FIXTURE_PATH_PATTERNS
    )
    for row in conn.execute(
        f"""
        SELECT sub.id, st.name AS student_name, sub.category, sub.filename,
               sub.report_path
        FROM submissions sub JOIN students st ON st.id = sub.student_id
        WHERE {fixture_clause}
        ORDER BY st.name, sub.category, sub.filename, sub.id
        """,
        _TEST_FIXTURE_PATH_PATTERNS,
    ).fetchall():
        if row["id"] in dropped_ids:
            continue
        result.test_fixture_rows.append(
            {
                "student_name": row["student_name"],
                "category": row["category"],
                "filename": row["filename"],
                "submission_id": row["id"],
                "report_path": row["report_path"],
                "need_count": need_count([row["id"]]),
            }
        )

    result.orphan_need_count = conn.execute(
        "SELECT COUNT(*) AS n FROM learning_needs WHERE submission_id IS NULL"
    ).fetchone()["n"]


def _backup_db(db_path: Path) -> Path:
    """WAL-safe file backup via the sqlite3 backup API (a plain copy can lose pages)."""
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = db_path.with_name(f"{db_path.stem}-backup-{timestamp}.db")
    source = sqlite3.connect(db_path)
    try:
        destination = sqlite3.connect(backup_path)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    return backup_path


def dedupe_history(db_path: Path | None = None, *, apply: bool = False) -> DedupeResult:
    """Remove legacy duplicate submissions (keeping the newest per identity key)
    and test-fixture rows, then create the unique identity index.

    Dry run by default; ``apply=True`` backs up the DB file first and performs
    everything in one transaction. Orphan learning needs are never deleted."""
    if db_path is None:
        db_path = resolve_db_path()

    result = DedupeResult()
    if not db_path.exists():
        return result

    if apply:
        result.backup_path = _backup_db(db_path)

    conn = _connect(db_path)
    try:
        if not apply:
            _compute_dedupe_plan(conn, result)
            return result
        with conn:
            # Recompute inside the write transaction so a concurrent write
            # between planning and deleting cannot be caught in the deletion.
            _compute_dedupe_plan(conn, result)
            doomed_ids = [
                drop_id for group in result.groups for drop_id in group["drop_ids"]
            ] + [row["submission_id"] for row in result.test_fixture_rows]
            if doomed_ids:
                placeholders = ", ".join("?" for _ in doomed_ids)
                conn.execute(
                    f"DELETE FROM learning_needs WHERE submission_id IN ({placeholders})",
                    doomed_ids,
                )
                conn.execute(
                    f"DELETE FROM submissions WHERE id IN ({placeholders})",
                    doomed_ids,
                )
            # Unlike the best-effort attempt in _connect, failure here must roll
            # back the whole cleanup.
            conn.execute(_SUBMISSION_IDENTITY_INDEX)
        result.applied = True
        result.index_created = True
        return result
    finally:
        conn.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "backfill", help="Import existing training_inputs/*.json files into the history DB."
    )
    apply_roster_parser = subparsers.add_parser(
        "apply-roster",
        help="Attach each student's external roster id from a students CSV export.",
    )
    apply_roster_parser.add_argument(
        "csv_path", type=Path, help="Path to the roster CSV (id,name,class,...)."
    )
    dedupe_parser = subparsers.add_parser(
        "dedupe",
        help="Remove duplicate submissions kept from before idempotent recording "
        "(dry run unless --apply).",
    )
    dedupe_parser.add_argument(
        "--apply",
        action="store_true",
        help="Back up the DB file, delete the duplicates, and create the unique index.",
    )
    dedupe_parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Database file to clean (defaults to the active data root's DB).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "backfill":
        count = backfill_from_training_inputs()
        print(f"Imported {count} training input file(s) into {resolve_db_path()}")
        return 0
    if args.command == "apply-roster":
        result = apply_roster(args.csv_path)
        print(result)
        return 0
    if args.command == "dedupe":
        print(dedupe_history(args.db, apply=args.apply))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
