from __future__ import annotations

from pathlib import Path
import subprocess

from ..io import read_json, safe_component, write_json
from ..source_package import DOCLING_EXTENSIONS
from .common import resolve_executable, sanitized_subprocess_env


def parse_assets_with_docling(
    source_root,
    output_root,
    *,
    docling_executable="docling",
    document_timeout=300,
    fail_fast=False,
    runner=subprocess.run,
):
    """Parse CME-referenced attachments locally with Docling; remote services stay disabled."""
    executable = resolve_executable(docling_executable)
    source_root = Path(source_root)
    output_root = Path(output_root)
    parsed = skipped = failed = unsupported = 0

    for assets_path in sorted(source_root.glob("*/*/assets.json")):
        page_folder = assets_path.parent
        metadata_path = page_folder / "metadata.json"
        metadata = read_json(metadata_path)
        assets = read_json(assets_path)
        changed = False

        for asset in assets:
            if not asset.get("exists") or not asset.get("resolved_path"):
                continue
            extension = str(asset.get("extension") or "").casefold()
            if asset.get("parse_status") == "native_source":
                skipped += 1
                continue
            if extension not in DOCLING_EXTENSIONS:
                asset["parse_status"] = "unsupported"
                unsupported += 1
                changed = True
                continue
            if (
                asset.get("parse_status") == "parsed"
                and asset.get("parsed_source_sha256") == asset.get("sha256")
                and asset.get("parser_output")
            ):
                output = asset["parser_output"]
                if all(Path(path).is_file() for path in output.values() if path):
                    skipped += 1
                    continue

            source = Path(asset["resolved_path"])
            target_dir = (
                output_root
                / safe_component(str(metadata["space"]))
                / safe_component(str(metadata["page_id"]))
                / f"{str(asset.get('sha256') or '')[:12]}-{safe_component(source.stem)}"
            )
            target_dir.mkdir(parents=True, exist_ok=True)
            args = [
                executable, "convert", str(source),
                "--to", "md", "--to", "json",
                "--output", str(target_dir),
                "--image-export-mode", "referenced",
                "--document-timeout", str(document_timeout),
                "--quiet",
            ]
            try:
                result = runner(
                    args,
                    env=sanitized_subprocess_env(),
                    capture_output=True,
                    text=True,
                    timeout=document_timeout + 60,
                    shell=False,
                )
            except subprocess.TimeoutExpired:
                result = None

            if result is None or result.returncode != 0:
                asset["parse_status"] = "failed"
                asset["parser"] = "docling"
                asset["parser_error"] = (
                    f"timeout>{document_timeout}s" if result is None
                    else ((result.stderr or "") + "\n" + (result.stdout or "")).strip()[-2000:]
                )
                failed += 1
                changed = True
                if fail_fast:
                    write_json(assets_path, assets)
                    raise ValueError(f"Docling 解析失败：{source.name}")
                continue

            markdown_files = sorted(target_dir.glob("*.md"))
            json_files = sorted(target_dir.glob("*.json"))
            if not markdown_files and not json_files:
                asset["parse_status"] = "failed"
                asset["parser"] = "docling"
                asset["parser_error"] = "Docling 成功退出但未生成 md/json"
                failed += 1
                changed = True
                continue

            asset["parse_status"] = "parsed"
            asset["parser"] = "docling"
            asset["parsed_source_sha256"] = asset.get("sha256")
            asset["parser_output"] = {
                "markdown": str(markdown_files[0]) if markdown_files else None,
                "json": str(json_files[0]) if json_files else None,
            }
            asset.pop("parser_error", None)
            parsed += 1
            changed = True

        if changed:
            write_json(assets_path, assets)
        complete_statuses = {"parsed", "native_source"}
        metadata["missing_asset_count"] = sum(not item.get("exists") for item in assets)
        metadata["pending_asset_count"] = sum(
            item.get("exists") and item.get("parse_status") not in complete_statuses for item in assets
        )
        metadata["source_complete"] = metadata["missing_asset_count"] == 0 and metadata["pending_asset_count"] == 0
        metadata["source_incomplete"] = not metadata["source_complete"]
        write_json(metadata_path, metadata)

    return {
        "parser": "docling",
        "parsed": parsed,
        "skipped": skipped,
        "failed": failed,
        "unsupported": unsupported,
        "output": str(output_root.resolve()),
    }
