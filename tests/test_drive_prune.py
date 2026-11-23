import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clipper.db import DB
from clipper.publishers import drive


class DrivePruneTests(unittest.TestCase):
    def test_posted_clips_leave_drive_others_stay(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:  # Windows: sqlite file still open
            db = DB(Path(tmp) / "t.db")
            ids = {}
            for status in ("posted", "approved", "ready", "posted", "rejected", "failed"):
                ids.setdefault(status, []).append(db.insert(
                    "clips", channel="x", status=status, source="s", created_at=1.0, updated_at=1.0,
                    drive_url=f"https://drive.google.com/file/d/F{len(ids)}{status}/view?usp=drivesdk"))
            deleted = []
            calls = {"n": 0}

            def fake_delete(cfg, link):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("Drive HTTP 500")  # first one fails: kept, retried next pass
                deleted.append(link)

            with mock.patch.object(drive, "enabled", return_value=True), \
                    mock.patch.object(drive, "delete", side_effect=fake_delete):
                self.assertEqual(drive.prune_posted(db, {}), 2)
                self.assertEqual(drive.prune_posted(db, {}), 1)  # the retry
                self.assertEqual(drive.prune_posted(db, {}), 0)  # nothing left
            urls = {r["status"]: [] for r in db.query("SELECT status FROM clips")}
            for r in db.query("SELECT status, drive_url FROM clips"):
                urls[r["status"]].append(r["drive_url"])
            self.assertEqual(urls["posted"], ["", ""])
            self.assertEqual(urls["rejected"], [""])
            self.assertTrue(urls["approved"][0] and urls["ready"][0])  # not posted yet: kept
            self.assertTrue(urls["failed"][0])  # kept for posting by hand
            self.assertEqual(len(deleted), 3)

    def test_file_id_from_link(self):
        with mock.patch.object(drive.yt, "_access_token", return_value="t"), \
                mock.patch.object(drive.yt, "_request", return_value=(404, {}, b"")) as req:
            drive.delete({}, "https://drive.google.com/file/d/AbC-12_x/view?usp=drivesdk")  # 404 = already gone
        self.assertTrue(req.call_args[0][1].endswith("/drive/v3/files/AbC-12_x"))
        self.assertEqual(req.call_args[0][0], "DELETE")


if __name__ == "__main__":
    unittest.main()
