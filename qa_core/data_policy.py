from __future__ import annotations


DATA_CLASSES = ("reference", "owned", "derived")


def validate_reference_data(value):
    """Validate readonly aliases to existing environment data.

    The plan intentionally stores aliases, not real VIN/account/customer values.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("reference_data 必须是数组")
    result, aliases = [], set()
    for index, item in enumerate(value, 1):
        if not isinstance(item, dict):
            raise ValueError(f"reference_data[{index}] 必须是对象")
        alias = item.get("alias")
        entity = item.get("entity")
        access = item.get("access", "readonly")
        purpose = item.get("purpose", "")
        if not isinstance(alias, str) or not alias.strip():
            raise ValueError(f"reference_data[{index}].alias 必须是非空字符串")
        if alias in aliases:
            raise ValueError("reference_data.alias 不能重复")
        aliases.add(alias)
        if not isinstance(entity, str) or not entity.strip():
            raise ValueError(f"reference_data[{index}].entity 必须是非空字符串")
        if access != "readonly":
            raise ValueError("Existing/reference data 只能声明 access=readonly")
        if purpose is not None and not isinstance(purpose, str):
            raise ValueError("reference_data.purpose 必须是字符串")
        extra = set(item) - {"alias", "entity", "access", "purpose"}
        if extra:
            raise ValueError("reference_data 只允许 alias/entity/access/purpose；真实标识不得写入 plan")
        result.append({
            "alias": alias.strip(),
            "entity": entity.strip(),
            "access": "readonly",
            "purpose": purpose or "",
        })
    return result
