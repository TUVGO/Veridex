from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from qa_brain.integrations.cme import run_cme_export
from qa_brain.integrations.docling import parse_assets_with_docling
from qa_brain.source_package import import_cme_export
from qa_brain.tasks import create_rich_extraction_tasks
from qa_brain.io import read_json, text_hash, write_json


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="qa-brain-integrations-"))
        self.addCleanup(shutil.rmtree, self.root)

    def _page(self, path, page_id, title, body, *, space="VW", version=1):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\n"
            f"confluence_page_id: '{page_id}'\n"
            f"confluence_space_key: {space}\n"
            f"confluence_version: {version}\n"
            'confluence_last_modified: "2026-09-08T00:00:00+08:00"\n'
            f"confluence_webui_url: https://confluence.example/pages/{page_id}\n"
            "---\n"
            f"# {title}\n\n{body}\n",
            encoding="utf-8",
        )

    def test_cme_import_preserves_tables_links_assets_and_parent(self):
        export = self.root / "export"
        home = export / "VW" / "Home.md"
        child = export / "VW" / "Home" / "Child.md"
        attachments = export / "VW" / "attachments"
        attachments.mkdir(parents=True)
        (attachments / "10.png").write_bytes(b"fake-image")
        (attachments / "11.xlsx").write_bytes(b"fake-xlsx")
        self._page(
            home, "1", "精准维保线索下发",
            "[子页面](Home/Child.md)\n\n"
            "![流程图](attachments/10.png)\n\n"
            "[字段字典](attachments/11.xlsx)\n\n"
            "## 字段表\n\n"
            "| 字段名 | 类型 | 字段描述 |\n"
            "| --- | --- | --- |\n"
            "| vin | String | 车架号 |\n",
        )
        self._page(child, "2", "详细设计", "正文")

        sources = self.root / "sources"
        result = import_cme_export(export, sources, self.root / "inventory.json")
        self.assertEqual(len(result["pages"]), 2)
        meta = read_json(sources / "VW" / "1" / "metadata.json")
        self.assertEqual(meta["table_count"], 1)
        self.assertEqual(meta["asset_count"], 2)
        self.assertFalse(meta["source_complete"])
        self.assertTrue(meta["visual_review_required"])
        links = read_json(sources / "VW" / "1" / "links.json")
        self.assertEqual(links[0]["target_page_id"], "2")
        tables = read_json(sources / "VW" / "1" / "tables.json")
        self.assertEqual(tables[0]["headers"], ["字段名", "类型", "字段描述"])
        self.assertEqual(tables[0]["rows"][0][0], "vin")
        self.assertEqual(read_json(sources / "VW" / "2" / "metadata.json")["parent_id"], "1")

    def test_cme_runner_sets_structured_settings_without_forwarding_qa_credentials(self):
        captured = {}
        def runner(args, **kwargs):
            captured["args"] = args
            captured["env"] = kwargs["env"]
            captured["shell"] = kwargs["shell"]
            return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

        with patch.dict(os.environ, {"QA_CONFLUENCE_PASSWORD": "secret", "PATH": os.environ.get("PATH", "")}, clear=False):
            run_cme_export(
                ["https://confluence.example/pages/viewpage.action?pageId=1"],
                self.root / "cme", cme_executable=sys.executable, runner=runner,
            )
        self.assertIn("pages-with-descendants", captured["args"])
        self.assertEqual(captured["env"]["CME_EXPORT__ATTACHMENTS_EXPORT"], "all")
        self.assertEqual(captured["env"]["CME_EXPORT__PAGE_METADATA_IN_FRONTMATTER"], "true")
        self.assertNotIn("QA_CONFLUENCE_PASSWORD", captured["env"])
        self.assertFalse(captured["shell"])

    def test_cme_http_requires_both_explicit_risk_flags(self):
        with self.assertRaisesRegex(ValueError, "allow-http"):
            run_cme_export(
                ["http://confluence.example/pages/viewpage.action?pageId=1"],
                self.root / "cme", cme_executable=sys.executable, runner=lambda *a, **k: None,
            )
        with self.assertRaisesRegex(ValueError, "allow-insecure-auth"):
            run_cme_export(
                ["http://confluence.example/pages/viewpage.action?pageId=1"],
                self.root / "cme", cme_executable=sys.executable, allow_http=True,
                runner=lambda *a, **k: None,
            )

    def test_docling_adapter_updates_asset_and_source_completeness(self):
        sources = self.root / "sources"
        page = sources / "VW" / "1"
        page.mkdir(parents=True)
        attachment = self.root / "a.xlsx"
        attachment.write_bytes(b"xlsx")
        write_json(page / "metadata.json", {
            "space": "VW", "page_id": "1", "missing_asset_count": 0,
            "pending_asset_count": 1, "source_complete": False, "source_incomplete": True,
        })
        write_json(page / "assets.json", [{
            "kind": "attachment", "label": "字段字典", "target": "a.xlsx",
            "resolved_path": str(attachment), "exists": True, "extension": ".xlsx",
            "sha256": "abc", "parse_status": "pending", "needs_visual_review": False,
        }])

        def runner(args, **kwargs):
            output = Path(args[args.index("--output") + 1])
            output.mkdir(parents=True, exist_ok=True)
            (output / "a.md").write_text("# parsed", encoding="utf-8")
            (output / "a.json").write_text("{}", encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        result = parse_assets_with_docling(
            sources, self.root / "docling", docling_executable=sys.executable, runner=runner,
        )
        self.assertEqual(result["parsed"], 1)
        self.assertEqual(read_json(page / "assets.json")[0]["parse_status"], "parsed")
        meta = read_json(page / "metadata.json")
        self.assertTrue(meta["source_complete"])
        self.assertFalse(meta["source_incomplete"])

    def test_rich_task_requires_table_attachment_outputs_and_visual_reads(self):
        sources = self.root / "sources"
        page = sources / "VW" / "1"
        page.mkdir(parents=True)
        image = self.root / "flow.png"
        image.write_bytes(b"img")
        parsed = self.root / "parsed.md"
        parsed.write_text("附件解析", encoding="utf-8")
        write_json(page / "metadata.json", {
            "page_id": "1", "title": "需求", "space": "VW", "version": 1,
            "updated_at": "2026-09-08", "content_hash": text_hash("正文"),
            "raw_status": "active", "source_incomplete": False,
            "source_complete": True, "visual_review_required": True,
        })
        (page / "content.md").write_text("正文", encoding="utf-8")
        write_json(page / "tables.json", [{"table_id": "table-001", "headers": ["字段名"], "rows": [["vin"]]}])
        write_json(page / "links.json", [{"kind": "page", "target_page_id": "2"}])
        write_json(page / "assets.json", [
            {"exists": True, "needs_visual_review": True, "resolved_path": str(image), "parse_status": "parsed", "parser_output": {}},
            {"exists": True, "needs_visual_review": False, "resolved_path": str(self.root / "a.xlsx"), "parse_status": "parsed",
             "parser_output": {"markdown": str(parsed), "json": None}},
        ])
        tasks = self.root / "tasks"
        create_rich_extraction_tasks(sources, tasks)
        task = read_json(tasks / "VW" / "1" / "task.json")
        self.assertEqual(task["schema_version"], 2)
        self.assertEqual(task["structured_sources"]["tables"][0]["rows"][0][0], "vin")
        self.assertIn(str(image), task["required_visual_reads"])
        self.assertIn(str(parsed), task["required_reads"])
        self.assertIn("MUST inspect", task["instruction"])


if __name__ == "__main__":
    unittest.main()
