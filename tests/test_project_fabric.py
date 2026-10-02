from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from project_fabric.core import JAKARTA, JsonTTLCache, as_float, env_bool, env_int, in_time_windows


class ProjectFabricTests(unittest.TestCase):
    def test_env_bool(self):
        with patch.dict(os.environ, {"FLAG": "true"}, clear=False):
            self.assertTrue(env_bool("FLAG"))
        with patch.dict(os.environ, {"FLAG": "0"}, clear=False):
            self.assertFalse(env_bool("FLAG"))

    def test_env_int_bounds(self):
        with patch.dict(os.environ, {"LIMIT": "7"}, clear=False):
            self.assertEqual(env_int("LIMIT", 3, minimum=1, maximum=10), 7)

    def test_time_windows(self):
        current = datetime(2026, 10, 2, 9, 30, tzinfo=JAKARTA)
        self.assertTrue(in_time_windows(current, "09:00-12:00,13:30-16:00"))
        self.assertFalse(in_time_windows(current, "13:30-16:00"))

    def test_cache_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            cache = JsonTTLCache(Path(temp))
            cache.set("stock-summary", {"rows": [1, 2, 3]})
            self.assertEqual(cache.get("stock-summary", 60), {"rows": [1, 2, 3]})

    def test_safe_float(self):
        self.assertEqual(as_float("1,234.5"), 1234.5)
        self.assertEqual(as_float(None), 0.0)


if __name__ == "__main__":
    unittest.main()
