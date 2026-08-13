import datetime
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from english_coach import history_store

_T1 = datetime.datetime(2026, 6, 1, 10, 0, 0).astimezone()
_T2 = datetime.datetime(2026, 6, 2, 10, 0, 0).astimezone()


def _profile(
    *,
    student: str = "Suzy",
    filename: str = "Suzy_2026-06-08.png",
    overall: float = 15.0,
    writing_evidence: str = "I go yesterday.",
    grammar_filename: str | None = "Suzy_grammar_2026-06-08.png",
    grammar_evidence: str = "a apple",
) -> dict:
    learning_needs = [
        {
            "source_type": "writing",
            "filename": filename,
            "skill_tag": "past_tense",
            "evidence": writing_evidence,
            "suggested_fix": "I went yesterday.",
            "explanation": "yesterday needs past tense.",
        }
    ]
    if grammar_filename is not None:
        learning_needs.append(
            {
                "source_type": "grammar_training",
                "filename": grammar_filename,
                "skill_tag": "articles",
                "evidence": grammar_evidence,
                "suggested_fix": "an apple",
                "explanation": "vowel sound takes an.",
            }
        )
    return {
        "student_name": student,
        "feedback_items": [
            {
                "filename": filename,
                "overall_score": overall,
                "dimensions": {
                    "content": 5,
                    "structure": 4,
                    "language": 3.5,
                    "handwriting": 4,
                },
                "prompt_summary": "Holiday plan.",
                "strengths": ["clear structure"],
                "improvements": ["watch tenses"],
            }
        ],
        "learning_needs": learning_needs,
    }


class HistoryStoreTestBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = Path(tmp.name) / "data" / "learnmate.db"

    def _record(self, profile: dict, *, at: datetime.datetime, report: str, training: str):
        history_store.record_student_profile(
            profile,
            report_path=Path(report),
            training_path=Path(training),
            recorded_at=at,
            db_path=self.db_path,
        )

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def _seed_legacy_duplicates(self):
        """A pre-idempotency DB: no unique index, same essay recorded three times."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(history_store._SCHEMA)
            conn.execute(
                "INSERT INTO students (id, name, first_seen_at, last_seen_at)"
                " VALUES (1, 'Suzy', ?, ?)",
                (_T1.isoformat(), _T2.isoformat()),
            )
            rows = [
                (10, _T1.isoformat(), 10.0, "run1.md"),
                (11, _T2.isoformat(), 11.0, "run2.md"),
                # Same recorded_at as id 11: the id tiebreaker must prefer 12.
                (12, _T2.isoformat(), 12.0, "run3.md"),
            ]
            for row_id, recorded_at, score, report in rows:
                conn.execute(
                    "INSERT INTO submissions (id, student_id, category, filename,"
                    " recorded_at, overall_score, report_path, training_json_path)"
                    " VALUES (?, 1, 'writing', 'essay.png', ?, ?, ?, 'a.json')",
                    (row_id, recorded_at, score, report),
                )
                conn.execute(
                    "INSERT INTO learning_needs (student_id, submission_id, skill_tag,"
                    " evidence, recorded_at) VALUES (1, ?, 'past_tense', ?, ?)",
                    (row_id, f"evidence-of-{row_id}", recorded_at),
                )
            conn.commit()
        finally:
            conn.close()


class RecordStudentProfileTest(HistoryStoreTestBase):
    def test_rerecording_same_essay_replaces_instead_of_appending(self):
        self._record(
            _profile(overall=12.0, writing_evidence="first-run evidence"),
            at=_T1,
            report="first.md",
            training="first.json",
        )
        self._record(
            _profile(
                overall=16.5,
                writing_evidence="second-run evidence",
                grammar_evidence="second-run grammar evidence",
            ),
            at=_T2,
            report="second.md",
            training="second.json",
        )

        submissions = self._query("SELECT * FROM submissions ORDER BY category")
        self.assertEqual(
            [row["category"] for row in submissions], ["grammar_training", "writing"]
        )
        writing = submissions[1]
        self.assertEqual(writing["overall_score"], 16.5)
        self.assertEqual(writing["report_path"], "second.md")
        self.assertEqual(writing["recorded_at"], _T2.isoformat())

        needs = self._query("SELECT evidence FROM learning_needs")
        evidences = {row["evidence"] for row in needs}
        self.assertEqual(
            evidences, {"second-run evidence", "second-run grammar evidence"}
        )

    def test_different_files_and_categories_stay_separate(self):
        self._record(
            _profile(filename="Suzy_2026-06-08.png"),
            at=_T1,
            report="a.md",
            training="a.json",
        )
        self._record(
            _profile(filename="Suzy_2026-06-09.png"),
            at=_T2,
            report="b.md",
            training="b.json",
        )

        rows = self._query(
            "SELECT category, filename FROM submissions ORDER BY category, filename"
        )
        self.assertEqual(
            [(row["category"], row["filename"]) for row in rows],
            [
                ("grammar_training", "Suzy_grammar_2026-06-08.png"),
                ("writing", "Suzy_2026-06-08.png"),
                ("writing", "Suzy_2026-06-09.png"),
            ],
        )

    def test_rerecording_against_legacy_duplicates_updates_newest_row(self):
        self._seed_legacy_duplicates()

        later = datetime.datetime(2026, 6, 3, 10, 0, 0).astimezone()
        self._record(
            _profile(
                filename="essay.png",
                overall=18.0,
                writing_evidence="fresh evidence",
                grammar_filename=None,
            ),
            at=later,
            report="rerun.md",
            training="rerun.json",
        )

        rows = self._query(
            "SELECT id, overall_score FROM submissions WHERE filename = 'essay.png'"
            " ORDER BY id"
        )
        self.assertEqual(len(rows), 3)
        by_id = {row["id"]: row["overall_score"] for row in rows}
        self.assertEqual(by_id[10], 10.0)
        self.assertEqual(by_id[11], 11.0)
        self.assertEqual(by_id[12], 18.0)

        needs = self._query(
            "SELECT submission_id, evidence FROM learning_needs ORDER BY submission_id"
        )
        by_submission = {row["submission_id"]: row["evidence"] for row in needs}
        self.assertEqual(by_submission[10], "evidence-of-10")
        self.assertEqual(by_submission[11], "evidence-of-11")
        self.assertEqual(by_submission[12], "fresh evidence")

    def test_fresh_db_has_unique_index_blocking_raw_duplicates(self):
        conn = history_store._connect(self.db_path)
        try:
            index_names = {
                row["name"] for row in conn.execute("PRAGMA index_list(submissions)")
            }
            self.assertIn("idx_submissions_identity", index_names)
            conn.execute(
                "INSERT INTO students (id, name, first_seen_at, last_seen_at)"
                " VALUES (1, 'Suzy', 'x', 'x')"
            )
            insert = (
                "INSERT INTO submissions (student_id, category, filename, recorded_at)"
                " VALUES (1, 'writing', 'essay.png', 'x')"
            )
            conn.execute(insert)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(insert)
        finally:
            conn.close()

    def test_legacy_db_with_duplicates_still_connects_and_reads(self):
        self._seed_legacy_duplicates()

        students = history_store.list_students(self.db_path)
        self.assertEqual(students[0]["name"], "Suzy")
        self.assertEqual(students[0]["submission_count"], 3)

        conn = history_store._connect(self.db_path)
        try:
            index_names = {
                row["name"] for row in conn.execute("PRAGMA index_list(submissions)")
            }
            self.assertNotIn("idx_submissions_identity", index_names)
        finally:
            conn.close()


class DedupeHistoryTest(HistoryStoreTestBase):
    def _seed_polluted_db(self):
        self._seed_legacy_duplicates()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO students (id, name, first_seen_at, last_seen_at)"
                " VALUES (2, 'Eve', ?, ?)",
                (_T1.isoformat(), _T1.isoformat()),
            )
            # A row written by the old, unisolated unit tests.
            conn.execute(
                "INSERT INTO submissions (id, student_id, category, filename,"
                " recorded_at, report_path, training_json_path)"
                " VALUES (20, 2, 'writing', 'Eve_writing.png', ?,"
                " '/var/folders/k_/tmpabc/reports/Eve_2026-07-17.md', 't.json')",
                (_T1.isoformat(),),
            )
            conn.execute(
                "INSERT INTO learning_needs (student_id, submission_id, skill_tag,"
                " evidence, recorded_at) VALUES (2, 20, 'articles', 'test-fixture-need', ?)",
                (_T1.isoformat(),),
            )
            # A healthy row that must survive untouched.
            conn.execute(
                "INSERT INTO submissions (id, student_id, category, filename,"
                " recorded_at, report_path, training_json_path)"
                " VALUES (30, 2, 'writing', 'Eve_2026-06-13.png', ?,"
                " '/Users/mac/project/reports/Eve.md', 't.json')",
                (_T1.isoformat(),),
            )
            # An orphan need that must never be deleted.
            conn.execute(
                "INSERT INTO learning_needs (student_id, submission_id, skill_tag,"
                " evidence, recorded_at) VALUES (2, NULL, 'other', 'orphan-need', ?)",
                (_T1.isoformat(),),
            )
            conn.commit()
        finally:
            conn.close()

    def test_dry_run_reports_without_deleting(self):
        self._seed_polluted_db()

        result = history_store.dedupe_history(self.db_path)

        self.assertFalse(result.applied)
        self.assertIsNone(result.backup_path)
        self.assertEqual(len(result.groups), 1)
        self.assertEqual(result.groups[0]["keep_id"], 12)
        self.assertEqual(result.groups[0]["drop_ids"], [11, 10])
        self.assertEqual(result.duplicate_need_count, 2)
        self.assertEqual(len(result.test_fixture_rows), 1)
        self.assertEqual(result.test_fixture_rows[0]["submission_id"], 20)
        self.assertEqual(result.orphan_need_count, 1)
        self.assertIn("Dry run", str(result))

        self.assertEqual(len(self._query("SELECT id FROM submissions")), 5)
        self.assertEqual(len(self._query("SELECT id FROM learning_needs")), 5)
        self.assertEqual(list(self.db_path.parent.glob("*backup*")), [])

    def test_apply_keeps_newest_purges_fixtures_and_creates_index(self):
        self._seed_polluted_db()

        result = history_store.dedupe_history(self.db_path, apply=True)

        self.assertTrue(result.applied)
        self.assertTrue(result.index_created)
        self.assertIn("Deleted", str(result))

        submission_ids = {row["id"] for row in self._query("SELECT id FROM submissions")}
        self.assertEqual(submission_ids, {12, 30})
        evidences = {row["evidence"] for row in self._query("SELECT evidence FROM learning_needs")}
        self.assertEqual(evidences, {"evidence-of-12", "orphan-need"})

        backup = sqlite3.connect(result.backup_path)
        try:
            backup_count = backup.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
        finally:
            backup.close()
        self.assertEqual(backup_count, 5)

        conn = sqlite3.connect(self.db_path)
        try:
            index_names = {
                row[1] for row in conn.execute("PRAGMA index_list(submissions)")
            }
        finally:
            conn.close()
        self.assertIn("idx_submissions_identity", index_names)

    def test_apply_twice_is_idempotent(self):
        self._seed_polluted_db()
        history_store.dedupe_history(self.db_path, apply=True)

        result = history_store.dedupe_history(self.db_path, apply=True)

        self.assertTrue(result.applied)
        self.assertEqual(result.groups, [])
        self.assertEqual(result.test_fixture_rows, [])
        self.assertEqual(len(self._query("SELECT id FROM submissions")), 2)

    def test_missing_db_is_a_noop(self):
        result = history_store.dedupe_history(self.db_path)

        self.assertEqual(result.groups, [])
        self.assertFalse(self.db_path.exists())


class DataRootResolutionTest(unittest.TestCase):
    def test_unset_uses_package_dir(self):
        with mock.patch.dict(os.environ):
            os.environ.pop(history_store.DATA_ROOT_ENV, None)

            self.assertIsNone(history_store.data_root())
            self.assertEqual(
                history_store.resolve_db_path(),
                history_store.PACKAGE_DIR / "data" / "learnmate.db",
            )

    def test_blank_value_is_ignored(self):
        with mock.patch.dict(os.environ, {history_store.DATA_ROOT_ENV: "   "}):
            self.assertIsNone(history_store.data_root())

    def test_set_redirects_the_whole_workspace(self):
        with mock.patch.dict(
            os.environ, {history_store.DATA_ROOT_ENV: "/tmp/learnmate-sandbox"}
        ):
            root = Path("/tmp/learnmate-sandbox")
            self.assertEqual(history_store.data_root(), root)
            self.assertEqual(
                history_store.resolve_db_path(), root / "data" / "learnmate.db"
            )
            self.assertEqual(history_store.resolve_reports_dir(), root / "reports")
            self.assertEqual(
                history_store.resolve_training_inputs_dir(), root / "training_inputs"
            )
            self.assertEqual(history_store.resolve_writing_inputs_dir(), root / "input")


if __name__ == "__main__":
    unittest.main()
