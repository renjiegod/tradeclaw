"""Tests for the structured chat attachment contract.

Covers the file_id -> path resolution (including the arbitrary-path /
traversal rejection that the old text-embedding approach left open),
normalization of the client-supplied attachments array, and the model-visible
rendering used both for the live turn and history replay.
"""

import unittest
import uuid

from doyoutrade.assistant import attachments as A


class AttachmentContractTests(unittest.TestCase):
    def setUp(self):
        A.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        self._created = []

    def tearDown(self):
        for p in self._created:
            if p.exists():
                p.unlink()

    def _make_upload(self, ext=".pdf", data=b"x"):
        file_id = uuid.uuid4().hex + ext
        path = A.UPLOADS_DIR / file_id
        path.write_bytes(data)
        self._created.append(path)
        return file_id, path

    # ── resolve_upload_path ──────────────────────────────────────────────

    def test_resolve_valid_file_id(self):
        file_id, path = self._make_upload()
        self.assertEqual(A.resolve_upload_path(file_id), path.resolve())

    def test_resolve_file_id_without_extension(self):
        file_id, path = self._make_upload(ext="")
        self.assertEqual(A.resolve_upload_path(file_id), path.resolve())

    def test_resolve_rejects_traversal_and_absolute(self):
        for bad in [
            "../etc/passwd",
            "/etc/passwd",
            "abc",  # not a uuid hex
            "g" * 32 + ".pdf",  # non-hex chars
            "../" + "a" * 32 + ".pdf",
            "",
            None,
            123,
        ]:
            with self.subTest(bad=bad):
                with self.assertRaises(A.AttachmentError) as ctx:
                    A.resolve_upload_path(bad)
                self.assertEqual(ctx.exception.error_code, "invalid_attachment_file_id")

    def test_resolve_missing_file(self):
        with self.assertRaises(A.AttachmentError) as ctx:
            A.resolve_upload_path(uuid.uuid4().hex + ".pdf")
        self.assertEqual(ctx.exception.error_code, "attachment_not_found")

    # ── normalize_attachments ────────────────────────────────────────────

    def test_normalize_valid(self):
        file_id, _ = self._make_upload()
        out = A.normalize_attachments(
            [{"file_id": file_id, "filename": " a.pdf ", "mime_type": "application/pdf", "size_bytes": 3}]
        )
        self.assertEqual(
            out,
            [{"file_id": file_id, "filename": "a.pdf", "mime_type": "application/pdf", "size_bytes": 3}],
        )

    def test_normalize_none_and_empty(self):
        self.assertEqual(A.normalize_attachments(None), [])
        self.assertEqual(A.normalize_attachments([]), [])

    def test_normalize_drops_optional_when_absent_or_bad(self):
        file_id, _ = self._make_upload()
        out = A.normalize_attachments(
            [{"file_id": file_id, "filename": "a.pdf", "size_bytes": -1, "mime_type": ""}]
        )
        self.assertEqual(out, [{"file_id": file_id, "filename": "a.pdf"}])

    def test_normalize_rejects_bad_shapes(self):
        file_id, _ = self._make_upload()
        cases = [
            ("not a list", {"file_id": file_id}),  # not a list
            ([{"file_id": file_id}], None),  # missing filename
            ([{"filename": "a.pdf"}], None),  # missing/invalid file_id
            ([{"file_id": "bogus", "filename": "a.pdf"}], None),  # bad file_id
            (["nope"], None),  # non-dict item
        ]
        for raw, _ in cases:
            with self.subTest(raw=raw):
                with self.assertRaises(A.AttachmentError):
                    A.normalize_attachments(raw)

    # ── render / compose ─────────────────────────────────────────────────

    def test_render_injects_absolute_path(self):
        file_id, path = self._make_upload()
        rendered = A.render_attachments_for_model([{"file_id": file_id, "filename": "报表.pdf"}])
        self.assertEqual(rendered, f"[Uploaded file: 报表.pdf, path: {path.resolve()}]")

    def test_render_missing_file_degrades_gracefully(self):
        rendered = A.render_attachments_for_model(
            [{"file_id": uuid.uuid4().hex + ".pdf", "filename": "gone.pdf"}]
        )
        self.assertEqual(rendered, "[Uploaded file: gone.pdf (unavailable)]")

    def test_compose_variants(self):
        file_id, path = self._make_upload()
        atts = [{"file_id": file_id, "filename": "a.pdf"}]
        block = f"[Uploaded file: a.pdf, path: {path.resolve()}]"
        # text + attachments
        self.assertEqual(A.compose_model_user_text("hi", atts), f"{block}\n\nhi")
        # attachments only
        self.assertEqual(A.compose_model_user_text("", atts), block)
        # text only
        self.assertEqual(A.compose_model_user_text("hi", []), "hi")
        self.assertEqual(A.compose_model_user_text("hi", None), "hi")


if __name__ == "__main__":
    unittest.main()
