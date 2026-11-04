import runpy
import sqlite3
import unittest
from pathlib import Path


class VideoVersionMigrationTests(unittest.TestCase):
    def setUp(self):
        queries_path = (
            Path(__file__).resolve().parents[1]
            / "jellyfin_kodi"
            / "objects"
            / "kodi"
            / "queries.py"
        )
        self.query = runpy.run_path(str(queries_path))["get_missing_versions"]
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(
            """
            CREATE TABLE movie (idMovie INTEGER PRIMARY KEY, idFile INTEGER NOT NULL);
            CREATE TABLE videoversion (
                idFile INTEGER UNIQUE,
                idMedia INTEGER,
                media_type TEXT,
                itemType INTEGER,
                idType INTEGER
            );
            """
        )

    def tearDown(self):
        self.db.close()

    def test_existing_file_is_not_selected_when_media_id_differs(self):
        self.db.execute("INSERT INTO movie(idMovie, idFile) VALUES (42, 7)")
        self.db.execute(
            "INSERT INTO videoversion(idFile, idMedia) VALUES (7, 999)"
        )

        self.assertEqual([], self.db.execute(self.query).fetchall())

    def test_movie_without_file_version_is_selected(self):
        self.db.execute("INSERT INTO movie(idMovie, idFile) VALUES (42, 7)")

        self.assertEqual([(7, 42)], self.db.execute(self.query).fetchall())


if __name__ == "__main__":
    unittest.main()
