"""隔离执行一条标准 unittest 用例，保留框架结果与聚合断言证据。"""
import json
import sys
import unittest
from pathlib import Path


def main():
    test_name, output_path = sys.argv[1:]
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName(test_name)
    if suite.countTestCases() != 1 or loader.errors:
        data = {"status": "BLOCKED", "reason": "用例必须解析到一个测试方法；加载失败或匹配数量不为一"}
    else:
        result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
        if result.errors:
            status, reason = "BLOCKED", "测试执行异常，需区分环境问题和脚本问题"
        elif result.failures or result.unexpectedSuccesses:
            status, reason = "FAIL", "断言失败或出现意外成功，需要检查需求与预期"
        elif result.skipped or result.expectedFailures:
            status, reason = "BLOCKED", "测试被跳过或标记为预期失败，不能作为验收通过"
        elif result.testsRun != 1:
            status, reason = "BLOCKED", "没有完成一条测试"
        else:
            status, reason = "PASS", "unittest 完成；仍须检查业务断言证据"
        data = {"status": status, "reason": reason, "tests_run": result.testsRun}
    Path(output_path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
