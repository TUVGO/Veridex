"""Veridex deterministic workflow: validate plans, run unittest cases, archive evidence, and build reports."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parent
STATUSES = ("PASS", "FAIL", "BLOCKED", "NOT_RUN")

from qa_core.data_policy import validate_reference_data
from qa_core.write_evidence import write_evidence_summary
from qa_core.write_policy import bulk_approval_ready, validate_write_policy


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def required_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} 必须是非空字符串")


def validate(plan):
    if not isinstance(plan, dict) or plan.get("schema_version") != 1:
        raise ValueError("schema_version 必须为 1")
    for key in ("project", "requirement_version", "system_version"):
        required_text(plan.get(key), key)
    if plan.get("environment") not in ("demo", "uat"):
        raise ValueError("第一版仅支持 demo / uat；生产不能作为功能验收环境")
    source = plan.get("source", {})
    required_text(source.get("path"), "source.path")
    if not re.fullmatch(r"[a-f0-9]{64}", source.get("sha256", "")):
        raise ValueError("source.sha256 必须是实际源文件的 SHA-256")
    if source.get("review_status") not in ("confirmed", "stale"):
        raise ValueError("source.review_status 必须为 confirmed 或 stale")
    requirements = plan.get("requirements")
    cases = plan.get("cases")
    if not isinstance(requirements, list) or not requirements:
        raise ValueError("requirements 不能为空")
    if not isinstance(cases, list) or not cases:
        raise ValueError("cases 不能为空")
    rules = {}
    for rule in requirements:
        for key in ("id", "statement", "source_ref"):
            required_text(rule.get(key), f"requirement.{key}")
        if rule["id"] in rules:
            raise ValueError("需求规则编号重复")
        if rule.get("rule_type") not in ("direct_mapping", "business_filter", "sample_selection", "presence", "data_constraint", "manual_review"):
            raise ValueError("规则类型不合法")
        if type(rule.get("confirmed")) is not bool:
            raise ValueError("confirmed 必须是布尔值")
        rules[rule["id"]] = rule
    ids, test_names = set(), set()
    for case in cases:
        for key in ("id", "title", "requirement_id", "jira_key", "priority", "module", "preconditions", "test_data", "expected"):
            required_text(case.get(key), f"case.{key}")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", case["id"]) or case["id"] in ids:
            raise ValueError("用例编号重复或包含不安全路径字符")
        ids.add(case["id"])
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*-[1-9][0-9]*", case["jira_key"]):
            raise ValueError("jira_key 必须且只能是一个 Jira 编号")
        if case["requirement_id"] not in rules:
            raise ValueError("requirement_id 未关联已有需求规则")
        refs = case.get("rule_ids")
        if not isinstance(refs, list) or not refs or any(r not in rules for r in refs):
            raise ValueError("每条用例必须关联已有规则 rule_ids")
        if case["requirement_id"] not in refs:
            raise ValueError("rule_ids 必须包含主 requirement_id")
        if not isinstance(case.get("steps"), list) or not case["steps"]:
            raise ValueError("steps 不能为空")
        for step in case["steps"]:
            required_text(step, "step")
        mode = case.get("mode")
        if mode not in ("automated", "manual", "blocked"):
            raise ValueError("mode 必须为 automated/manual/blocked")
        validate_reference_data(case.get("reference_data"))
        validate_write_policy(case, plan["environment"])
        if mode == "automated":
            name = case.get("test", "")
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*){2,}", name):
                raise ValueError("automated 用例必须指定 Python 点分测试方法名")
            if name in test_names:
                raise ValueError("同一测试方法不能重复映射多条用例")
            test_names.add(name)
        else:
            required_text(case.get("reason"), "未自动执行的原因")
    execution = plan.get("execution", {})
    required_text(execution.get("cwd"), "execution.cwd")
    timeout = execution.get("timeout_seconds")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
        raise ValueError("timeout_seconds 必须介于 0 和 3600 秒之间")
    return plan


def resolve(base, path):
    path = Path(path)
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


def evidence_files(folder):
    return {str(p.relative_to(folder)).replace("\\", "/"): digest(p)
            for p in sorted(folder.rglob("*")) if p.is_file()}


def run_plan(plan_path, runs_root=None):
    plan_path = Path(plan_path).resolve()
    plan = validate(read_json(plan_path))
    source = resolve(plan_path.parent, plan["source"]["path"])
    cwd = resolve(plan_path.parent, plan["execution"]["cwd"])
    if not source.is_file() or digest(source) != plan["source"]["sha256"]:
        raise ValueError("需求源文件不存在或已变更；请重新分析并更新版本及哈希")
    if not cwd.is_dir():
        raise ValueError("execution.cwd 不存在")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    folder = Path(runs_root or ROOT / "runs") / run_id
    folder.mkdir(parents=True, exist_ok=False)
    evidence = folder / "evidence"
    evidence.mkdir()
    # 只快照本次明确指定的需求文件，不读取或复制项目凭据配置。
    shutil.copyfile(source, evidence / ("requirement" + source.suffix))
    write_json(folder / "plan.json", plan)
    result = {"run_id": run_id, "started_at": now(), "finished_at": None,
              "state": "RUNNING", "plan_sha256": digest(folder / "plan.json"),
              "cases": [], "evidence_sha256": {}}
    rules = {r["id"]: r for r in plan["requirements"]}
    for case in plan["cases"]:
        write_policy = validate_write_policy(case, plan["environment"])
        record = {"case_id": case["id"], "status": "NOT_RUN", "reason": "尚未执行", "checks": [],
                  "started_at": now(), "finished_at": None, "evidence": None,
                  "write_scope": write_policy["scope"], "write_summary": None}
        if case["mode"] == "manual":
            record["reason"] = case["reason"]
        elif case["mode"] == "blocked":
            record.update(status="BLOCKED", reason=case["reason"])
        elif plan["source"]["review_status"] != "confirmed" or any(not rules[r]["confirmed"] for r in case["rule_ids"]):
            record.update(status="BLOCKED", reason="需求版本或断言来源未确认")
        elif write_policy["scope"] != "readonly" and os.environ.get("QA_UAT_WRITE_ENABLED") != "1":
            record.update(status="BLOCKED", reason="UAT 写入总开关未开启（QA_UAT_WRITE_ENABLED=1）")
        elif write_policy["scope"] == "bulk_write" and not bulk_approval_ready(write_policy):
            record.update(status="BLOCKED", reason="批量写入尚未获得本次运行审批或审批引用不匹配")
        else:
            case_dir = evidence / case["id"]
            case_dir.mkdir()
            checks_file = case_dir / "checks.jsonl"
            child_result = case_dir / "runner.json"
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT) + os.pathsep + str(cwd) + os.pathsep + env.get("PYTHONPATH", "")
            env["QA_CHECKS_PATH"] = str(checks_file.resolve())
            env["QA_ENVIRONMENT"] = plan["environment"]
            env["QA_RUN_ID"] = run_id
            env["QA_CASE_ID"] = case["id"]
            env["QA_WRITE_SCOPE"] = write_policy["scope"]
            env["QA_ESTIMATED_WRITE_ROWS"] = str(write_policy["estimated_rows"])
            if write_policy["scope"] != "readonly":
                env["QA_TEST_DATA_REGISTRY"] = str((case_dir / "test-data-registry.json").resolve())
                env["QA_UAT_WRITE_AUDIT_PATH"] = str((case_dir / "write-audit.jsonl").resolve())
                approval = write_policy.get("bulk_approval")
                if approval:
                    env["QA_UAT_BULK_APPROVAL_REF_EXPECTED"] = approval["approval_ref"]
                    env["QA_UAT_BULK_APPROVED_MAX_ROWS"] = str(approval["approved_max_rows"])
            command = [sys.executable, str(ROOT / "execute_case.py"), case["test"], str(child_result.resolve())]
            write_json(case_dir / "invocation.json", {"test": case["test"], "cwd": str(cwd), "environment": plan["environment"]})
            # 快照工作目录内的 Python 测试代码，便于复核断言；不复制其他配置或数据文件。
            module_path = cwd.joinpath(*case["test"].split(".")[:-2]).with_suffix(".py")
            if module_path.is_file():
                shutil.copyfile(module_path, case_dir / "test_source.py")
            try:
                process = subprocess.run(command, cwd=cwd, env=env, capture_output=True,
                                         timeout=plan["execution"]["timeout_seconds"])
                (case_dir / "stdout.log").write_bytes(process.stdout)
                (case_dir / "stderr.log").write_bytes(process.stderr)
                if process.returncode != 0 or not child_result.is_file():
                    record.update(status="BLOCKED", reason=f"执行器异常退出 ({process.returncode})")
                else:
                    child = read_json(child_result)
                    record.update(status=child["status"], reason=child["reason"])
                    checks = [json.loads(line) for line in checks_file.read_text(encoding="utf-8").splitlines()] if checks_file.exists() else []
                    record["checks"] = checks
                    if record["status"] == "PASS" and (not checks or any(c.get("matched") is not True for c in checks)):
                        record.update(status="BLOCKED", reason="框架成功但没有完整的通过断言证据")
            except subprocess.TimeoutExpired as exc:
                (case_dir / "stdout.log").write_bytes(exc.stdout or b"")
                (case_dir / "stderr.log").write_bytes(exc.stderr or b"")
                record.update(status="BLOCKED", reason="测试超时；未自动重试，需检查副作用后另开批次")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                record.update(status="BLOCKED", reason=f"证据或执行结果无效：{type(exc).__name__}")
            if write_policy["scope"] != "readonly":
                summary = write_evidence_summary(case_dir, run_id, case["id"])
                record["write_summary"] = summary
                if not summary["valid"]:
                    if record["status"] == "PASS":
                        record.update(status="BLOCKED", reason=summary["reason"])
                    elif summary["reason"] not in record["reason"]:
                        record["reason"] = record["reason"] + "；" + summary["reason"]
            record["evidence"] = f"evidence/{case['id']}"
        record["finished_at"] = now()
        result["cases"].append(record)
        result["evidence_sha256"] = evidence_files(evidence)
        write_json(folder / "results.json", result)
    result.update(state="COMPLETE", finished_at=now())
    write_json(folder / "results.json", result)
    build_report(folder)
    return folder


def cell(value):
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", "<br>")


def build_report(folder):
    folder = Path(folder).resolve()
    plan = validate(read_json(folder / "plan.json"))
    result = read_json(folder / "results.json")
    if result["plan_sha256"] != digest(folder / "plan.json"):
        raise ValueError("计划快照被修改，拒绝生成报告")
    if evidence_files(folder / "evidence") != result["evidence_sha256"]:
        raise ValueError("执行证据缺失或被修改，拒绝生成报告")
    records = {}
    planned_ids = {c["id"] for c in plan["cases"]}
    for record in result["cases"]:
        if record["case_id"] not in planned_ids or record["case_id"] in records or record["status"] not in STATUSES:
            raise ValueError("执行记录包含未知、重复用例或非法状态")
        if record["status"] in ("PASS", "FAIL"):
            expected_evidence = f"evidence/{record['case_id']}"
            if record.get("evidence") != expected_evidence:
                raise ValueError("已执行用例缺少绑定的证据目录")
            child = read_json(folder / expected_evidence / "runner.json")
            if child["status"] != record["status"]:
                raise ValueError("报告状态与原始执行器结果不一致")
            checks_path = folder / expected_evidence / "checks.jsonl"
            checks = [json.loads(line) for line in checks_path.read_text(encoding="utf-8").splitlines()] if checks_path.exists() else []
            if checks != record["checks"] or (record["status"] == "PASS" and (not checks or any(c.get("matched") is not True for c in checks))):
                raise ValueError("断言证据与结果不一致或缺失")
        records[record["case_id"]] = record
    for case in plan["cases"]:
        records.setdefault(case["id"], {"case_id": case["id"], "status": "NOT_RUN", "reason": "批次尚未执行到此用例", "checks": [], "evidence": None})
    counts = Counter(r["status"] for r in records.values())
    total = len(plan["cases"])
    executed = counts["PASS"] + counts["FAIL"]
    rate = f"{counts['PASS'] / executed:.1%}" if executed else "不适用（无完成判定的用例）"
    fully_passed = result["state"] == "COMPLETE" and counts["PASS"] == total
    prefix = "演示数据；不代表 VW-V6 UAT 验收。" if plan["environment"] == "demo" else "UAT 结果仅覆盖本次计划，不代表全部需求验收。"
    lines = ["# VW-V6 测试报告", "", prefix, "", f"- 批次：{result['run_id']}",
             f"- 项目：{plan['project']}；环境：{plan['environment']}",
             f"- 需求版本：{plan['requirement_version']}；系统版本：{plan['system_version']}",
             f"- 需求复核状态：{plan['source']['review_status']}；SHA-256：{plan['source']['sha256']}",
             f"- 开始：{result['started_at']}；结束：{result['finished_at'] or '未结束'}；批次状态：{result['state']}",
             "", "## 结果汇总", "", "| 总数 | PASS | FAIL | BLOCKED | NOT_RUN |", "|---:|---:|---:|---:|---:|",
             f"| {total} | {counts['PASS']} | {counts['FAIL']} | {counts['BLOCKED']} | {counts['NOT_RUN']} |", "",
             f"执行完成率：(PASS+FAIL)/总数 = {executed}/{total} = {executed/total:.1%}。",
             f"通过率：PASS/(PASS+FAIL) = {rate}。BLOCKED 和 NOT_RUN 不算通过。", "",
             "## 逐条结果", "", "| 用例 | 标题 | 状态 | 原因 | 证据 |", "|---|---|---|---|---|"]
    details = ["# 测试用例", "", "结构化主记录为 plan.json；本文件不是 SynapseRT Excel 导入文件。", ""]
    defects = ["# 缺陷草稿", "", "仅记录本批次 FAIL，未上传 Jira。执行异常列入报告阻塞项。", ""]
    for case in plan["cases"]:
        record = records[case["id"]]
        write_policy = validate_write_policy(case, plan["environment"])
        link = f"[证据]({record['evidence']}/runner.json)" if record["evidence"] and (folder / record["evidence"] / "runner.json").exists() else "—"
        lines.append(f"| {case['id']} | {cell(case['title'])} | {record['status']} | {cell(record['reason'])} | {link} |")
        details.extend([f"## {case['id']} {case['title']}", "", f"- 需求：{case['jira_key']}；规则：{', '.join(case['rule_ids'])}",
                        f"- 模块：{case['module']}；优先级：{case['priority']}", f"- 前置条件：{case['preconditions']}",
                        f"- 测试数据：{case['test_data']}", f"- 步骤：{'；'.join(case['steps'])}", f"- 预期：{case['expected']}",
                        f"- Reference Data：{', '.join(item['alias'] for item in validate_reference_data(case.get('reference_data'))) or '无'}",
                        f"- UAT 写范围：{write_policy['scope']}；计划单次最大影响行数：{write_policy['estimated_rows']}",
                        f"- 写入审计：{json.dumps(record.get('write_summary'), ensure_ascii=False) if record.get('write_summary') else '不适用'}", ""])
        if record["status"] == "FAIL":
            defects.extend([f"## {case['id']} {case['title']}", "", f"- 环境：{plan['environment']}；版本：{plan['system_version']}",
                            f"- 关联需求：{case['jira_key']}；规则：{', '.join(case['rule_ids'])}", f"- 前置条件：{case['preconditions']}",
                            f"- 数据：{case['test_data']}", f"- 复现：{'；'.join(case['steps'])}", f"- 预期：{case['expected']}",
                            f"- 实际聚合断言：{json.dumps(record['checks'], ensure_ascii=False)}", f"- 证据：{link}",
                            "- 严重程度与归因：待复核；不自动提交。", ""])
    write_cases = [c for c in plan["cases"] if validate_write_policy(c, plan["environment"])["scope"] != "readonly"]
    if write_cases:
        lines.extend(["", "## UAT 写入安全", "",
                      "原始/既有数据不得 UPDATE/DELETE；写用例只能通过 QA-owned Test Data Registry 管理自身创建的数据。批量写入需要计划审批和运行时审批引用双重匹配。", "",
                      "| 用例 | 写范围 | 预计单次最大行数 | 清理结果 |", "|---|---|---:|---|"])
        for case in write_cases:
            policy = validate_write_policy(case, plan["environment"])
            summary = records[case["id"]].get("write_summary")
            cleanup = summary.get("reason") if summary else records[case["id"]]["reason"]
            lines.append(f"| {case['id']} | {policy['scope']} | {policy['estimated_rows']} | {cell(cleanup)} |")
    lines.extend(["", "## 需求覆盖", "", "覆盖按规则关联计算，不证明需求提取无遗漏。sample_selection 仅为样本范围，不构成业务准入断言。", "",
                  "| 规则 | 类型 | 复核 | 用例 | 状态 |", "|---|---|---|---|---|"])
    uncovered = []
    for rule in plan["requirements"]:
        mapped = [c for c in plan["cases"] if rule["id"] in c["rule_ids"]]
        if not mapped:
            uncovered.append(rule["id"])
        lines.append(f"| {cell(rule['id'])} | {rule['rule_type']} | {rule['confirmed']} | {', '.join(c['id'] for c in mapped) or '未覆盖'} | {', '.join(records[c['id']]['status'] for c in mapped) or 'NOT_RUN'} |")
    conclusion = "本次计划用例全部通过。" if fully_passed and not uncovered and plan['source']['review_status'] == 'confirmed' and all(r['confirmed'] for r in plan['requirements']) else "尚不具备完整通过结论，需处理失败、阻塞、未执行或需求覆盖/复核问题。"
    lines.extend(["", "## 测试结论与限制", "", conclusion, "", prefix,
                  "", "证据哈希用于发现意外变更，不是防篡改签名；原始测试代码与断言仍需业务复核。"])
    for note in plan.get("notes", []):
        lines.append(f"- {note}")
    for name, content in (("report.md", lines), ("cases.md", details), ("defects.md", defects)):
        (folder / name).write_text("\n".join(content) + "\n", encoding="utf-8")
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "run"):
        p = sub.add_parser(name)
        p.add_argument("--plan", required=True)
    p = sub.add_parser("report")
    p.add_argument("--run", required=True)
    sub.add_parser("demo")
    args = parser.parse_args()
    try:
        if args.command == "validate":
            validate(read_json(args.plan))
            print("计划格式校验通过；不代表需求或系统验证通过")
            return 0
        if args.command == "report":
            counts = build_report(args.run)
            print(json.dumps(dict(counts), ensure_ascii=False))
            return 0
        plan_path = ROOT / "examples" / "demo.plan.json" if args.command == "demo" else Path(args.plan)
        folder = run_plan(plan_path)
        print(str(folder / "report.md"))
        statuses = [c["status"] for c in read_json(folder / "results.json")["cases"]]
        if "FAIL" in statuses:
            return 1
        return 2 if any(s != "PASS" for s in statuses) else 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"工作流未完成：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
