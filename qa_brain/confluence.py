from __future__ import annotations

import base64
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from .io import json_hash, read_json, required_text, safe_component, text_hash, write_json


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _StorageTextParser(HTMLParser):
    BLOCKS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCKS:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")

    def handle_endtag(self, tag):
        if tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)

    def text(self):
        value = "".join(self.parts)
        value = re.sub(r"[ \t]+", " ", value)
        value = re.sub(r"\n\s*\n+", "\n", value)
        return value.strip()


def storage_to_text(storage_html: str) -> str:
    parser = _StorageTextParser()
    parser.feed(storage_html or "")
    parser.close()
    return parser.text()


def attachment_manifest(items):
    fields = (
        "filename", "content_type", "version", "size", "attachment_id",
        "processed", "processing_status", "download_path",
    )
    return [{key: item.get(key) for key in fields} for item in items]


def attachment_fingerprint(items) -> str:
    stable = []
    for item in attachment_manifest(items):
        stable.append({
            "filename": item.get("filename"),
            "content_type": item.get("content_type"),
            "version": item.get("version"),
            "size": item.get("size"),
            "attachment_id": item.get("attachment_id"),
        })
    stable.sort(key=lambda x: (str(x.get("attachment_id")), str(x.get("filename"))))
    return json_hash(stable)


def page_fingerprint(page):
    body = page.get("body", "")
    storage_html = page.get("storage_html", "")
    return {
        "content_hash": text_hash(body),
        "storage_hash": text_hash(storage_html) if storage_html else None,
        "attachment_hash": attachment_fingerprint(page.get("attachments", [])),
        "raw_status": page.get("status", "active"),
    }


def source_incomplete(page):
    body = page.get("body", "")
    attachments = page.get("attachments", [])
    unread = any(not item.get("processed", False) for item in attachments)
    return not body.strip() or ("详见附件" in body and unread)


def load_export(path):
    payload = read_json(path)
    if isinstance(payload, list):
        pages = payload
    elif isinstance(payload, dict) and isinstance(payload.get("pages"), list):
        pages = payload["pages"]
    elif isinstance(payload, dict) and isinstance(payload.get("batches"), list):
        pages = []
        for batch in payload["batches"]:
            if not isinstance(batch, dict) or not isinstance(batch.get("pages"), list):
                raise ValueError("每个分页批次必须包含 pages 数组")
            pages.extend(batch["pages"])
    else:
        raise ValueError("Confluence 导出必须是页面数组、pages 或 batches")

    seen = set()
    for page in pages:
        if not isinstance(page, dict):
            raise ValueError("页面必须是对象")
        for key in ("page_id", "title", "space", "updated_at", "url"):
            required_text(page.get(key), f"page.{key}")
        if page["page_id"] in seen:
            raise ValueError(f"页面编号重复：{page['page_id']}")
        seen.add(page["page_id"])
        if type(page.get("version")) is not int or page["version"] < 1:
            raise ValueError("page.version 必须是正整数")
        if not isinstance(page.get("body", ""), str):
            raise ValueError("page.body 必须是字符串")
        if not isinstance(page.get("storage_html", ""), str):
            raise ValueError("page.storage_html 必须是字符串")
        if not isinstance(page.get("attachments", []), list):
            raise ValueError("page.attachments 必须是数组")
        if not isinstance(page.get("knowledge", []), list):
            raise ValueError("page.knowledge 必须是数组")
        if page.get("status", "active") not in ("active", "archived"):
            raise ValueError("page.status 只能是 active 或 archived")
        if page.get("parent_id") is not None and not isinstance(page["parent_id"], str):
            raise ValueError("page.parent_id 必须是字符串或 null")
        for attachment in page.get("attachments", []):
            if not isinstance(attachment, dict):
                raise ValueError("附件必须是对象")
            for key in ("filename", "content_type", "attachment_id", "processing_status"):
                required_text(attachment.get(key), f"attachment.{key}")
            if type(attachment.get("version")) is not int or attachment["version"] < 1:
                raise ValueError("attachment.version 必须是正整数")
            if type(attachment.get("size")) is not int or attachment["size"] < 0:
                raise ValueError("attachment.size 必须是非负整数")
            if type(attachment.get("processed")) is not bool:
                raise ValueError("attachment.processed 必须是布尔值")
    return pages


def _synced_baseline(previous):
    if not previous:
        return None
    keys = ("version", "updated_at", "content_hash", "storage_hash", "attachment_hash", "raw_status")
    if all(f"synced_{key}" in previous for key in keys):
        return {key: previous.get(f"synced_{key}") for key in keys}
    return None


def _current_state(page):
    fp = page_fingerprint(page)
    return {
        "version": page["version"],
        "updated_at": page["updated_at"],
        **fp,
    }


def inventory_item(page, status, previous=None):
    attachments = page.get("attachments", [])
    fp = page_fingerprint(page)
    item = {
        "page_id": page["page_id"],
        "title": page["title"],
        "space": page["space"],
        "parent_id": page.get("parent_id"),
        "path": page.get("path", page["title"]),
        "version": page["version"],
        "updated_at": page["updated_at"],
        "url": page["url"],
        "status": status,
        "raw_status": page.get("status", "active"),
        "attachment_count": len(attachments),
        **fp,
        "processing_status": "pending" if status in ("NEW", "UPDATED") else status.lower(),
        "source_incomplete": source_incomplete(page),
    }
    baseline = _synced_baseline(previous)
    if baseline:
        for key, value in baseline.items():
            item[f"synced_{key}"] = value
    return item


def _classify(page, previous):
    if page.get("status", "active") == "archived":
        return "ARCHIVED"
    baseline = _synced_baseline(previous)
    if baseline is None:
        return "NEW"
    return "UNCHANGED" if baseline == _current_state(page) else "UPDATED"


def scan(export_path, inventory_path):
    pages = load_export(export_path)
    inventory_path = Path(inventory_path)
    old_items = read_json(inventory_path).get("pages", []) if inventory_path.is_file() else []
    old = {item["page_id"]: item for item in old_items}
    current_ids = set()
    items = []
    for page in pages:
        page_id = page["page_id"]
        current_ids.add(page_id)
        previous = old.get(page_id)
        items.append(inventory_item(page, _classify(page, previous), previous))
    for page_id, item in old.items():
        if page_id not in current_ids:
            archived = dict(item)
            archived.update(status="ARCHIVED", raw_status="archived", processing_status="archived")
            items.append(archived)
    items.sort(key=lambda item: (item["space"], item["path"], item["page_id"]))
    result = {"schema_version": 2, "pages": items}
    write_json(inventory_path, result)
    return result


def _mark_synced(item):
    for key in ("version", "updated_at", "content_hash", "storage_hash", "attachment_hash", "raw_status"):
        item[f"synced_{key}"] = item.get(key)
    item["processing_status"] = "archived" if item.get("raw_status") == "archived" else "synced"


def sync(export_path, inventory_path, source_root):
    pages = load_export(export_path)
    inventory = scan(export_path, inventory_path)
    states = {item["page_id"]: item for item in inventory["pages"]}
    source_root = Path(source_root)
    written = 0
    for page in pages:
        item = states[page["page_id"]]
        folder = source_root / safe_component(page["space"]) / safe_component(page["page_id"])
        metadata_path = folder / "metadata.json"
        must_write = item["status"] in ("NEW", "UPDATED") or not metadata_path.is_file()
        if must_write:
            folder.mkdir(parents=True, exist_ok=True)
            metadata = {k: v for k, v in item.items() if not k.startswith("synced_")}
            write_json(metadata_path, metadata)
            (folder / "content.md").write_text(page.get("body", ""), encoding="utf-8")
            if page.get("storage_html"):
                (folder / "storage.html").write_text(page["storage_html"], encoding="utf-8")
            elif (folder / "storage.html").exists():
                (folder / "storage.html").unlink()
            write_json(folder / "attachments.json", attachment_manifest(page.get("attachments", [])))
            if page.get("attachments"):
                (folder / "attachments").mkdir(exist_ok=True)
            written += 1
        _mark_synced(item)

    for item in inventory["pages"]:
        if item["status"] != "ARCHIVED":
            continue
        metadata_path = source_root / safe_component(item["space"]) / safe_component(item["page_id"]) / "metadata.json"
        if metadata_path.is_file():
            metadata = read_json(metadata_path)
            metadata.update(status="ARCHIVED", raw_status="archived")
            write_json(metadata_path, metadata)
        _mark_synced(item)

    write_json(inventory_path, inventory)
    return {
        "inventory": inventory,
        "sources_written": written,
        "needs_extraction": [item["page_id"] for item in inventory["pages"] if item["status"] in ("NEW", "UPDATED")],
    }


class ConfluenceClient:
    """Minimal read-only Confluence Server/Data Center REST client."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        user: str | None = None,
        password: str | None = None,
        cookie: str | None = None,
        timeout: int = 20,
        allow_http: bool = False,
        allow_insecure_auth: bool = False,
    ):
        parsed = urlparse(required_text(base_url, "base_url"))
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("base_url 必须是绝对 HTTP(S) 地址")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url 不得包含凭据、查询参数或 fragment")
        if parsed.scheme == "http" and not allow_http:
            raise ValueError("HTTP Confluence 必须显式 --allow-http")
        has_auth = bool(token or cookie or user or password)
        if parsed.scheme == "http" and has_auth and not allow_insecure_auth:
            raise ValueError("HTTP 上传递凭据必须显式 --allow-insecure-auth")
        if bool(user) != bool(password):
            raise ValueError("Basic Auth 必须同时提供 user/password")
        self.base_url = base_url.rstrip("/") + "/"
        self.host = parsed.netloc
        self.timeout = timeout
        self.headers = {"Accept": "application/json", "User-Agent": "qa-brain/phase1a"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        elif user and password:
            raw = f"{user}:{password}".encode("utf-8")
            self.headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        if cookie:
            self.headers["Cookie"] = cookie
        context = ssl.create_default_context()
        self.opener = build_opener(_NoRedirect(), HTTPSHandler(context=context))

    @classmethod
    def from_env(cls, base_url: str, **kwargs):
        return cls(
            base_url,
            token=os.getenv("QA_CONFLUENCE_TOKEN"),
            user=os.getenv("QA_CONFLUENCE_USER"),
            password=os.getenv("QA_CONFLUENCE_PASSWORD"),
            cookie=os.getenv("QA_CONFLUENCE_COOKIE"),
            **kwargs,
        )

    def get_json(self, path: str, params=None):
        url = urljoin(self.base_url, path.lstrip("/"))
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params)
        parsed = urlparse(url)
        if parsed.netloc != self.host:
            raise ValueError("Confluence 请求不得跨主机")
        req = Request(url, headers=self.headers, method="GET")
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise ValueError("Confluence 响应超过 8 MiB 限制")
                return json.loads(raw.decode("utf-8"))
        except HTTPError as exc:
            raise ValueError(f"Confluence HTTP {exc.code}") from exc
        except URLError as exc:
            raise ValueError("Confluence 连接失败") from exc

    def _paged(self, path: str, params=None):
        start = 0
        params = dict(params or {})
        while True:
            request_params = {**params, "start": start, "limit": 100}
            payload = self.get_json(path, request_params)
            results = payload.get("results", [])
            if not isinstance(results, list):
                raise ValueError("Confluence 分页响应缺少 results")
            for item in results:
                yield item
            size = payload.get("size", len(results))
            if not results or not payload.get("_links", {}).get("next"):
                break
            start += int(size or len(results) or 100)

    def page(self, page_id: str):
        return self.get_json(
            f"rest/api/content/{page_id}",
            {"expand": "body.storage,version,space,ancestors"},
        )

    def child_pages(self, page_id: str):
        return list(self._paged(
            f"rest/api/content/{page_id}/child/page",
            {"expand": "version,space,ancestors"},
        ))

    def attachments(self, page_id: str):
        return list(self._paged(
            f"rest/api/content/{page_id}/child/attachment",
            {"expand": "version"},
        ))


def _attachment_from_rest(item):
    extensions = item.get("extensions") or {}
    version = item.get("version") or {}
    links = item.get("_links") or {}
    return {
        "filename": str(item.get("title") or item.get("id") or "attachment"),
        "content_type": str(extensions.get("mediaType") or "application/octet-stream"),
        "version": int(version.get("number") or 1),
        "size": int(extensions.get("fileSize") or 0),
        "attachment_id": str(item.get("id") or ""),
        "processed": False,
        "processing_status": "pending",
        "download_path": links.get("download"),
    }


def _page_from_rest(client: ConfluenceClient, payload, parent_id=None, path_prefix=None):
    page_id = str(payload["id"])
    full = client.page(page_id)
    storage_html = (((full.get("body") or {}).get("storage") or {}).get("value") or "")
    version = full.get("version") or {}
    space = full.get("space") or {}
    title = required_text(full.get("title"), "Confluence title")
    page_path = f"{path_prefix}/{title}" if path_prefix else title
    return {
        "page_id": page_id,
        "title": title,
        "space": required_text(space.get("key"), "Confluence space.key"),
        "parent_id": parent_id,
        "path": page_path,
        "version": int(version.get("number") or 1),
        "updated_at": str(version.get("when") or "unknown"),
        "url": urljoin(client.base_url, str((full.get("_links") or {}).get("webui") or f"pages/viewpage.action?pageId={page_id}")),
        "status": "archived" if full.get("status") == "archived" else "active",
        "body": storage_to_text(storage_html),
        "storage_html": storage_html,
        "attachments": [_attachment_from_rest(item) for item in client.attachments(page_id)],
        "knowledge": [],
    }


def collect_tree(client: ConfluenceClient, root_page_ids):
    pages = []
    seen = set()

    def visit(page_id, parent_id=None, path_prefix=None):
        page_id = str(page_id)
        if page_id in seen:
            return
        seen.add(page_id)
        current = _page_from_rest(client, {"id": page_id}, parent_id, path_prefix)
        pages.append(current)
        for child in client.child_pages(page_id):
            visit(str(child["id"]), page_id, current["path"])

    for root in root_page_ids:
        visit(str(root))
    return pages


def collect_to_export(client: ConfluenceClient, root_page_ids, output_path):
    pages = collect_tree(client, root_page_ids)
    write_json(output_path, {"schema_version": 1, "pages": pages})
    return {"pages": len(pages), "output": str(Path(output_path).resolve())}
