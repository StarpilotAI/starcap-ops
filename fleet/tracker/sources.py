"""
Read-only clients for the two production stores this tool reads: the dashboard's
Postgres and its Upstash Redis.

Why so defensive. The credentials come from the dashboard's own env file, and they
are WRITE credentials: the same DATABASE_URL the Next.js app inserts with, the same
Upstash token that can FLUSHALL. Nothing here is allowed to rely on "the caller only
ever passes a select". So there are three independent locks on the door:

  1. Postgres sessions are `set_session(readonly=True)`: every statement runs inside
     a READ ONLY transaction that is always rolled back, never committed, so even a
     statement that slipped through could not change a row.
  2. `check_read_only()` refuses, before anything is sent, any SQL that is not one
     plain select/with statement: no second statement after a `;`, no data-modifying
     CTE (`with x as (delete ...)`), no `select ... into`, no row locks, no
     side-effect functions (pg_terminate_backend, set_config, dblink, ...).
  3. Redis only sends commands from READ_COMMANDS, checked per command.

Secrets never leave this module in a message: every error raised from here goes
through `scrub()`, which removes the DSN, its parts, the Upstash URL and token.

psycopg2 is the only third-party import in the whole tool, and it is imported
lazily, inside `Postgres.connect`, so the web UI and the Redis half still work on a
laptop that hasn't installed it yet.
"""
import decimal
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import config

PG_TIMEOUT_S = 25.0          # SET LOCAL statement_timeout; each query must stay well under 30 s
PG_CONNECT_TIMEOUT_S = 15
REDIS_TIMEOUT_S = 30.0
REDIS_BATCH = 50             # commands per Upstash /pipeline request

PSYCOPG2_HINT = "psycopg2 is not installed: sudo apt install python3-psycopg2"


class SourceError(Exception):
    """A source failed. The message is already scrubbed of secrets."""


class ReadOnlyViolation(SourceError):
    """A statement or command that could write was refused before it was sent."""


class QueryTimeout(SourceError):
    """Postgres cancelled a statement at statement_timeout. The caller may retry it in
    smaller pieces; the connection itself is fine."""


# --------------------------------------------------------------------------- #
# env file + secret scrubbing
# --------------------------------------------------------------------------- #
_SECRETS = set()        # every credential value seen by load_env / the clients


def load_env(files=None):
    """KEY=VALUE pairs from the first env file that exists (config.ENV_FILES).

    Blank lines and # comments are skipped, an `export ` prefix is allowed, and one
    pair of matching surrounding quotes is stripped. Never prints a value. Returns {}
    when no file exists; callers decide whether that is fatal."""
    for path in (config.ENV_FILES if files is None else files):
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        env = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, sep, value = line.partition("=")
            key = key.strip()
            if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            env[key] = value
        _remember_secrets(env.get(k) for k in env if _looks_secret(k))
        return env
    return {}


def _looks_secret(key):
    return any(w in key.upper() for w in ("URL", "TOKEN", "SECRET", "PASSWORD", "KEY", "JSON", "PHONE"))


def _remember_secrets(values):
    for v in values:
        if v and len(str(v)) >= 6:
            _SECRETS.add(str(v))


def _dsn_parts(dsn):
    """The pieces of a postgres URL that must never be printed (user, password, host)."""
    try:
        u = urllib.parse.urlsplit(dsn)
        return [p for p in (u.password, u.username, u.hostname, urllib.parse.unquote(u.password or ""))
                if p and len(p) >= 4]
    except ValueError:
        return []


_URL_RE = re.compile(r"\b(?:postgres(?:ql)?|rediss?|https?)://[^\s'\"<>]+", re.I)


def scrub(text):
    """`text` with every known secret, and anything shaped like a connection URL, removed."""
    s = str(text)
    for secret in sorted(_SECRETS, key=len, reverse=True):
        s = s.replace(secret, "***")
    return _URL_RE.sub(lambda m: m.group(0).split("://")[0] + "://***", s)


# --------------------------------------------------------------------------- #
# the SQL guard
# --------------------------------------------------------------------------- #
# Words that have no business in a read: DML/DDL, transaction control, session
# state, and `into` (SELECT INTO creates a table). Matched as whole words on the
# statement with every comment, string literal and quoted identifier blanked out,
# so a column like updated_at or a literal like 'deleted_at' never trips it.
_WRITE_WORDS = (
    "insert update delete merge upsert truncate drop alter create grant revoke copy call do "
    "execute prepare deallocate vacuum analyze cluster reindex refresh lock listen notify "
    "unlisten discard reset comment security import load into returning checkpoint begin "
    "commit rollback savepoint release abort"
).split()
_WRITE_RE = re.compile(r"\b(" + "|".join(_WRITE_WORDS) + r")\b", re.I)
_LOCK_RE = re.compile(r"\bfor\s+(no\s+key\s+update|key\s+share|update|share)\b", re.I)
# Functions that act outside the transaction, so READ ONLY would not stop them.
_SIDE_EFFECT_RE = re.compile(
    r"\b(pg_terminate_backend|pg_cancel_backend|pg_reload_conf|pg_rotate_logfile|pg_promote|"
    r"pg_switch_wal|pg_create_\w+|pg_drop_\w+|pg_replication_\w+|pg_advisory\w*|pg_try_advisory\w*|"
    r"pg_sleep\w*|pg_notify|pg_read_\w+|pg_ls_\w+|pg_stat_file|pg_file_\w+|set_config|"
    r"lo_\w+|dblink\w*|nextval|setval|txid_\w+|pg_current_xact_id\w*)\s*\(", re.I)


def _skeleton(sql):
    """The statement with comments turned into spaces and the contents of every string
    literal, dollar-quoted string and quoted identifier removed. Postgres treats a
    comment as whitespace and nests /* */ comments, and so does this."""
    out, i, n = [], 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
            out.append(" ")
        elif c == "/" and sql.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            if depth:
                raise ReadOnlyViolation("unterminated /* comment")
            out.append(" ")
        elif c == "'":
            # E'...' strings honour backslash escapes; standard strings only ''.
            escaped = i > 0 and sql[i - 1] in "eE" and (i < 2 or not (sql[i - 2].isalnum() or sql[i - 2] == "_"))
            i += 1
            while True:
                if i >= n:
                    raise ReadOnlyViolation("unterminated string literal")
                if escaped and sql[i] == "\\":
                    i += 2
                    continue
                if sql[i] == "'":
                    if sql.startswith("''", i):
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append("''")
        elif c == '"':
            j = i + 1
            while True:
                j = sql.find('"', j)
                if j < 0:
                    raise ReadOnlyViolation("unterminated quoted identifier")
                if sql.startswith('""', j):
                    j += 2
                    continue
                break
            i = j + 1
            out.append('""')
        elif c == "$":
            m = re.match(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$", sql[i:])
            if m and not (i > 0 and (sql[i - 1].isalnum() or sql[i - 1] == "_")):
                tag = m.group(0)
                j = sql.find(tag, i + len(tag))
                if j < 0:
                    raise ReadOnlyViolation("unterminated dollar-quoted string")
                i = j + len(tag)
                out.append("''")
            else:
                out.append(c)
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def check_read_only(sql):
    """Raise ReadOnlyViolation unless `sql` is exactly one select/with statement that
    cannot write. Returns the skeleton it judged (handy in tests)."""
    if not isinstance(sql, str):
        raise ReadOnlyViolation("statement must be a string")
    skel = _skeleton(sql).strip()
    while skel.endswith(";"):
        skel = skel[:-1].rstrip()
    if not re.match(r"(select|with)\b", skel, re.I):
        raise ReadOnlyViolation("only select / with statements are allowed")
    if ";" in skel:
        raise ReadOnlyViolation("only one statement per query")
    m = _WRITE_RE.search(skel) or _LOCK_RE.search(skel) or _SIDE_EFFECT_RE.search(skel)
    if m:
        raise ReadOnlyViolation(f"refused: {m.group(0).strip()!r} has no place in a read")
    return skel


# --------------------------------------------------------------------------- #
# Postgres
# --------------------------------------------------------------------------- #
def _import_psycopg2():
    try:
        import psycopg2            # noqa: PLC0415  lazy on purpose (see module docstring)
        import psycopg2.extensions  # noqa: PLC0415,F401
    except ImportError as e:
        raise SourceError(PSYCOPG2_HINT) from e
    return psycopg2


def _plain(value):
    """Decimal -> float (numeric/extract(epoch) come back as Decimal); the rest as is."""
    return float(value) if isinstance(value, decimal.Decimal) else value


class Postgres:
    """One lazily-opened, read-only psycopg2 connection. Not thread-safe; the syncer
    is its only user and runs one pass at a time."""

    def __init__(self, dsn, timeout_s=PG_TIMEOUT_S):
        if not dsn:
            raise SourceError("DATABASE_URL is not set in the env file")
        self._dsn = dsn
        self.timeout_s = timeout_s
        self._conn = None
        self.queries = 0
        _remember_secrets([dsn, *_dsn_parts(dsn)])

    @classmethod
    def from_env(cls, env=None, **kw):
        env = load_env() if env is None else env
        return cls(env.get("DATABASE_URL", ""), **kw)

    def connect(self):
        pg = _import_psycopg2()
        try:
            conn = pg.connect(self._dsn, connect_timeout=PG_CONNECT_TIMEOUT_S,
                              application_name="starcap-tracker")
            conn.set_session(readonly=True, autocommit=False)
        except pg.Error as e:
            raise SourceError("postgres connect failed: " + scrub(_pg_message(e))) from None
        self._conn = conn
        return conn

    def ensure(self):
        """Connect now if not connected, so a dead database fails once, fast, at the
        start of a sync instead of once per step."""
        if self._conn is None or self._conn.closed:
            self.connect()

    def close(self):
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:                   # noqa: BLE001  closing a dead socket may raise
                pass

    def query(self, sql, params=None, timeout_s=None):
        """Rows of one read-only statement as dicts. Refuses anything but a single
        select/with, runs it under SET LOCAL statement_timeout, always rolls back, and
        reconnects once if the connection was dropped (a timeout is not retried)."""
        check_read_only(sql)
        pg = _import_psycopg2()
        timeout_ms = int((timeout_s or self.timeout_s) * 1000)
        for attempt in (1, 2):
            conn = self._conn if self._conn is not None and not self._conn.closed else self.connect()
            try:
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL statement_timeout = %s", (timeout_ms,))
                    cur.execute(sql, params)
                    cols = [d[0] for d in cur.description or ()]
                    rows = [{c: _plain(v) for c, v in zip(cols, r)} for r in cur.fetchall()]
                self.queries += 1
                return rows
            except pg.Error as e:
                dropped = conn.closed != 0
                msg = scrub(_pg_message(e))
                if dropped:
                    self.close()
                    if attempt == 1:
                        continue
                    raise SourceError("postgres connection lost twice: " + msg) from None
                if isinstance(e, pg.extensions.QueryCanceledError):
                    raise QueryTimeout(f"postgres query took over {timeout_ms / 1000:.0f} s and was cancelled") from None
                raise SourceError("postgres: " + msg) from None
            finally:
                if not conn.closed:
                    try:
                        conn.rollback()            # never commit: every query ends in a rollback
                    except pg.Error:
                        self.close()
        raise SourceError("postgres: unreachable")   # pragma: no cover


def _pg_message(e):
    text = (getattr(e, "pgerror", None) or str(e) or type(e).__name__).strip()
    return text.splitlines()[0] if text else type(e).__name__


# --------------------------------------------------------------------------- #
# Upstash Redis (REST)
# --------------------------------------------------------------------------- #
READ_COMMANDS = frozenset(
    "GET MGET HGET HMGET HGETALL HKEYS HLEN HSCAN SMEMBERS SCARD SISMEMBER ZRANGE ZREVRANGE "
    "ZRANGEBYSCORE ZREVRANGEBYSCORE ZCARD ZSCORE LRANGE LLEN TYPE TTL EXISTS SCAN STRLEN".split())


def check_read_command(cmd):
    """Raise ReadOnlyViolation unless `cmd` (a list) names an allowlisted read command."""
    if not isinstance(cmd, (list, tuple)) or not cmd or not isinstance(cmd[0], str):
        raise ReadOnlyViolation("a redis command is a non-empty list starting with its name")
    name = cmd[0].strip().upper()
    if name not in READ_COMMANDS:
        raise ReadOnlyViolation(f"redis command {name or '?'} is not on the read allowlist")
    return name


class Redis:
    """Upstash's REST /pipeline endpoint, read commands only. Counts every request and
    byte so the cost of a sync can be measured (Upstash bills per command and per byte)."""

    def __init__(self, url, token, timeout_s=REDIS_TIMEOUT_S, batch=REDIS_BATCH):
        if not url or not token:
            raise SourceError("UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN are not set in the env file")
        self._url = url.rstrip("/")
        self._token = token
        self.timeout_s = timeout_s
        self.batch = max(1, min(int(batch), REDIS_BATCH))
        self.requests = self.commands = self.bytes_sent = self.bytes_received = 0
        _remember_secrets([url, token, urllib.parse.urlsplit(url).hostname or ""])

    @classmethod
    def from_env(cls, env=None, **kw):
        env = load_env() if env is None else env
        return cls(env.get("UPSTASH_REDIS_REST_URL", ""), env.get("UPSTASH_REDIS_REST_TOKEN", ""), **kw)

    def stats(self):
        return {"requests": self.requests, "commands": self.commands,
                "bytes_sent": self.bytes_sent, "bytes_received": self.bytes_received}

    def call(self, *cmd):
        return self.pipeline([list(cmd)])[0]

    def pipeline(self, commands):
        """Results of `commands` (lists like ["HGETALL", key]), in order. Every command is
        checked against the allowlist BEFORE any request is sent, then they go out in
        batches of at most 50. Any per-command error raises."""
        cmds = [[str(a) for a in c] for c in commands]
        for c in cmds:
            check_read_command(c)
        out = []
        for i in range(0, len(cmds), self.batch):
            out.extend(self._post(cmds[i:i + self.batch]))
        return out

    def _post(self, cmds):
        body = json.dumps(cmds).encode()
        req = urllib.request.Request(self._url + "/pipeline", data=body, method="POST", headers={
            "Authorization": "Bearer " + self._token, "Content-Type": "application/json"})
        raw = None
        for attempt in (1, 2):
            try:
                self.requests += 1
                self.bytes_sent += len(body)
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    raw = resp.read()
                self.bytes_received += len(raw)
                break
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read()[:300].decode("utf-8", "replace")
                except Exception:              # noqa: BLE001
                    pass
                raise SourceError(f"upstash HTTP {e.code}: {scrub(detail) or e.reason}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                if attempt == 2:
                    raise SourceError("upstash unreachable: " + scrub(getattr(e, "reason", e))) from None
                time.sleep(1.0)
        try:
            results = json.loads(raw)
        except ValueError:
            raise SourceError("upstash returned something that is not JSON") from None
        if not isinstance(results, list) or len(results) != len(cmds):
            raise SourceError("upstash pipeline answered with the wrong shape")
        out = []
        for cmd, r in zip(cmds, results):
            if not isinstance(r, dict) or r.get("error") is not None:
                err = r.get("error") if isinstance(r, dict) else r
                raise SourceError(f"upstash {cmd[0]} {cmd[1] if len(cmd) > 1 else ''}: {scrub(err)}")
            out.append(r.get("result"))
        self.commands += len(cmds)
        return out
