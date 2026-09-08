"""测试脚本使用的聚合断言证据接口；不得传入密码或未脱敏个人数据。"""
import json
import os
from pathlib import Path


def _record(label, actual, expected, operator, matched):
    path = os.environ.get("QA_CHECKS_PATH")
    if not path:
        raise RuntimeError("请通过 workflow.py run 执行，以绑定证据批次")
    item = dict(label=label, actual=actual, expected=expected,
                operator=operator, matched=bool(matched))
    with Path(path).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n")


def equal(test, label, actual, expected):
    """先记录实际值与独立预期，再使用 unittest 断言。"""
    _record(label, actual, expected, "==", actual == expected)
    test.assertEqual(actual, expected, label)


def greater(test, label, actual, expected):
    """记录数值下界断言。"""
    _record(label, actual, expected, ">", actual > expected)
    test.assertGreater(actual, expected, label)
