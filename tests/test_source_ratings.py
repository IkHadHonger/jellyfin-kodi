"""Pure unit/SQLite regression tests; no running Kodi or network required."""

import importlib.util
import ast
from collections import defaultdict
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ratings = load_module("source_ratings", "jellyfin_kodi/objects/ratings.py")
queries = load_module("rating_queries", "jellyfin_kodi/objects/kodi/queries.py")

SAMPLE = b'''<movie><ratings>
  <rating default="true" max="10" name="imdb"><value>6.5</value><votes>38562</votes></rating>
  <rating max="10" name="themoviedb"><value>6.4</value><votes>492</votes></rating>
  <rating max="100" name="tomatometerallcritics"><value>86.0</value><votes>325</votes></rating>
  <rating max="100" name="metacritic"><value>69.0</value><votes>52</votes></rating>
  <rating max="10" name="metacriticuser"><value>6.1</value><votes>169</votes></rating>
  <rating max="100" name="tomatometeravgcritics"><value>76.0</value><votes>787</votes></rating>
  <rating max="10" name="trakt"><value>6.8</value><votes>3522</votes></rating>
</ratings></movie>'''


class FakeFile:
    def __init__(self, data):
        self.data = data
        self.closed = False

    def size(self):
        return len(self.data)

    def readBytes(self, size):
        return self.data[:size]

    def close(self):
        self.closed = True


class FakeVFS:
    def __init__(self, files):
        self.files = files
        self.opened = []

    def exists(self, path):
        return path in self.files

    def File(self, path):
        handle = FakeFile(self.files[path])
        self.opened.append(handle)
        return handle


class TestSourceRatings(unittest.TestCase):
    def test_user_nfo_keeps_source_and_scale(self):
        self.assertEqual(ratings.parse_nfo(SAMPLE, "movie"), {
            "imdb": (6.5, 38562), "tomatometerallcritics": (8.6, 325)
        })

    def test_real_audience_not_average_critics(self):
        data = SAMPLE.replace(b"</ratings>", b'<rating name="tomatometerallaudience" max="100"><value>93</value></rating></ratings>')
        self.assertEqual(ratings.parse_nfo(data, "movie")["tomatometerallaudience"], (9.3, 0))

    def test_invalid_values_never_enter_database(self):
        for value, maximum in [("NaN", "10"), ("inf", "10"), ("6", "0"), ("6", "NaN"), ("-1", "10"), ("11", "10"), ("oops", "10")]:
            with self.subTest(value=value, maximum=maximum):
                self.assertIsNone(ratings.scaled_rating(value, maximum))
        self.assertEqual(ratings.scaled_rating(0, 100), 0)
        self.assertEqual(ratings.scaled_rating(100, 100), 10)

    def test_no_invented_imdb_from_community_rating(self):
        self.assertEqual(ratings.collect_source_ratings({"CommunityRating": 6.8}, "", "movie", False), {})

    def test_server_critics_fallback(self):
        self.assertEqual(ratings.collect_source_ratings({"CriticRating": 86}, "", "movie", False), {"tomatometerallcritics": (8.6, 0)})

    def test_nfo_overrides_server_critics(self):
        vfs = FakeVFS({"smb://nas/movies/Film.mkv.nfo": SAMPLE})
        result = ratings.collect_source_ratings({"CriticRating": 50}, "smb://nas/movies/Film.mkv.mkv", "movie", False, vfs)
        self.assertEqual(result["tomatometerallcritics"], (8.6, 325))
        self.assertTrue(vfs.opened[0].closed)

    def test_missing_nfo_keeps_server_score(self):
        self.assertEqual(ratings.collect_source_ratings({"CriticRating": 86}, "smb://nas/Film.mkv", "movie", True, FakeVFS({})), {"tomatometerallcritics": (8.6, 0)})

    def test_unavailable_vfs_does_not_fail_sync(self):
        class BrokenVFS:
            def exists(self, path):
                raise OSError("offline")
        self.assertEqual(ratings.collect_source_ratings({}, "nfs://nas/Film.mkv", "movie", True, BrokenVFS()), {})

    def test_oversized_file_closed(self):
        vfs = FakeVFS({"/films/Film.nfo": b"x" * (ratings.MAX_NFO_BYTES + 1)})
        self.assertEqual(ratings.collect_source_ratings({}, "/films/Film.mkv", "movie", True, vfs), {})
        self.assertTrue(vfs.opened[0].closed)

    def test_reject_malformed_and_entities(self):
        for data in [b"<movie>", b'<!DOCTYPE movie [<!ENTITY x "test">]><movie>&x;</movie>', '<!DOCTYPE movie><movie/>'.encode("utf-16")]:
            self.assertEqual(ratings.parse_nfo(data, "movie"), {})

    def test_wrong_identity_and_type(self):
        data = SAMPLE.replace(b"<movie>", b'<movie><uniqueid type="imdb">tt123</uniqueid>')
        self.assertEqual(ratings.parse_nfo(data, "movie", {"Imdb": "tt999"}), {})
        self.assertEqual(ratings.parse_nfo(SAMPLE, "tvshow"), {})
        self.assertEqual(ratings.parse_nfo(SAMPLE, "movie", {"Imdb": "tt123"}, True), {})
        self.assertIn("imdb", ratings.parse_nfo(data, "movie", {"Imdb": "tt123"}, True))

    def test_conflicting_duplicate_is_ignored(self):
        data = SAMPLE.replace(b"</ratings>", b'<rating name="imdb" max="10"><value>9.0</value></rating></ratings>')
        self.assertNotIn("imdb", ratings.parse_nfo(data, "movie"))

    def test_votes_fit_sqlite_integer(self):
        data = SAMPLE.replace(b"38562", b"999999999999999999999999999999999999")
        self.assertEqual(ratings.parse_nfo(data, "movie")["imdb"][1], 2**63 - 1)

    def test_invalid_path_is_optional_metadata_not_sync_failure(self):
        for path in [None, 123, "smb://[bad/Film.mkv"]:
            self.assertEqual(ratings.collect_source_ratings({}, path, "movie", True), {})

    def test_tvshow_episode_root(self):
        for media, root in [("tvshow", b"tvshow"), ("episode", b"episodedetails")]:
            data = SAMPLE.replace(b"movie", root)
            self.assertIn("imdb", ratings.parse_nfo(data, media))

    def test_only_safe_adjacent_paths(self):
        for path in ["https://server/Film.mkv", "plugin://jellyfin/Film.mkv", "stack://a.mkv,b.mkv", "/server-only/Film.mkv", "relative/Film.mkv", "smb://nas/Film.mkv?token=x"]:
            self.assertEqual(ratings.nfo_candidates(path, "movie", False), [])
        self.assertEqual(ratings.nfo_candidates("smb://nas/Show/", "tvshow", False), [("smb://nas/Show/tvshow.nfo", False)])
        self.assertEqual(ratings.nfo_candidates("nfs://nas/Show/S01E01.mkv", "episode", False), [("nfs://nas/Show/S01E01.nfo", False)])
        self.assertEqual(ratings.nfo_candidates("/films/Film.mkv", "movie", True)[0], ("/films/Film.nfo", False))
        self.assertEqual(ratings.nfo_candidates(r"C:\Films\Film.mkv", "movie", True)[0], (r"C:\Films\Film.nfo", False))

    def test_shared_movie_nfo_requires_identity(self):
        vfs = FakeVFS({"smb://nas/movie.nfo": SAMPLE})
        self.assertEqual(ratings.collect_source_ratings({}, "smb://nas/Other.mkv", "movie", False, vfs), {})


class TestRatingDatabase(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.cursor = self.db.cursor()
        self.cursor.execute("CREATE TABLE rating (rating_id INTEGER PRIMARY KEY, media_id INTEGER, media_type TEXT, rating_type TEXT, rating REAL, votes INTEGER)")
        self.cursor.executemany("INSERT INTO rating VALUES (?, ?, ?, ?, ?, ?)", [
            (1, 42, "movie", "imdb", 5, 100),
            (2, 42, "movie", "default", 6.8, 3522),
            (3, 42, "movie", "tmdb", 6.4, 492),
            (4, 42, "movie", "trakt", 6.8, 3522),
            (5, 99, "movie", "imdb", 9, 20),
        ])

    def snapshot(self):
        return self.cursor.execute("SELECT * FROM rating ORDER BY rating_id").fetchall()

    def test_sync_is_repeatable_and_preserves_unrelated_ratings(self):
        before = self.snapshot()
        data = ratings.parse_nfo(SAMPLE, "movie")
        data["trakt"] = (1, 0)  # defensively excluded even if a caller passes it
        ratings.sync_source_ratings(self.cursor, 42, "movie", data)
        once = self.snapshot()
        ratings.sync_source_ratings(self.cursor, 42, "movie", data)
        self.assertEqual(self.snapshot(), once)
        self.assertEqual(once[1:5], before[1:5])
        self.assertEqual(once[0][4:], (6.5, 38562))
        self.assertEqual(once[-1][3:], ("tomatometerallcritics", 8.6, 325))
        ratings.sync_source_ratings(self.cursor, 42, "movie", {})
        self.assertEqual(self.snapshot(), once)

    def test_later_default_sync_cannot_overwrite_named_rating(self):
        self.cursor.execute(queries.get_rating, ("movie", 42))
        default_id = self.cursor.fetchone()[0]
        self.assertEqual(default_id, 2)
        self.cursor.execute(queries.update_rating, (42, "movie", "default", 7, 0, default_id))
        self.assertEqual(self.snapshot()[0][3:], ("imdb", 5, 100))

    def test_missing_default_does_not_select_named_source(self):
        self.cursor.execute(queries.get_rating, ("movie", 99))
        self.assertIsNone(self.cursor.fetchone())

    def test_tv_and_episode_are_scoped_separately(self):
        for media in ("tvshow", "episode"):
            ratings.sync_source_ratings(self.cursor, 42, media, {"imdb": (8, 10)})
        self.assertEqual(self.snapshot()[0][4], 5)
        self.assertEqual(len(self.snapshot()), 7)

    def test_rollback_remains_owned_by_sync(self):
        self.db.commit()
        before = self.snapshot()
        ratings.sync_source_ratings(self.cursor, 42, "movie", {"tomatometerallaudience": (9.3, 10)})
        self.db.rollback()
        self.assertEqual(self.snapshot(), before)

    def test_actual_update_methods_preserve_named_ratings(self):
        # Execute real update method bodies with real SQLite. Mock only the
        # unrelated paths, artwork and Jellyfin reference database operations.
        cases = [
            ("movies.py", "Movies", "movie_update", "movie", "MovieId"),
            ("tvshows.py", "TVShows", "tvshow_update", "tvshow", "ShowId"),
            ("tvshows.py", "TVShows", "episode_update", "episode", "EpisodeId"),
        ]
        for filename, class_name, method_name, media_type, id_key in cases:
            with self.subTest(method=method_name):
                tree = ast.parse((ROOT / "jellyfin_kodi/objects" / filename).read_text())
                cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
                method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == method_name)
                namespace = {"QU": queries, "QUEM": Mock(), "LOG": Mock()}

                def values(obj, template):
                    if not isinstance(template, list):
                        return []
                    return [obj[x[1:-1]] if x.startswith("{") else x for x in template]

                namespace["values"] = values
                module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
                exec(compile(module, filename, "exec"), namespace)
                cursor = self.cursor

                def get_rating_id(*args):
                    row = cursor.execute(queries.get_rating, args).fetchone()
                    return row[0] if row else None

                fake = Mock()
                fake.get_rating_id.side_effect = get_rating_id
                fake.create_entry_rating.side_effect = lambda: cursor.execute(queries.create_rating).fetchone()[0] + 1
                fake.add_ratings.side_effect = lambda *a: cursor.execute(queries.add_rating, a)
                fake.update_ratings.side_effect = lambda *a: cursor.execute(queries.update_rating, a)
                obj = defaultdict(lambda: 0, {id_key: 99, "Rating": 6.8, "Votes": 3522})
                namespace[method_name](fake, obj)
                obj["Rating"] = 7.2
                namespace[method_name](fake, obj)
                rows = cursor.execute(
                    "SELECT rating FROM rating WHERE media_id=99 AND media_type=? AND rating_type='default'",
                    (media_type,),
                ).fetchall()
                self.assertEqual(rows, [(7.2,)])
                self.assertEqual(cursor.execute("SELECT rating_type, rating FROM rating WHERE rating_id=5").fetchone(), ("imdb", 9))


if __name__ == "__main__":
    unittest.main()
