import importlib.machinery
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

RENAME_BIN = Path(__file__).resolve().parent.parent / "bin" / "doxie-rename"
_loader = importlib.machinery.SourceFileLoader("doxie_rename_mod", str(RENAME_BIN))
_spec = importlib.util.spec_from_loader("doxie_rename_mod", _loader)
assert _spec is not None
doxie_rename = importlib.util.module_from_spec(_spec)
_loader.exec_module(doxie_rename)


class TestDoxieRename(unittest.TestCase):
    def test_is_already_clean_iso(self):
        self.assertTrue(doxie_rename.is_already_clean_iso("2026-11-20 Acme Auto Insurance Renewal.pdf"))
        self.assertFalse(doxie_rename.is_already_clean_iso("2023-10-05 08-15-54.pdf"))
        self.assertFalse(doxie_rename.is_already_clean_iso("2026-11-24_10-45-35_cvs-pharmacy.pdf"))
        self.assertFalse(doxie_rename.is_already_clean_iso("2024-04-04 Doxie Scan.pdf"))
        self.assertFalse(doxie_rename.is_already_clean_iso("Doxie - 2024-09-30 09-50-13.pdf"))

    def test_try_compact_ymd_standardize(self):
        self.assertEqual(
            doxie_rename.try_compact_ymd_standardize("20170320 Inspection and Rear Brake Pads.pdf"),
            "2017-03-20 Inspection and Rear Brake Pads.pdf",
        )
        self.assertIsNone(doxie_rename.try_compact_ymd_standardize("20130519 Sent Fax M0600434131394996700.pdf"))
        self.assertIsNone(doxie_rename.try_compact_ymd_standardize("20170320.pdf"))

    def test_safe_rename_collision_and_undo(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            existing = d / "2026-01-01 Receipt.pdf"
            existing.write_bytes(b"%PDF-1.4\n1\n")
            src = d / "Doxie - 2026-01-01.pdf"
            src.write_bytes(b"%PDF-1.4\n2\n")

            dest = doxie_rename.safe_rename_in_place(src, "2026-01-01 receipt.pdf", dry_run=False)
            self.assertEqual(dest.parent, d)
            self.assertEqual(dest.name, "2026-01-01 receipt (2).pdf")
            self.assertFalse(src.exists())
            self.assertTrue(dest.is_file())

            log_path = d / "run.jsonl"
            log_path.write_text(
                json.dumps({"status": "renamed", "old_path": str(src), "new_path": str(dest)}) + "\n",
                encoding="utf-8",
            )
            doxie_rename.run_undo(log_path, dry_run=False)
            self.assertTrue(src.is_file())
            self.assertFalse(dest.exists())


if __name__ == "__main__":
    unittest.main()
