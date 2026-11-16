from __future__ import annotations

import configparser
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def _config(database: Path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database.as_posix()}")
    parser = configparser.ConfigParser(interpolation=None)
    with ALEMBIC_INI.open(encoding="utf-8") as handle:
        parser.read_file(handle)
    config = Config()
    config.config_file_name = "alembic.ini"
    config.__dict__["file_config"] = parser
    for section in parser.sections():
        for key, value in parser.items(section):
            config.set_section_option(section, key, value)
    config.set_main_option("script_location", "migrations")
    return config


def test_migration_merges_format_only_duplicates_and_backfills_pathless_fingerprints(
    tmp_path, monkeypatch
):
    database = tmp_path / "fingerprints.db"
    config = _config(database, monkeypatch)
    command.upgrade(config, "2026_11_13")
    connection = sqlite3.connect(database)
    connection.execute("INSERT INTO users (username, password_hash, role) VALUES ('demo', 'x', 'student')")
    stems = [
        "(2026秋) 如图，棱长均为 $2$（ ） A、$4$",
        "（2026秋）如图，棱长均为 $2$（ ） A、$4$",
        "(2026秋) 如图，棱长均为 2（ ） A、4",
    ]
    for index, stem in enumerate(stems, 1):
        connection.execute(
            """INSERT INTO canonical_questions (
                stem_markdown, normalized_stem, normalized_hash,
                analysis_markdown, answer, knowledge_points, tags, origin, validation_state
            ) VALUES (?, ?, ?, '解析', '答案', '[]', '[]', 'student', 'machine_validated')""",
            (stem, stem, f"old-hash-{index}"),
        )
        connection.execute(
            """INSERT INTO questions (
                user_id, canonical_id, private_image_path, content_markdown,
                answer, knowledge_points, tags, difficulty, source,
                reps, ease, interval_days, image_hash, subject
            ) VALUES (
                1, ?, ?, '题干', '答案', '[]', '[]', 'medium', 'ai',
                0, 2.5, 0, 'same-source-sha256', 'math'
            )""",
            (index, f"/private/student-{index}.jpg"),
        )
    for canonical_id in (2, 3):
        connection.execute(
            """INSERT INTO question_assets (
                canonical_id, kind, visibility, storage_path
            ) VALUES (?, 'ai_redraw', 'shared', ?)""",
            (canonical_id, f"/shared/redraw-{canonical_id}.png"),
        )
    connection.commit()
    connection.close()

    command.upgrade(config, "head")

    connection = sqlite3.connect(database)
    canonical_rows = connection.execute(
        "SELECT id, normalized_stem, normalized_hash FROM canonical_questions"
    ).fetchall()
    assert len(canonical_rows) == 1
    survivor_id = canonical_rows[0][0]
    assert survivor_id == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM questions WHERE canonical_id = ?", (survivor_id,)
    ).fetchone()[0] == 3
    assert connection.execute(
        "SELECT COUNT(*) FROM question_assets WHERE canonical_id = ?", (survivor_id,)
    ).fetchone()[0] == 2
    fingerprints = connection.execute(
        "SELECT canonical_id, content_hash FROM question_source_fingerprints"
    ).fetchall()
    assert fingerprints == [(survivor_id, "same-source-sha256")]
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(question_source_fingerprints)")
    }
    assert "user_id" not in columns
    assert "storage_path" not in columns
    connection.close()
