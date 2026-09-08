from __future__ import annotations

import hashlib
from pathlib import Path
import re
from urllib.parse import unquote, urlparse

from .io import json_hash, read_json, safe_component, text_hash, write_json


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".svg"}
DOCLING_EXTENSIONS = IMAGE_EXTENSIONS | {".pdf", ".docx", ".pptx", ".xlsx", ".xls", ".html", ".htm", ".csv"}
NATIVE_DIAGRAM_EXTENSIONS = {".drawio", ".puml", ".plantuml", ".mmd", ".mermaid"}
_LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _frontmatter(text: str):
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = next((idx for idx in range(1, len(lines)) if lines[idx].strip() == "---"), None)
    if end is None:
        return {}, text
    metadata = {}
    for line in lines[1:end]:
        if ":" not in line or line[:1].isspace():
            continue
        key, value = line.split(":", 1)
        value = value.strip()
        if (value.startswith("'") and value.endswith("'")) or (value.startswith('"') and value.endswith('"')):
            value = value[1:-1]
        metadata[key.strip()] = value
    return metadata, "\n".join(lines[end + 1:]).lstrip("\n")


def _title(body: str, fallback: str) -> str:
    for line in body.splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match:
            return match.group(1).strip()
    return fallback


def _destination(raw: str) -> str:
    value = raw.strip()
    if value.startswith("<") and ">" in value:
        value = value[1:value.index(">")]
    if ' "' in value:
        value = value.split(' "', 1)[0]
    return unquote(value.strip())


def _safe_resolve(base: Path, target: str, root: Path):
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or target.startswith("#"):
        return None
    clean = target.split("#", 1)[0].split("?", 1)[0]
    if not clean:
        return None
    resolved = (base / clean).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        return None
    return resolved


def _split_table_row(line: str):
    value = line.strip()
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|"):
        value = value[:-1]
    cells, current, escaped = [], [], False
    for char in value:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
            current.append(char)
        elif char == "|":
            cells.append("".join(current).strip().replace("\\|", "|"))
            current = []
        else:
            current.append(char)
    cells.append("".join(current).strip().replace("\\|", "|"))
    return cells


def _separator_row(line: str) -> bool:
    cells = _split_table_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells)


def extract_markdown_tables(body: str):
    lines = body.splitlines()
    tables, i, current_heading = [], 0, None
    while i < len(lines):
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", lines[i])
        if heading:
            current_heading = heading.group(2).strip()
        if i + 1 < len(lines) and "|" in lines[i] and _separator_row(lines[i + 1]):
            rows = [_split_table_row(lines[i])]
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_split_table_row(lines[i]))
                i += 1
            tables.append({
                "table_id": f"table-{len(tables) + 1:03d}",
                "heading": current_heading,
                "headers": rows[0],
                "rows": rows[1:],
            })
            continue
        i += 1
    return tables


def _collect_refs(md_path: Path, body: str, export_root: Path, page_ids_by_path: dict[Path, str]):
    images = []
    for match in _IMAGE_RE.finditer(body):
        target = _destination(match.group(2))
        resolved = _safe_resolve(md_path.parent, target, export_root)
        images.append({
            "kind": "image",
            "label": match.group(1),
            "target": target,
            "resolved_path": str(resolved) if resolved else None,
        })

    links = []
    for match in _LINK_RE.finditer(body):
        target = _destination(match.group(2))
        resolved = _safe_resolve(md_path.parent, target, export_root)
        target_page_id = page_ids_by_path.get(resolved) if resolved else None
        links.append({
            "kind": "page" if target_page_id else ("external" if urlparse(target).scheme else "file"),
            "label": match.group(1),
            "target": target,
            "resolved_path": str(resolved) if resolved else None,
            "target_page_id": target_page_id,
        })
    return images, links


def _asset_record(kind: str, label: str, target: str, resolved: Path | None, previous=None):
    previous = previous or {}
    exists = bool(resolved and resolved.is_file())
    extension = resolved.suffix.casefold() if exists else Path(target).suffix.casefold()
    sha = file_hash(resolved) if exists else None
    if extension in NATIVE_DIAGRAM_EXTENSIONS:
        default_status = "native_source"
    elif extension in DOCLING_EXTENSIONS:
        default_status = "pending"
    else:
        default_status = "unsupported"
    record = {
        "kind": kind,
        "label": label,
        "target": target,
        "resolved_path": str(resolved) if resolved else None,
        "exists": exists,
        "extension": extension,
        "sha256": sha,
        "parse_status": default_status,
        "needs_visual_review": extension in IMAGE_EXTENSIONS or extension == ".drawio",
    }
    if sha and previous.get("sha256") == sha:
        for key in ("parse_status", "parser", "parsed_source_sha256", "parser_output", "parser_error"):
            if key in previous:
                record[key] = previous[key]
    return record


def _parent_candidate(md_path: Path):
    parent_dir = md_path.parent
    if parent_dir.parent == parent_dir:
        return None
    return parent_dir.parent / f"{parent_dir.name}.md"


def import_cme_export(export_root, source_root, inventory_path):
    export_root = Path(export_root).resolve()
    source_root = Path(source_root)
    inventory_path = Path(inventory_path)
    if not export_root.is_dir():
        raise ValueError(f"CME 导出目录不存在：{export_root}")

    pages = []
    for md_path in sorted(export_root.rglob("*.md")):
        text = md_path.read_text(encoding="utf-8-sig")
        meta, body = _frontmatter(text)
        page_id = str(meta.get("confluence_page_id") or "").strip()
        if page_id:
            pages.append((md_path.resolve(), meta, body))
    if not pages:
        raise ValueError("CME 导出中没有找到带 confluence_page_id front matter 的页面")

    page_ids_by_path = {path: str(meta["confluence_page_id"]) for path, meta, _ in pages}
    referenced_assets = set()
    inventory_pages, needs_extraction = [], []

    for md_path, frontmatter, body in pages:
        page_id = str(frontmatter["confluence_page_id"])
        space = str(frontmatter.get("confluence_space_key") or "UNKNOWN")
        version_raw = str(frontmatter.get("confluence_version") or "1")
        version = int(version_raw) if version_raw.isdigit() else 1
        updated_at = str(frontmatter.get("confluence_last_modified") or "unknown")
        url = str(frontmatter.get("confluence_tinyui_url") or frontmatter.get("confluence_webui_url") or "")
        folder = source_root / safe_component(space) / safe_component(page_id)
        old_meta = read_json(folder / "metadata.json") if (folder / "metadata.json").is_file() else {}
        old_assets = read_json(folder / "assets.json") if (folder / "assets.json").is_file() else []
        old_by_path = {item.get("resolved_path"): item for item in old_assets if item.get("resolved_path")}

        images, links = _collect_refs(md_path, body, export_root, page_ids_by_path)
        assets = []
        for image in images:
            resolved = Path(image["resolved_path"]) if image["resolved_path"] else None
            asset = _asset_record("image", image["label"], image["target"], resolved, old_by_path.get(image["resolved_path"]))
            assets.append(asset)
            if resolved and resolved.is_file():
                referenced_assets.add(resolved.resolve())
        for link in links:
            if link["kind"] != "file":
                continue
            resolved = Path(link["resolved_path"]) if link["resolved_path"] else None
            asset = _asset_record("attachment", link["label"], link["target"], resolved, old_by_path.get(link["resolved_path"]))
            assets.append(asset)
            if resolved and resolved.is_file():
                referenced_assets.add(resolved.resolve())

        tables = extract_markdown_tables(body)
        source_fingerprint = json_hash({
            "content_hash": text_hash(body),
            "assets": [{"target": a["target"], "sha256": a["sha256"], "exists": a["exists"]} for a in assets],
            "version": version,
        })
        previous_fingerprint = old_meta.get("source_fingerprint")
        status = "NEW" if not previous_fingerprint else ("UNCHANGED" if previous_fingerprint == source_fingerprint else "UPDATED")
        if status != "UNCHANGED":
            needs_extraction.append(page_id)

        parent_path = _parent_candidate(md_path)
        parent_id = page_ids_by_path.get(parent_path.resolve()) if parent_path and parent_path.is_file() else None
        missing_count = sum(not item["exists"] for item in assets)
        complete_statuses = {"parsed", "native_source"}
        pending_count = sum(item["exists"] and item["parse_status"] not in complete_statuses for item in assets)
        visual_count = sum(bool(item["needs_visual_review"]) for item in assets)
        source_complete = missing_count == 0 and pending_count == 0

        metadata = {
            "schema_version": 1,
            "source_adapter": "confluence-markdown-exporter",
            "page_id": page_id,
            "title": _title(body, md_path.stem),
            "space": space,
            "parent_id": parent_id,
            "path": str(md_path.relative_to(export_root)),
            "version": version,
            "updated_at": updated_at,
            "url": url,
            "status": status,
            "raw_status": "active",
            "content_hash": text_hash(body),
            "source_fingerprint": source_fingerprint,
            "table_count": len(tables),
            "link_count": len(links),
            "asset_count": len(assets),
            "missing_asset_count": missing_count,
            "pending_asset_count": pending_count,
            "visual_asset_count": visual_count,
            "visual_review_required": visual_count > 0,
            "source_complete": source_complete,
            "source_incomplete": not source_complete,
            "original_markdown_path": str(md_path),
        }
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "content.md").write_text(body, encoding="utf-8")
        write_json(folder / "cme-frontmatter.json", frontmatter)
        write_json(folder / "tables.json", tables)
        write_json(folder / "links.json", links)
        write_json(folder / "assets.json", assets)
        write_json(folder / "metadata.json", metadata)
        inventory_pages.append(metadata)

    orphan_assets = []
    for attachments_dir in export_root.rglob("attachments"):
        if attachments_dir.is_dir():
            for path in attachments_dir.rglob("*"):
                if path.is_file() and path.resolve() not in referenced_assets:
                    orphan_assets.append({
                        "path": str(path.resolve()),
                        "relative_path": str(path.resolve().relative_to(export_root)),
                        "sha256": file_hash(path),
                    })
    write_json(source_root / "_orphan-assets.json", orphan_assets)

    counts = {}
    for item in inventory_pages:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    inventory = {
        "schema_version": 1,
        "adapter": "confluence-markdown-exporter",
        "export_root": str(export_root),
        "pages": inventory_pages,
        "counts": counts,
        "needs_extraction": needs_extraction,
        "orphan_asset_count": len(orphan_assets),
    }
    write_json(inventory_path, inventory)
    return inventory
