# -*- coding: utf-8 -*-
"""The database: schema history, connections, and getting data back.

Three quiet problems lived here. Migrations were "ALTER TABLE, swallow the
error", so a locked database looked exactly like a column that already existed.
Every query opened and closed its own connection, with no WAL, so a read waited
on every write. And there was a backup button with no way to restore — a false
sense of safety that only reveals itself on the day it is needed.
"""
import sqlite3
import unittest

from helper import load_bot, read


def rows_in(path, table="events"):
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
    finally:
        con.close()


def make_backup(bot, path, rows):
    """A valid database at the current schema, with some events in it."""
    con = sqlite3.connect(path)
    bot.db_migrate(con)
    con.executemany("INSERT INTO events(ts,url,status) VALUES(?,?,?)",
                    [(i, "u%d" % i, "sent") for i in range(rows)])
    con.commit()
    con.close()
    return open(path, "rb").read()


class TestMigrations(unittest.TestCase):
    def setUp(self):
        self.bot = load_bot()
        self.bot.db_init()

    def test_a_fresh_database_lands_on_the_latest_version(self):
        con = self.bot.db_conn()
        self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0],
                         len(self.bot.MIGRATIONS))

    def test_running_twice_changes_nothing(self):
        """Startup runs them every time — a second pass must be a no-op."""
        con = self.bot.db_conn()
        before = con.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(self.bot.db_migrate(con), before)

    def test_the_tables_are_there(self):
        names = {r[0] for r in self.bot.db_conn().execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertLessEqual({"events", "settings", "access"}, names)

    def test_the_grouped_columns_are_indexed(self):
        """The panel groups by these; each was a full table scan without them."""
        idx = {r[0] for r in self.bot.db_conn().execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertLessEqual({"idx_events_ts", "idx_events_chat",
                              "idx_events_source", "idx_events_status"}, idx)

    def test_columns_added_later_are_present(self):
        cols = {r[1] for r in self.bot.db_conn().execute("PRAGMA table_info(events)")}
        self.assertLessEqual({"size", "thumb_id"}, cols)

    def test_a_first_release_database_is_carried_forward(self):
        """The real thing people have on disk: no size/thumb_id, old access model."""
        bot = load_bot()
        con = sqlite3.connect(bot.STATS_DB)
        con.execute("CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    " ts INTEGER, user_id INTEGER, chat_id INTEGER, chat_type TEXT,"
                    " chat_title TEXT, platform TEXT, source TEXT, url TEXT,"
                    " status TEXT, via TEXT)")
        con.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT)")
        con.execute("CREATE TABLE access(kind TEXT, ident TEXT, level TEXT,"
                    " label TEXT, PRIMARY KEY(kind, ident))")
        con.execute("INSERT INTO settings VALUES('access_mode','restricted')")
        con.executemany("INSERT INTO access VALUES(?,?,?,?)",
                        [("user", "@someone", "basic", "S"), ("chat", "-100123", "basic", "C")])
        con.execute("INSERT INTO events(ts,url,status) VALUES(1,'old','sent')")
        con.commit()
        con.close()

        bot.db_init()
        con = bot.db_conn()
        self.assertEqual(con.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1,
                         "the migration lost the existing events")
        self.assertLessEqual({"size", "thumb_id"},
                             {r[1] for r in con.execute("PRAGMA table_info(events)")})
        self.assertEqual(
            con.execute("SELECT value FROM settings WHERE key='whitelist'").fetchone()[0], "1")
        self.assertEqual([r[0] for r in con.execute("SELECT ident FROM access")], ["-100123"])

    def test_no_blind_alter_table_is_left(self):
        """A swallowed ALTER hid a locked database behind a normal-looking start."""
        src = read("bot.py")
        block = src[src.index("def _mig_1_base"):src.index("MIGRATIONS = (")]
        self.assertIn("PRAGMA table_info(events)", block,
                      "it should ask what exists instead of trying blindly")


class TestConnections(unittest.TestCase):
    def setUp(self):
        self.bot = load_bot()
        self.bot.db_init()

    def test_the_connection_is_reused(self):
        self.assertIs(self.bot.db_conn(), self.bot.db_conn())

    def test_wal_is_on(self):
        """Without it every read waits for the write that is in flight."""
        mode = self.bot.db_conn().execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")

    def test_a_reopen_replaces_it(self):
        first = self.bot.db_conn()
        self.bot.db_reopen()
        self.assertIsNot(self.bot.db_conn(), first)

    def test_queries_go_through_the_shared_connection(self):
        src = read("bot.py")
        opens = src.count("sqlite3.connect(STATS_DB")
        self.assertLessEqual(opens, 2,
                             "a query still opens its own connection (%d found)" % opens)


class TestRestore(unittest.TestCase):
    """A backup nobody can restore is a false sense of safety."""

    def setUp(self):
        self.bot = load_bot()
        self.bot.db_init()
        self.tmp = self.bot._test_tmp
        make_backup(self.bot, self.bot.STATS_DB, 3)
        self.bot.db_reopen()

    def path(self, name):
        import os
        return os.path.join(self.tmp, name)

    def test_a_good_backup_comes_back(self):
        blob = make_backup(self.bot, self.path("good.db"), 7)
        ok, why = self.bot.restore_backup(blob)
        self.assertTrue(ok, why)
        self.assertEqual(rows_in(self.bot.STATS_DB), 7)

    def test_the_replaced_database_is_kept(self):
        """A restore from the wrong file must not be the end of the story."""
        import os
        blob = make_backup(self.bot, self.path("good.db"), 7)
        self.bot.restore_backup(blob)
        self.assertTrue(os.path.exists(self.bot.STATS_DB + ".replaced"))
        self.assertEqual(rows_in(self.bot.STATS_DB + ".replaced"), 3)

    def test_junk_is_refused(self):
        ok, why = self.bot.restore_backup(b"this is not a database")
        self.assertFalse(ok)
        self.assertEqual(why, "not_sqlite")
        self.assertEqual(rows_in(self.bot.STATS_DB), 3, "the live database was touched")

    def test_a_foreign_database_is_refused(self):
        con = sqlite3.connect(self.path("other.db"))
        con.execute("CREATE TABLE nope(x)")
        con.commit()
        con.close()
        ok, why = self.bot.restore_backup(open(self.path("other.db"), "rb").read())
        self.assertFalse(ok)
        self.assertEqual(why, "wrong_tables")
        self.assertEqual(rows_in(self.bot.STATS_DB), 3)

    def test_the_right_tables_with_the_wrong_shape_are_refused(self):
        """Table names alone would pass, and the bot would break on the first insert."""
        con = sqlite3.connect(self.path("bad.db"))
        con.execute("CREATE TABLE events(id INTEGER PRIMARY KEY, ts INTEGER,"
                    " url TEXT, status TEXT)")
        con.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT)")
        con.execute("CREATE TABLE access(kind TEXT, ident TEXT, level TEXT,"
                    " label TEXT, PRIMARY KEY(kind, ident))")
        con.commit()
        con.close()
        ok, why = self.bot.restore_backup(open(self.path("bad.db"), "rb").read())
        self.assertFalse(ok)
        self.assertEqual(why, "wrong_columns")
        self.assertEqual(rows_in(self.bot.STATS_DB), 3)

    def test_an_old_backup_is_migrated_forward(self):
        con = sqlite3.connect(self.path("legacy.db"))
        con.execute("CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    " ts INTEGER, user_id INTEGER, chat_id INTEGER, chat_type TEXT,"
                    " chat_title TEXT, platform TEXT, source TEXT, url TEXT,"
                    " status TEXT, via TEXT)")
        con.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT)")
        con.execute("CREATE TABLE access(kind TEXT, ident TEXT, level TEXT,"
                    " label TEXT, PRIMARY KEY(kind, ident))")
        con.execute("INSERT INTO events(ts,url,status) VALUES(1,'old','sent')")
        con.commit()
        con.close()
        ok, why = self.bot.restore_backup(open(self.path("legacy.db"), "rb").read())
        self.assertTrue(ok, why)
        con = self.bot.db_conn()
        self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0],
                         len(self.bot.MIGRATIONS))
        self.assertLessEqual({"size", "thumb_id"},
                             {r[1] for r in con.execute("PRAGMA table_info(events)")})

    def test_the_admin_can_send_a_db_back(self):
        """A .db must be routed to the restore before the cookies branch.

        The cookies branch returns silently for anything that is not a .txt,
        so a database arriving after it would simply vanish.
        """
        src = read("bot.py")
        block = src[src.index("async def on_cookies_document"):]
        block = block[:block.index("COOKIES_MAX_BYTES")]
        self.assertIn('name.endswith(".db")', block)
        self.assertLess(block.index('name.endswith(".db")'),
                        block.index('name.endswith(".txt")'),
                        "a .db falls into the cookies branch and is ignored")

    def test_every_outcome_is_translated(self):
        for key in ("rs_ok", "rs_bad", "rs_too_big"):
            for lang in ("uk", "en"):
                with self.subTest(key=key, lang=lang):
                    self.assertIn(key, self.bot.T[lang])


if __name__ == "__main__":
    unittest.main(verbosity=2)
