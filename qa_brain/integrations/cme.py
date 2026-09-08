from __future__ import annotations

from pathlib import Path
import subprocess
from urllib.parse import urlparse

from .common import resolve_executable, sanitized_subprocess_env


CME_PINNED_VERSION = "5.4.0"


def run_cme_export(
    page_urls,
    output_path,
    *,
    command="pages-with-descendants",
    cme_executable="cme",
    config_path=None,
    attachments="all",
    timeout=3600,
    allow_http=False,
    allow_insecure_auth=False,
    runner=subprocess.run,
):
    """Run Confluence Markdown Exporter without putting credentials on the command line."""
    if command not in {"pages", "pages-with-descendants"}:
        raise ValueError("CME command 只能是 pages 或 pages-with-descendants")
    if attachments not in {"referenced", "all", "disabled"}:
        raise ValueError("attachments 必须是 referenced/all/disabled")
    if not page_urls:
        raise ValueError("至少提供一个 Confluence 页面 URL")

    normalized_urls = []
    for raw in page_urls:
        parsed = urlparse(str(raw))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Confluence 页面 URL 必须是绝对 HTTP(S) 地址")
        if parsed.username or parsed.password:
            raise ValueError("Confluence URL 不得包含凭据")
        if parsed.scheme == "http" and not allow_http:
            raise ValueError("HTTP Confluence 必须显式 --allow-http")
        if parsed.scheme == "http" and not allow_insecure_auth:
            raise ValueError("CME 会携带认证信息；HTTP 必须额外显式 --allow-insecure-auth")
        normalized_urls.append(str(raw))

    executable = resolve_executable(cme_executable)
    output = Path(output_path).resolve()
    output.mkdir(parents=True, exist_ok=True)

    env = sanitized_subprocess_env({
        "CME_EXPORT__OUTPUT_PATH": str(output),
        "CME_EXPORT__ATTACHMENTS_EXPORT": attachments,
        "CME_EXPORT__PAGE_HREF": "relative",
        "CME_EXPORT__ATTACHMENT_HREF": "relative",
        "CME_EXPORT__PAGE_METADATA_IN_FRONTMATTER": "true",
        "CME_EXPORT__CONFLUENCE_URL_IN_FRONTMATTER": "both",
        "CME_EXPORT__PAGE_PROPERTIES_FORMAT": "frontmatter_and_table",
        "CME_EXPORT__IMAGE_CAPTIONS": "true",
    })
    if config_path:
        env["CME_CONFIG_PATH"] = str(Path(config_path).resolve())

    args = [executable, command, *normalized_urls]
    try:
        result = runner(
            args,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"CME 导出超时（{timeout}s）") from exc

    if result.returncode != 0:
        detail = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()[-4000:]
        raise ValueError(f"CME 导出失败，exit={result.returncode}: {detail}")

    return {
        "tool": "confluence-markdown-exporter",
        "command": command,
        "pages_requested": len(normalized_urls),
        "output": str(output),
        "stdout_tail": (result.stdout or "")[-4000:],
    }
