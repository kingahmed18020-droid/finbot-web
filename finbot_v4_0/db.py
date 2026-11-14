"""قاعدة بيانات SQLite: أكواد محفوظة + صلاة + سجل مهام + إحصائيات الأدمن."""
import sqlite3
import threading
import time

_lock = threading.Lock()
_conn = None


def init(path="bot.db"):
    global _conn
    _conn = sqlite3.connect(path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    with _lock:
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                tg_id      INTEGER PRIMARY KEY,
                tg_name    TEXT,
                username   TEXT,
                first_seen INTEGER,
                last_seen  INTEGER,
                is_banned  INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS counters (
                name  TEXT PRIMARY KEY,
                value INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id      INTEGER,
                started    INTEGER,
                finished   INTEGER,
                status     TEXT,
                questions  INTEGER DEFAULT 0,
                activities INTEGER DEFAULT 0,
                note       TEXT
            );
            INSERT OR IGNORE INTO counters(name, value) VALUES ('salawat', 0);
            """
        )
        _conn.commit()


# ── مستخدمين ──────────────────────────────────────────────────────────────────

def touch_user(tg_id, tg_name=""):
    now = int(time.time())
    with _lock:
        _conn.execute(
            "INSERT INTO users(tg_id, tg_name, first_seen, last_seen) VALUES (?,?,?,?) "
            "ON CONFLICT(tg_id) DO UPDATE SET tg_name=excluded.tg_name, last_seen=excluded.last_seen",
            (tg_id, tg_name, now, now),
        )
        _conn.commit()


def save_username(tg_id, username):
    with _lock:
        _conn.execute("UPDATE users SET username=? WHERE tg_id=?", (username, tg_id))
        _conn.commit()


def get_username(tg_id):
    with _lock:
        row = _conn.execute("SELECT username FROM users WHERE tg_id=?", (tg_id,)).fetchone()
    return row["username"] if row and row["username"] else None


def is_banned(tg_id):
    with _lock:
        row = _conn.execute("SELECT is_banned FROM users WHERE tg_id=?", (tg_id,)).fetchone()
    return bool(row and row["is_banned"])


def ban_user(tg_id):
    with _lock:
        _conn.execute("UPDATE users SET is_banned=1 WHERE tg_id=?", (tg_id,))
        _conn.commit()


def unban_user(tg_id):
    with _lock:
        _conn.execute("UPDATE users SET is_banned=0 WHERE tg_id=?", (tg_id,))
        _conn.commit()


def get_all_users(limit=200):
    with _lock:
        rows = _conn.execute(
            "SELECT tg_id, tg_name, username, first_seen, last_seen, is_banned "
            "FROM users ORDER BY last_seen DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ── صلاة على النبي ────────────────────────────────────────────────────────────

def add_salawat():
    with _lock:
        _conn.execute("UPDATE counters SET value=value+1 WHERE name='salawat'")
        value = _conn.execute("SELECT value FROM counters WHERE name='salawat'").fetchone()[0]
        _conn.commit()
    return value


def get_salawat():
    with _lock:
        return _conn.execute("SELECT value FROM counters WHERE name='salawat'").fetchone()[0]


# ── مهام (jobs) ───────────────────────────────────────────────────────────────

def log_job(tg_id, started, status, questions, activities, note=""):
    with _lock:
        _conn.execute(
            "INSERT INTO jobs(tg_id, started, finished, status, questions, activities, note) "
            "VALUES (?,?,?,?,?,?,?)",
            (tg_id, int(started), int(time.time()), status, questions, activities, note[:300]),
        )
        _conn.commit()


def get_stats():
    """إحصائيات الأدمن الشاملة."""
    with _lock:
        total_users  = _conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        banned_users = _conn.execute("SELECT COUNT(*) FROM users WHERE is_banned=1").fetchone()[0]
        total_jobs   = _conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        done_jobs    = _conn.execute("SELECT COUNT(*) FROM jobs WHERE status='finished'").fetchone()[0]
        total_q      = _conn.execute("SELECT COALESCE(SUM(questions),0)  FROM jobs").fetchone()[0]
        total_a      = _conn.execute("SELECT COALESCE(SUM(activities),0) FROM jobs").fetchone()[0]
        active_today = _conn.execute(
            "SELECT COUNT(DISTINCT tg_id) FROM users WHERE last_seen >= ?",
            (int(time.time()) - 86400,)
        ).fetchone()[0]
        recent_jobs  = _conn.execute(
            "SELECT j.tg_id, u.tg_name, j.started, j.finished, j.status, "
            "j.questions, j.activities FROM jobs j "
            "LEFT JOIN users u ON u.tg_id=j.tg_id "
            "ORDER BY j.id DESC LIMIT 10"
        ).fetchall()
    return {
        "total_users":  total_users,
        "banned_users": banned_users,
        "active_today": active_today,
        "total_jobs":   total_jobs,
        "done_jobs":    done_jobs,
        "total_q":      total_q,
        "total_a":      total_a,
        "recent_jobs":  [dict(r) for r in recent_jobs],
    }
