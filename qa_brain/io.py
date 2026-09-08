from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def json_hash(value) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return text_hash(payload)


def normalized(value) -> str:
    return re.sub(r"[\s_-]+", "", str(value)).casefold()


def safe_component(value) -> str:
    value = re.sub(r"[^\w.-]+", "_", str(value), flags=re.UNICODE).strip("._")
    if not value:
        raise ValueError("页面目录标识不能为空")
    return value


def required_text(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} 必须是非空字符串")
    return value.strip()
