"""Test package. TempDatabaseTestCase gives every test its own fresh SQLite file."""
import os
import tempfile
import unittest

import config
import database


class TempDatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._real_db_path = config.DB_PATH
        config.DB_PATH = os.path.join(self._tmp.name, "test.db")
        database.init_db()

    def tearDown(self):
        config.DB_PATH = self._real_db_path
        self._tmp.cleanup()
