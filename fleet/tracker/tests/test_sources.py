"""Offline tests for sources.py: the SQL guard, the Redis allowlist, the env file, secret
scrubbing, and the Postgres session rules (against a fake psycopg2, so no network and no
psycopg2 install is needed).

    cd fleet/tracker && python3 -m unittest discover -s tests -v
"""
import decimal
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import sources  # noqa: E402


def refused(sql):
    try:
        sources.check_read_only(sql)
    except sources.ReadOnlyViolation:
        return True
    return False


class SqlGuard(unittest.TestCase):
    def test_accepts_plain_reads(self):
        for sql in (
            "select 1",
            "SELECT id FROM recordings WHERE project_id = %(project)s",
            "  \n\tselect 1",
            "with x as (select 1 as a) select a from x",
            "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE n < 3) SELECT n FROM t",
            "select 1;",
            "select 1 ;  \n",
            "-- leading comment\nselect 1",
            "/* leading */ select 1",
        ):
            self.assertFalse(refused(sql), sql)

    def test_keywords_inside_names_and_literals_are_fine(self):
        for sql in (
            "select updated_at, created_at, returned_at, deleted_flag from t",
            "select data->>'deleted_at' from trips where data->>'task' <> '(per camera)'",
            "select 'insert into t values (1); drop table x' as s",
            "select 'it''s; delete' as s",
            "select $$; drop table x$$",
            "select $tag$ delete from t; $tag$",
            'select "delete", "drop table" from t',
            "select 1 /* outer /* nested ; delete */ still comment */",
            "select data ? 'renamed_from' from t",
            "select starts_with(id, 'dragon:') from cameras",
        ):
            self.assertFalse(refused(sql), sql)

    def test_refuses_writes(self):
        for sql in (
            "insert into t values (1)",
            "INSERT INTO t SELECT * FROM u",
            "update t set a = 1",
            "delete from t",
            "drop table t",
            "truncate t",
            "alter table t add column x int",
            "create table x (a int)",
            "grant all on t to public",
            "copy t to '/tmp/x'",
            "vacuum",
            "set statement_timeout = 0",
            "begin; delete from t; commit",
            "explain analyze delete from t",
            "call proc()",
            "do $$ begin delete from t; end $$",
        ):
            self.assertTrue(refused(sql), sql)

    def test_refuses_a_second_statement(self):
        for sql in (
            "select 1; delete from t",
            "select 1;delete from t",
            "select 1; select 2",
            "select 'x'; drop table t; --'",
            "select 1 -- comment\n; drop table t",
        ):
            self.assertTrue(refused(sql), sql)

    def test_comment_tricks(self):
        for sql in (
            "/* select */ delete from t",
            "-- select\ndelete from t",
            "sel/**/ect 1",
            "/* /* nested */ select */ delete from t",
            "select 1 /* unterminated",
            "select 'unterminated",
            'select "unterminated',
            "select $$ unterminated",
        ):
            self.assertTrue(refused(sql), sql)

    def test_escape_string_cannot_hide_a_statement(self):
        # In E'...' a backslash escapes the quote, so the literal is just "'" and the
        # delete is real code. A naive tokenizer would think it is inside a string.
        self.assertTrue(refused("select E'\\''; delete from t; --'"))
        self.assertTrue(refused("select e'\\'' ; delete from t"))
        self.assertFalse(refused("select E'a\\'b' as s"))

    def test_data_modifying_cte(self):
        for sql in (
            "with x as (delete from t returning *) select * from x",
            "WITH a AS (UPDATE t SET x = 1 RETURNING x) SELECT * FROM a",
            "with a as (insert into t values (1) returning *) select 1",
        ):
            self.assertTrue(refused(sql), sql)

    def test_select_into_locks_and_side_effects(self):
        for sql in (
            "select * into newtable from t",
            "select * from t for update",
            "select * from t for no key update",
            "select * from t for share",
            "select * from t for key share",
            "select pg_terminate_backend(123)",
            "select pg_cancel_backend(123)",
            "select set_config('statement_timeout', '0', false)",
            "select nextval('seq')",
            "select pg_sleep(100)",
            "select * from dblink('host=x', 'delete from t') as t(a int)",
            "select lo_import('/etc/passwd')",
            "select pg_read_file('/etc/passwd')",
            "select pg_advisory_lock(1)",
        ):
            self.assertTrue(refused(sql), sql)

    def test_non_string(self):
        self.assertTrue(refused(None))
        self.assertTrue(refused(b"select 1"))


class RedisAllowlist(unittest.TestCase):
    def test_refuses_writes_case_insensitively(self):
        for name in ("SET", "set", "Del", "DEL", "EVAL", "evalsha", "HSET", "hdel", "FLUSHALL", "flushdb",
                     "ZADD", "ZREMRANGEBYRANK", "SADD", "SREM", "EXPIRE", "RENAME", "GETDEL", "GETSET",
                     "INCR", "LPUSH", "SORT", "ZRANGESTORE", "COPY", "MIGRATE", "CONFIG", "SCRIPT",
                     "PUBLISH", "MULTI", "KEYS", " "):
            with self.assertRaises(sources.ReadOnlyViolation, msg=name):
                sources.check_read_command([name, "k", "v"])

    def test_accepts_reads_case_insensitively(self):
        for name in ("GET", "get", "MGet", "hgetall", "HKEYS", "smembers", "zrangebyscore", "ZREVRANGE",
                     "ZCARD", "type", "SCAN", "strlen"):
            self.assertEqual(sources.check_read_command([name, "k"]), name.strip().upper())

    def test_malformed(self):
        for cmd in ([], None, "GET k", [1, "k"], ()):
            with self.assertRaises(sources.ReadOnlyViolation):
                sources.check_read_command(cmd)


class FakeHTTP:
    """Stands in for urllib.request.urlopen: records each request, answers each command."""

    def __init__(self, answer=None):
        self.bodies = []
        self.headers = []
        self.answer = answer or (lambda cmd: {"result": f"ok:{cmd[0]}"})

    def __call__(self, req, timeout=None):
        cmds = json.loads(req.data)
        self.bodies.append(cmds)
        self.headers.append(dict(req.header_items()))
        return io.BytesIO(json.dumps([self.answer(c) for c in cmds]).encode())


class RedisClient(unittest.TestCase):
    def setUp(self):
        self.r = sources.Redis("https://example-123.upstash.io", "TOKEN-abcdef-123456")

    def test_batches_at_most_fifty(self):
        http = FakeHTTP()
        with mock.patch("urllib.request.urlopen", http):
            out = self.r.pipeline([["GET", f"k{i}"] for i in range(120)])
        self.assertEqual([len(b) for b in http.bodies], [50, 50, 20])
        self.assertEqual(len(out), 120)
        self.assertEqual(self.r.requests, 3)
        self.assertEqual(self.r.commands, 120)
        self.assertGreater(self.r.bytes_received, 0)
        self.assertEqual(http.headers[0]["Authorization"], "Bearer TOKEN-abcdef-123456")

    def test_one_bad_command_refuses_the_whole_batch_before_sending(self):
        http = FakeHTTP()
        with mock.patch("urllib.request.urlopen", http):
            with self.assertRaises(sources.ReadOnlyViolation):
                self.r.pipeline([["GET", "a"], ["GET", "b"], ["DEL", "a"]])
        self.assertEqual(http.bodies, [])          # nothing at all went out

    def test_per_command_error_raises(self):
        http = FakeHTTP(lambda c: {"error": "WRONGTYPE Operation against a key"} if c[1] == "bad" else {"result": 1})
        with mock.patch("urllib.request.urlopen", http):
            with self.assertRaises(sources.SourceError) as cm:
                self.r.pipeline([["GET", "good"], ["GET", "bad"]])
        self.assertIn("WRONGTYPE", str(cm.exception))

    def test_args_are_sent_as_strings(self):
        http = FakeHTTP()
        with mock.patch("urllib.request.urlopen", http):
            self.r.call("ZRANGEBYSCORE", "k", "(5", "+inf", "LIMIT", 0, 1000)
        self.assertEqual(http.bodies[0][0], ["ZRANGEBYSCORE", "k", "(5", "+inf", "LIMIT", "0", "1000"])

    def test_missing_credentials(self):
        with self.assertRaises(sources.SourceError):
            sources.Redis("", "x")
        with self.assertRaises(sources.SourceError):
            sources.Redis.from_env({})


class EnvFile(unittest.TestCase):
    def test_first_existing_file_wins_and_quotes_are_stripped(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = Path(d) / "missing.env", Path(d) / "b.env"
            b.write_text('# comment\n\nDATABASE_URL=postgresql://u:pw@h:5432/db\n'
                         'UPSTASH_REDIS_REST_TOKEN="tok-123456"\nexport QUOTED=\'single\'\n'
                         'EMPTY=\nnot a pair\n  SPACED = value with spaces  \n')
            c = Path(d) / "c.env"
            c.write_text("DATABASE_URL=wrong\n")
            env = sources.load_env([a, b, c])
        self.assertEqual(env["DATABASE_URL"], "postgresql://u:pw@h:5432/db")
        self.assertEqual(env["UPSTASH_REDIS_REST_TOKEN"], "tok-123456")
        self.assertEqual(env["QUOTED"], "single")
        self.assertEqual(env["EMPTY"], "")
        self.assertEqual(env["SPACED"], "value with spaces")
        self.assertNotIn("not a pair", env)

    def test_no_file_is_an_empty_dict(self):
        self.assertEqual(sources.load_env([Path("/nonexistent/x.env")]), {})

    def test_load_env_prints_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "e.env"
            f.write_text("DATABASE_URL=postgresql://user:hunter2-secret@db.example.com/x\n")
            with mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
                    mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                sources.load_env([f])
        self.assertEqual(out.getvalue() + err.getvalue(), "")


class Scrub(unittest.TestCase):
    def test_removes_dsn_parts_tokens_and_urls(self):
        sources.Postgres("postgresql://postgres.abcd:sup3r-s3cret@pooler.example.com:6543/postgres")
        sources.Redis("https://brave-cat-1234.upstash.io", "AXyzTOKENvalue123")
        msg = ('connection to server at "pooler.example.com" failed for user "postgres.abcd" password '
               'sup3r-s3cret; url postgresql://postgres.abcd:sup3r-s3cret@pooler.example.com:6543/postgres '
               'token AXyzTOKENvalue123 host brave-cat-1234.upstash.io')
        clean = sources.scrub(msg)
        for secret in ("sup3r-s3cret", "postgres.abcd", "pooler.example.com", "AXyzTOKENvalue123",
                       "brave-cat-1234"):
            self.assertNotIn(secret, clean)

    def test_unknown_url_shapes(self):
        clean = sources.scrub("could not reach postgres://someone:pw@10.0.0.1/db or rediss://:pw@h:6379")
        self.assertNotIn("pw@", clean)
        self.assertIn("postgres://***", clean)


# --------------------------------------------------------------------------- #
# a fake psycopg2, enough to prove the session rules without a database
# --------------------------------------------------------------------------- #
def fake_psycopg2(plan):
    """A module object whose connections follow `plan`, a list of what each user query
    does: "rows", "drop" (server closed the connection) or "timeout"."""
    m = types.ModuleType("psycopg2")

    class Error(Exception):
        pgerror = None

    class OperationalError(Error):
        pass

    class InterfaceError(Error):
        pass

    class QueryCanceledError(OperationalError):
        pass

    m.Error, m.OperationalError, m.InterfaceError = Error, OperationalError, InterfaceError
    m.extensions = types.ModuleType("psycopg2.extensions")
    m.extensions.QueryCanceledError = QueryCanceledError
    m.connections, m.log = [], []

    class Cursor:
        def __init__(self, conn):
            self.conn, self.description, self.rows = conn, None, []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            m.log.append((sql, params))
            if sql.startswith("SET LOCAL"):
                return
            step = plan.pop(0) if plan else "rows"
            if step == "drop":
                self.conn.closed = 2
                raise OperationalError("server closed the connection unexpectedly")
            if step == "timeout":
                raise QueryCanceledError("canceling statement due to statement timeout")
            self.description = [("a",), ("n",)]
            self.rows = [("x", decimal.Decimal("2.5"))]

        def fetchall(self):
            return self.rows

    class Conn:
        def __init__(self, dsn, **kw):
            self.dsn, self.kw, self.closed = dsn, kw, 0
            self.session, self.rollbacks = None, 0
            m.connections.append(self)

        def set_session(self, **kw):
            self.session = kw

        def cursor(self):
            return Cursor(self)

        def rollback(self):
            self.rollbacks += 1

        def commit(self):
            raise AssertionError("the tracker must never commit")

        def close(self):
            self.closed = 1

    m.connect = Conn
    return m


class PostgresSession(unittest.TestCase):
    DSN = "postgresql://user:pw-123456@db.example.com:5432/postgres"

    def _with(self, plan):
        fake = fake_psycopg2(plan)
        return fake, mock.patch.dict(sys.modules, {"psycopg2": fake, "psycopg2.extensions": fake.extensions})

    def test_read_only_session_timeout_and_rollback(self):
        fake, patch = self._with(["rows"])
        with patch:
            pg = sources.Postgres(self.DSN, timeout_s=12)
            rows = pg.query("select a, n from t where x = %(x)s", {"x": 1})
        self.assertEqual(rows, [{"a": "x", "n": 2.5}])           # Decimal came back as float
        self.assertIsInstance(rows[0]["n"], float)
        conn = fake.connections[0]
        self.assertEqual(conn.session, {"readonly": True, "autocommit": False})
        self.assertEqual(conn.kw["application_name"], "starcap-tracker")
        self.assertEqual(conn.kw["connect_timeout"], 15)
        self.assertEqual(fake.log[0], ("SET LOCAL statement_timeout = %s", (12000,)))
        self.assertEqual(conn.rollbacks, 1)

    def test_refused_sql_never_reaches_the_database(self):
        fake, patch = self._with([])
        with patch:
            pg = sources.Postgres(self.DSN)
            with self.assertRaises(sources.ReadOnlyViolation):
                pg.query("delete from recordings")
        self.assertEqual(fake.connections, [])                   # not even a connection

    def test_reconnects_once_on_a_dropped_connection(self):
        fake, patch = self._with(["drop", "rows"])
        with patch:
            rows = sources.Postgres(self.DSN).query("select 1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(fake.connections), 2)

    def test_gives_up_after_the_second_drop(self):
        fake, patch = self._with(["drop", "drop", "rows"])
        with patch:
            with self.assertRaises(sources.SourceError):
                sources.Postgres(self.DSN).query("select 1")
        self.assertEqual(len(fake.connections), 2)

    def test_timeout_is_not_retried(self):
        fake, patch = self._with(["timeout", "rows"])
        with patch:
            pg = sources.Postgres(self.DSN)
            with self.assertRaises(sources.QueryTimeout):
                pg.query("select 1")
            self.assertEqual(pg.query("select 1"), [{"a": "x", "n": 2.5}])   # same connection still works
        self.assertEqual(len(fake.connections), 1)
        self.assertEqual(fake.connections[0].rollbacks, 2)

    def test_missing_psycopg2_says_how_to_install(self):
        with mock.patch.dict(sys.modules, {"psycopg2": None, "psycopg2.extensions": None}):
            with self.assertRaises(sources.SourceError) as cm:
                sources.Postgres(self.DSN).query("select 1")
        self.assertIn("sudo apt install python3-psycopg2", str(cm.exception))

    def test_connect_error_is_scrubbed(self):
        fake, patch = self._with([])

        def boom(dsn, **kw):
            raise fake.OperationalError(f'connection to "db.example.com" failed: password pw-123456 rejected ({dsn})')

        fake.connect = boom
        with patch:
            with self.assertRaises(sources.SourceError) as cm:
                sources.Postgres(self.DSN).query("select 1")
        for secret in ("pw-123456", "db.example.com", "user:"):
            self.assertNotIn(secret, str(cm.exception))

    def test_missing_dsn(self):
        with self.assertRaises(sources.SourceError):
            sources.Postgres.from_env({})


if __name__ == "__main__":
    unittest.main()
