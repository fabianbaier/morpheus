"""Tests for the context signal store (omnipresence context ingestion)."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from morpheus import db, signals


class _TempDB:
    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._p = [patch.object(db, "DB_DIR", root),
                   patch.object(db, "DB_PATH", root / "morpheus.db")]
        for p in self._p:
            p.start()
        return root

    def __exit__(self, *a):
        for p in reversed(self._p):
            p.stop()
        self._tmp.cleanup()


class AddSignalTest(unittest.TestCase):
    def test_add_and_latest_roundtrip(self):
        with _TempDB():
            sid = signals.add_signal("location", {"lat": 52.52, "lon": 13.405})
            got = signals.latest("location")
            self.assertEqual(got.id, sid)
            self.assertEqual(got.kind, "location")
            self.assertEqual(got.payload["lat"], 52.52)
            self.assertEqual(got.payload["lon"], 13.405)

    def test_location_ts_defaults_to_now(self):
        with _TempDB():
            before = time.time()
            signals.add_signal("location", {"lat": 1, "lon": 2})
            got = signals.latest("location")
            self.assertGreaterEqual(got.ts, before)
            self.assertGreaterEqual(got.payload["ts"], before)

    def test_location_payload_ts_becomes_signal_ts(self):
        with _TempDB():
            signals.add_signal("location", {"lat": 1, "lon": 2, "ts": 1234.5})
            got = signals.latest("location")
            self.assertEqual(got.ts, 1234.5)
            self.assertEqual(got.payload["ts"], 1234.5)

    def test_explicit_ts_argument_wins(self):
        with _TempDB():
            signals.add_signal("battery", {"percent": 80}, ts=99.0)
            self.assertEqual(signals.latest("battery").ts, 99.0)

    def test_location_rejects_bad_coordinates(self):
        bad = [
            {"lon": 13.4},                       # missing lat
            {"lat": "52.5", "lon": 13.4},        # string lat
            {"lat": True, "lon": 13.4},          # bool is not numeric
            {"lat": 91, "lon": 13.4},            # out of range
            {"lat": 52.5, "lon": -181},          # out of range
            {"lat": float("nan"), "lon": 13.4},  # NaN
        ]
        with _TempDB():
            for payload in bad:
                with self.assertRaises(ValueError, msg=payload):
                    signals.add_signal("location", payload)
            self.assertIsNone(signals.latest("location"))

    def test_location_rejects_negative_accuracy(self):
        with _TempDB():
            with self.assertRaises(ValueError):
                signals.add_signal("location", {"lat": 1, "lon": 2, "accuracy": -5})
            sid = signals.add_signal("location", {"lat": 1, "lon": 2, "accuracy": 12})
            self.assertEqual(signals.latest("location").id, sid)
            self.assertEqual(signals.latest("location").payload["accuracy"], 12.0)

    def test_rejects_bad_kind_and_payload(self):
        with _TempDB():
            with self.assertRaises(ValueError):
                signals.add_signal("", {"a": 1})
            with self.assertRaises(ValueError):
                signals.add_signal("no spaces", {"a": 1})
            with self.assertRaises(ValueError):
                signals.add_signal("battery", ["not", "a", "dict"])
            with self.assertRaises(ValueError):
                signals.add_signal("battery", {"blob": "x" * signals.PAYLOAD_MAX_CHARS})

    def test_kind_is_normalized(self):
        with _TempDB():
            signals.add_signal("  Location ", {"lat": 1, "lon": 2})
            self.assertIsNotNone(signals.latest("location"))
            self.assertEqual(signals.kinds(), ["location"])


class BoundedGrowthTest(unittest.TestCase):
    def test_prunes_oldest_per_kind_on_insert(self):
        with _TempDB():
            with patch.object(signals, "MAX_PER_KIND", 3):
                for i in range(5):
                    signals.add_signal("battery", {"percent": i})
                # other kinds are untouched by battery's pruning
                signals.add_signal("location", {"lat": 1, "lon": 2})
                recent = signals.recent("battery", limit=10)
            self.assertEqual(len(recent), 3)
            self.assertEqual([s.payload["percent"] for s in recent], [4, 3, 2])
            self.assertEqual(len(signals.recent("location", limit=10)), 1)


class ReadViewsTest(unittest.TestCase):
    def test_recent_is_newest_first_and_limited(self):
        with _TempDB():
            for i in range(4):
                signals.add_signal("activity", {"step": i})
            got = signals.recent("activity", limit=2)
            self.assertEqual([s.payload["step"] for s in got], [3, 2])

    def test_latest_per_kind(self):
        with _TempDB():
            signals.add_signal("battery", {"percent": 10})
            signals.add_signal("battery", {"percent": 20})
            signals.add_signal("location", {"lat": 1, "lon": 2})
            latest = signals.latest_per_kind()
            self.assertEqual([s.kind for s in latest], ["battery", "location"])
            self.assertEqual(latest[0].payload["percent"], 20)

    def test_latest_missing_kind_is_none(self):
        with _TempDB():
            self.assertIsNone(signals.latest("calendar_window"))


if __name__ == "__main__":
    unittest.main()
