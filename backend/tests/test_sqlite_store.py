import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import sqlite_store


class TestSQLiteStore(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "dashboard.sqlite3"
        self.environment_patch = patch.dict(
            os.environ, {"SQLITE_PATH": str(self.database_path)}
        )
        self.environment_patch.start()
        sqlite_store.initialize()

    def tearDown(self):
        self.environment_patch.stop()
        self.temporary_directory.cleanup()

    def test_task_replacements_keep_order_and_replace_previous_rows(self):
        first = [{"Job ID": "A", "Task": "First"}, {"Job ID": "B", "Task": "Second"}]
        second = [{"Job ID": "A", "Task": "Updated"}]

        sqlite_store.replace_rows("tasks", "Job ID", first)
        sqlite_store.replace_rows("tasks", "Job ID", second)

        self.assertEqual(sqlite_store.get_rows("tasks"), second)

    def test_empty_task_replacement_clears_rows(self):
        sqlite_store.replace_rows("archived_tasks", "Job ID", [{"Job ID": "A"}])
        sqlite_store.replace_rows("archived_tasks", "Job ID", [])
        self.assertEqual(sqlite_store.get_rows("archived_tasks"), [])

    def test_task_ids_must_be_nonempty_and_case_insensitively_unique(self):
        with self.assertRaises(ValueError):
            sqlite_store.replace_rows("tasks", "Job ID", [{"Job ID": "A"}, {"Job ID": "a"}])
        with self.assertRaises(ValueError):
            sqlite_store.replace_rows("tasks", "Job ID", [{"Job ID": " "}])

    def test_resource_create_update_deactivate_and_delete(self):
        resource = {"id": "M1", "name": "Mill", "active": True, "data": {"type": "CNC"}}
        self.assertTrue(sqlite_store.save_resource("machines", resource, create_only=True))
        self.assertFalse(sqlite_store.save_resource("machines", resource, create_only=True))
        self.assertEqual(sqlite_store.get_resources("machines")[0]["data"], {"type": "CNC"})

        updated = {**resource, "name": "Updated mill"}
        self.assertTrue(sqlite_store.save_resource("machines", updated))
        self.assertTrue(sqlite_store.set_resource_active("machines", "M1", False))
        self.assertEqual(sqlite_store.get_resources("machines")[0]["active"], False)
        self.assertEqual(sqlite_store.get_resources("machines", include_inactive=False), [])
        self.assertTrue(sqlite_store.remove_resource("machines", "M1"))
        self.assertEqual(sqlite_store.get_resources("machines"), [])

    def test_latest_schedule_is_upserted(self):
        sqlite_store.save_latest_schedule({"runs": 1})
        sqlite_store.save_latest_schedule({"runs": 2})
        self.assertEqual(sqlite_store.get_latest_schedule(), {"runs": 2})

    def test_database_persists_between_connections(self):
        sqlite_store.save_latest_schedule({"status": "saved"})
        sqlite_store.check_connection()
        self.assertEqual(sqlite_store.get_latest_schedule(), {"status": "saved"})
        self.assertTrue(self.database_path.exists())

    def test_only_supported_row_collections_are_allowed(self):
        with self.assertRaises(ValueError):
            sqlite_store.get_rows("resources")
        with self.assertRaises(ValueError):
            sqlite_store.replace_rows("resources", "id", [])


if __name__ == "__main__":
    unittest.main()
