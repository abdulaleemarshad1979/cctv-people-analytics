"""Permanent IN/OUT history: one row per camera per local hour in a SQLite file next to the camera settings.

Counts used to live only in memory, so every restart started from zero. The app now adds each camera's new crossings to
this file every few seconds; on start-up it reads the lifetime totals back, so the totals shown on the site only ever go up,
and the hourly report is built from the same rows.
"""
import sqlite3
from datetime import datetime, timedelta, timezone


class History:
    def __init__(self, path, utc_offset_min=330):
        self.path = str(path)
        self.tz = timezone(timedelta(minutes=utc_offset_min))   # report hours are local time (IST has no daylight saving)
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS hourly (
                camera TEXT NOT NULL, hour TEXT NOT NULL,
                in_count INTEGER NOT NULL DEFAULT 0, out_count INTEGER NOT NULL DEFAULT 0,
                people_sum INTEGER NOT NULL DEFAULT 0, people_n INTEGER NOT NULL DEFAULT 0, people_max INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (camera, hour))""")

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        return _Closing(db)

    def hour_key(self, when=None):
        return (when or datetime.now(timezone.utc)).astimezone(self.tz).strftime("%Y-%m-%d %H:00")

    def add_counts(self, camera, d_in, d_out, when=None):
        with self._db() as db:
            db.execute("""INSERT INTO hourly (camera, hour, in_count, out_count) VALUES (?, ?, ?, ?)
                          ON CONFLICT(camera, hour) DO UPDATE SET in_count = in_count + excluded.in_count,
                                                                  out_count = out_count + excluded.out_count""",
                       (camera, self.hour_key(when), d_in, d_out))

    def add_people(self, camera, people, when=None):
        with self._db() as db:
            db.execute("""INSERT INTO hourly (camera, hour, people_sum, people_n, people_max) VALUES (?, ?, ?, 1, ?)
                          ON CONFLICT(camera, hour) DO UPDATE SET people_sum = people_sum + excluded.people_sum,
                                                                  people_n = people_n + 1,
                                                                  people_max = MAX(people_max, excluded.people_max)""",
                       (camera, self.hour_key(when), people, people))

    def totals(self):
        """{camera: (in, out)} over all time."""
        with self._db() as db:
            return {c: (i, o) for c, i, o in db.execute("SELECT camera, SUM(in_count), SUM(out_count) FROM hourly GROUP BY camera")}

    def rows(self, first_day, last_day):
        """Hourly rows for local dates first_day..last_day (YYYY-MM-DD, inclusive): (hour, camera, in, out, people avg, people max)."""
        with self._db() as db:
            return [(h, c, i, o, round(s / n, 1) if n else None, m if n else None)
                    for h, c, i, o, s, n, m in db.execute(
                        """SELECT hour, camera, in_count, out_count, people_sum, people_n, people_max FROM hourly
                           WHERE hour >= ? AND hour < ? ORDER BY hour, camera""",
                        (first_day + " 00:00", (datetime.strptime(last_day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d") + " 00:00"))]


class _Closing:
    """sqlite3 connection as a context manager that commits and then really closes (the stdlib one only commits)."""

    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, *exc):
        try:
            if exc[0] is None:
                self.db.commit()
            else:
                self.db.rollback()
        finally:
            self.db.close()
