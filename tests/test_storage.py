import tempfile
import time
import unittest
from pathlib import Path

from helix_mcp.config import ConfigError, Settings
from helix_mcp.storage import Store, StoreError


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "data")

    def tearDown(self):
        self.temp.cleanup()

    def test_idempotency_and_single_active_lease(self):
        first = self.store.enqueue("win", "hx_discover", {}, "req-1")
        self.assertEqual(first["id"], self.store.enqueue("win", "hx_discover", {}, "req-1")["id"])
        with self.assertRaises(StoreError):
            self.store.enqueue("win", "hx_launch", {}, "req-1")
        second = self.store.enqueue("win", "hx_discover", {}, "req-2")
        claimed = self.store.claim("win")
        self.assertEqual(claimed["id"], first["id"])
        self.assertIsNone(self.store.claim("win"))
        self.store.complete(first["id"], "win", claimed["lease_token"], True, {"ok": True}, None)
        self.assertEqual(self.store.claim("win")["id"], second["id"])

    def test_lease_expiry_never_requeues_and_late_result_rejected(self):
        job = self.store.enqueue("win", "hx_launch", {}, "lease-test")
        claimed = self.store.claim("win")
        with self.store.connect() as conn:
            conn.execute("UPDATE jobs SET lease_deadline=? WHERE id=?", (time.time() - 1, job["id"]))
        self.assertIsNone(self.store.claim("win"))
        self.assertEqual(self.store.get_job(job["id"])["status"], "unknown")
        with self.assertRaises(StoreError):
            self.store.complete(job["id"], "win", claimed["lease_token"], True, {}, None)

    def test_expired_queue_and_forged_lease(self):
        job = self.store.enqueue("win", "hx_discover", {}, "expire-test", queue_seconds=-1)
        self.assertEqual(self.store.get_job(job["id"])["status"], "expired")
        job = self.store.enqueue("win", "hx_discover", {}, "forged-test")
        self.store.claim("win")
        with self.assertRaises(StoreError):
            self.store.complete(job["id"], "win", "invalid-lease", True, {}, None)

    def test_online_backup_is_readable_and_no_overwrite(self):
        artifact = self.store.add_artifact("template", "fixture", "{}", {})
        output = Path(self.temp.name) / "backup" / "helix.sqlite3"
        self.store.backup(output)
        restored = Store(output.parent)
        self.assertEqual(restored.get_artifact(artifact["id"])["content"], "{}")
        with self.assertRaises(StoreError):
            self.store.backup(output)

    def test_secrets_redacted_and_roles_must_differ(self):
        settings = Settings(Path(self.temp.name), "m" * 48, "w" * 48)
        self.assertNotIn(settings.mcp_token, repr(settings))
        self.assertNotIn(settings.worker_token, repr(settings))
        with self.assertRaises(ConfigError):
            Settings(Path(self.temp.name), "x" * 48, "x" * 48)


if __name__ == "__main__":
    unittest.main()
