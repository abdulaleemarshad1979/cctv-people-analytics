import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from history import History


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "history.db"

    def tearDown(self):
        self.dir.cleanup()

    def test_totals_survive_a_restart_and_only_grow(self):
        History(self.path).add_counts("cam01", 5, 2)
        again = History(self.path)               # "restart": a fresh object on the same file
        self.assertEqual(again.totals()["cam01"], (5, 2))
        again.add_counts("cam01", 3, 1)
        self.assertEqual(History(self.path).totals()["cam01"], (8, 3))

    def test_hours_are_local_time_and_split_per_hour(self):
        h = History(self.path, utc_offset_min=330)
        h.add_counts("cam01", 4, 0, datetime(2026, 11, 24, 18, 40, tzinfo=timezone.utc))   # 00:10 IST the next day
        h.add_counts("cam01", 1, 1, datetime(2026, 11, 24, 19, 5, tzinfo=timezone.utc))    # 00:35 IST: same hour, summed
        h.add_counts("cam01", 2, 0, datetime(2026, 11, 24, 20, 5, tzinfo=timezone.utc))    # 01:35 IST
        rows = h.rows("2026-11-25", "2026-11-25")
        self.assertEqual([(r[0], r[2], r[3]) for r in rows], [("2026-11-25 00:00", 5, 1), ("2026-11-25 01:00", 2, 0)])
        self.assertEqual(h.rows("2026-11-24", "2026-11-24"), [])

    def test_people_average_and_peak(self):
        h = History(self.path)
        for n in (10, 30, 20):
            h.add_people("cam02", n)
        (_, _, _, _, avg, peak), = h.rows("2000-01-01", "2100-01-01")
        self.assertEqual((avg, peak), (20.0, 30))


if __name__ == "__main__":
    unittest.main()
