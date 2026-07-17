import asyncio
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from doyoutrade.api.app import create_app


class _FakeApprovalGate:
    async def list_pending(self):
        return []

    async def approve(self, approval_id):
        return SimpleNamespace(approval_id=approval_id, intent_id="i-1", status="approved")

    async def reject(self, approval_id, reason=None):
        return SimpleNamespace(approval_id=approval_id, intent_id="i-1", status="rejected")

    async def expire_pending(self):
        return []


class _FakeService:
    pass


class TestUploadAPI(unittest.TestCase):
    """TDD tests for POST /upload endpoint."""

    @classmethod
    def setUpClass(cls):
        cls._uploads_root = Path(__file__).resolve().parent.parent / "uploads"
        cls._uploads_root.mkdir(exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        if cls._uploads_root.exists():
            shutil.rmtree(cls._uploads_root)

    def setUp(self):
        self._tmp_files = []
        self._service = _FakeService()
        self._approval_gate = _FakeApprovalGate()
        self._app = create_app(
            service=self._service,
            approval_gate=self._approval_gate,
        )
        self._client = TestClient(self._app)

    def tearDown(self):
        for f in self._tmp_files:
            if f.exists():
                f.unlink()

    def _post_upload(self, filename: str, content: bytes, extra_files: list = None):
        """Helper to POST a multipart file upload."""
        files = {"file": (filename, io.BytesIO(content), "application/octet-stream")}
        if extra_files:
            for name, data in extra_files:
                files[name] = (name, io.BytesIO(data), "application/octet-stream")
        return self._client.post("/upload", files=files)

    def _assert_upload_cleanup(self, path: Path):
        """Assert that a partial file has been cleaned up."""
        self.assertFalse(
            path.exists(),
            f"Partial upload file {path} should have been cleaned up",
        )

    # ── test_upload_accepts_allowed_file ──────────────────────────────────────

    def test_upload_accepts_allowed_file(self):
        """POST a .txt returns 201 + {status:'ok', file_id, filename, mime_type, size_bytes}.

        The server's absolute path is intentionally NOT returned to the client
        (only the opaque file_id); the file still lands under uploads/.
        """
        content = b"hello world"
        response = self._post_upload("report.txt", content)

        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("file_id", data)
        self.assertNotIn("file_path", data)  # absolute path must not leak to client
        self.assertEqual(data["filename"], "report.txt")
        self.assertEqual(data["size_bytes"], len(content))

        # File actually exists on disk, resolved server-side from file_id.
        file_path = self._uploads_root / data["file_id"]
        self.assertTrue(file_path.exists(), f"Uploaded file not found at {file_path}")
        self.assertEqual(file_path.read_bytes(), content)
        self._tmp_files.append(file_path)

    # ── test_upload_rejects_blocked_extension ─────────────────────────────────

    def test_upload_rejects_blocked_extension(self):
        """POST .exe, .zip, .dll returns 400."""
        for ext in (".exe", ".zip", ".dll"):
            with self.subTest(ext=ext):
                response = self._post_upload(f"malicious{ext}", b"not a real exe")
                self.assertEqual(response.status_code, 400, f"Expected 400 for {ext}")

    # ── test_upload_rejects_too_large_file ────────────────────────────────────

    def test_upload_rejects_too_large_file(self):
        """POST >50 MB file returns 413, partial file cleaned up."""
        # Create >50 MB of data
        large_content = b"x" * (50 * 1024 * 1024 + 1)
        # Keep track of what path the file would have been saved to
        # We can't know the UUID in advance, so we watch the uploads directory
        uploads_dir = self._uploads_root
        before = set(p.name for p in uploads_dir.iterdir()) if uploads_dir.exists() else set()

        response = self._post_upload("huge.txt", large_content)

        self.assertEqual(response.status_code, 413, response.text)
        # No new files should remain (partial file cleaned up)
        after = set(p.name for p in uploads_dir.iterdir()) if uploads_dir.exists() else set()
        new_files = after - before
        self.assertEqual(
            new_files, set(), f"Unexpected files left in uploads: {new_files}"
        )

    # ── test_upload_saves_to_uploads_dir ──────────────────────────────────────

    def test_upload_saves_to_uploads_dir(self):
        """File saved as uploads/{uuid.hex}{ext}; file_id is that storage name."""
        content = b"test content"
        response = self._post_upload("data.csv", content)

        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        file_id = data["file_id"]
        file_path = self._uploads_root / file_id

        # Is under uploads/
        self.assertEqual(file_path.parent.resolve(), self._uploads_root.resolve())
        # file_id is 32 hex chars (uuid4 hex) + original extension
        self.assertEqual(len(file_path.stem), 32)
        self.assertTrue(file_path.stem.isalnum())
        self.assertEqual(file_path.suffix, ".csv")

        self._tmp_files.append(file_path)

    # ── test_upload_requires_auth ──────────────────────────────────────────────

    def test_upload_requires_auth(self):
        """No auth header returns 401 (only if existing API routes require auth)."""
        # This app has no auth on any route, so this test is a no-op placeholder.
        # The endpoint is intentionally unauthenticated to match the rest of the API.
        self.skipTest("API has no auth on any route; upload follows same pattern")


if __name__ == "__main__":
    unittest.main()
