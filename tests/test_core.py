import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from doxie_filer import core


class TestDoxieFilerCore(unittest.TestCase):
    def test_is_ocr_low_quality(self):
        self.assertTrue(core.is_ocr_low_quality(""))
        self.assertTrue(core.is_ocr_low_quality("short text"))
        self.assertTrue(core.is_ocr_low_quality("Vukdlo T B'JUBJY MO/eeQV QCVS Ve Cfl(?vo Hjfl/e/e E0GS"))
        good = (
            "Acme Insurance Company Policy Declarations. "
            "This renewal summary confirms your coverage effective November 10, 2026 "
            "for the household vehicles listed on page two of this statement."
        )
        self.assertFalse(core.is_ocr_low_quality(good))

    def test_resolve_folder_template_year(self):
        self.assertEqual(
            core.resolve_folder_template("Shared/Health/Receipts/{YEAR}", "Service Date: 10/30/2025"),
            "Shared/Health/Receipts/2025",
        )
        self.assertEqual(
            core.resolve_folder_template("Shared/Auto/Insurance", "2025"),
            "Shared/Auto/Insurance",
        )

    def test_match_rules_and_person_aliases(self):
        profile = {"people": {"Alice": ["alice", "alison"]}}
        rules = [
            {
                "id": "r1",
                "name": "Alice School Plans",
                "when": "School IEP or evaluation documents for Alice",
                "keywords": ["iep", "evaluation"],
                "person": "Alice",
                "folder": "Shared/Kids/Alice/IEP",
                "enabled": True,
            }
        ]
        hits = core.match_rules(
            "Annual IEP Evaluation report for Alison Smith",
            "scan.pdf",
            rules=rules,
            profile=profile,
        )
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["folder"], "Shared/Kids/Alice/IEP")
        self.assertTrue(hits[0]["person_match"])
        self.assertIn("iep", hits[0]["matched_keywords"])

    def test_browse_and_move_and_undo(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            inbox = root / "Scans"
            archive = root / "Archive"
            state_dir = root / "state"
            inbox.mkdir()
            (archive / "Health" / "Receipts").mkdir(parents=True)
            (archive / "Taxes" / "2026 Taxes").mkdir(parents=True)
            (archive / "Scans").mkdir(parents=True)

            sample_pdf = inbox / "2026-11-24_test.pdf"
            sample_pdf.write_bytes(b"%PDF-1.4\n%test\n")

            cfg = {
                "inbox_dir": inbox,
                "target_root": archive,
                "lmstudio_url": "http://127.0.0.1:1234",
                "model_fast": "qwen/qwen3.5-4b",
                "model_strong": "qwen/qwen3.8-27b",
                "listen_addr": "127.0.0.1:8401",
                "context_length": 16384,
                "profile": core.load_profile(root / "missing.json"),
            }

            browsed = core.browse_folder(archive, "", profile=cfg["profile"])
            names = [d["name"] for d in browsed["dirs"]]
            self.assertIn("Health", names)
            self.assertNotIn("Scans", names)

            with (
                mock.patch.object(core, "STATE_DIR", state_dir),
                mock.patch.object(core, "HISTORY_FILE", state_dir / "filing-history.jsonl"),
                mock.patch.object(core, "CACHE_FILE", state_dir / "rec-cache.json"),
                mock.patch.object(core, "RULES_FILE", state_dir / "filing-rules.json"),
            ):
                entry = core.approve_move(
                    sample_pdf,
                    relative_folder="Health/Receipts",
                    filename="2026-11-24 Lab Bill.pdf",
                    cfg=cfg,
                    recommendation={"recommended_folder": "Taxes/2026 Taxes", "summary": "Lab bill", "cues": ["lab", "bill"]},
                    user_note="Medical receipts go here",
                    save_rule=True,
                )
                self.assertFalse(sample_pdf.exists())
                self.assertTrue(Path(entry["dest_path"]).is_file())
                self.assertTrue(entry["user_overrode"])
                self.assertIsNotNone(entry["created_rule"])
                self.assertEqual(len(core.load_rules()), 1)

                undone = core.undo_move(entry["id"])
                self.assertTrue(Path(undone["restored_path"]).is_file())
                self.assertFalse(Path(entry["dest_path"]).exists())

    def test_select_prompt_folders_keeps_pinned_deep_folders(self):
        all_folders = [
            "Shared",
            "Shared/Kids",
            "Shared/Kids/Alice",
            "Shared/Kids/Alice/Elementary",
            "Shared/Kids/Alice/Elementary/IEP",
            "Scans",
        ]
        profile = {"prune_subtrees": ["Scans"]}
        chosen = core.select_prompt_folders(
            all_folders,
            doc_text="unrelated text",
            pdf_name="doc.pdf",
            pinned_folders={"Shared/Kids/Alice/Elementary/IEP"},
            profile=profile,
        )
        self.assertIn("Shared/Kids/Alice/Elementary/IEP", chosen)
        self.assertNotIn("Scans", chosen)


if __name__ == "__main__":
    unittest.main()
