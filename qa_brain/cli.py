from __future__ import annotations

import argparse
from dataclasses import asdict
import json

from .confluence import ConfluenceClient, collect_to_export, scan, sync
from .integrations import parse_assets_with_docling, run_cme_export, tool_version
from .knowledge import accept_extraction_result, build_knowledge, import_extractions, run_demo
from .source_package import import_cme_export
from .tasks import create_rich_extraction_tasks


def build_parser():
    parser = argparse.ArgumentParser(description="QA Brain Phase 1A")
    commands = parser.add_subparsers(dest="area", required=True)

    confluence = commands.add_parser("confluence").add_subparsers(dest="command", required=True)
    scan_cmd = confluence.add_parser("scan")
    scan_cmd.add_argument("--export", required=True)
    scan_cmd.add_argument("--inventory", default="data/confluence/inventory.json")

    sync_cmd = confluence.add_parser("sync")
    sync_cmd.add_argument("--export", required=True)
    sync_cmd.add_argument("--inventory", default="data/confluence/inventory.json")
    sync_cmd.add_argument("--sources", default="sources/confluence")

    collect = confluence.add_parser("collect")
    collect.add_argument("--base-url", required=True)
    collect.add_argument("--root-page", action="append", required=True)
    collect.add_argument("--export", default="work/confluence-export.json")
    collect.add_argument("--timeout", type=int, default=20)
    collect.add_argument("--allow-http", action="store_true")
    collect.add_argument("--allow-insecure-auth", action="store_true")

    cme_export = confluence.add_parser("cme-export")
    cme_export.add_argument("--page-url", action="append", required=True)
    cme_export.add_argument("--output", default="sources/cme-export")
    cme_export.add_argument("--cme-exe", default="cme")
    cme_export.add_argument("--config-path")
    cme_export.add_argument("--command", choices=("pages", "pages-with-descendants"), default="pages-with-descendants")
    cme_export.add_argument("--attachments", choices=("referenced", "all", "disabled"), default="all")
    cme_export.add_argument("--timeout", type=int, default=3600)
    cme_export.add_argument("--allow-http", action="store_true")
    cme_export.add_argument("--allow-insecure-auth", action="store_true")

    cme_import = confluence.add_parser("cme-import")
    cme_import.add_argument("--export-root", default="sources/cme-export")
    cme_import.add_argument("--sources", default="sources/confluence")
    cme_import.add_argument("--inventory", default="data/confluence/cme-inventory.json")

    attachments = commands.add_parser("attachments").add_subparsers(dest="command", required=True)
    docling = attachments.add_parser("docling")
    docling.add_argument("--sources", default="sources/confluence")
    docling.add_argument("--output", default="extractions/attachments")
    docling.add_argument("--docling-exe", default="docling")
    docling.add_argument("--document-timeout", type=int, default=300)
    docling.add_argument("--fail-fast", action="store_true")

    tools = commands.add_parser("tools").add_subparsers(dest="command", required=True)
    doctor = tools.add_parser("doctor")
    doctor.add_argument("--cme-exe", default="cme")
    doctor.add_argument("--docling-exe", default="docling")

    knowledge = commands.add_parser("knowledge").add_subparsers(dest="command", required=True)
    imp = knowledge.add_parser("import")
    imp.add_argument("--export", required=True)
    imp.add_argument("--extractions", default="extractions/confluence")

    tasks = knowledge.add_parser("tasks")
    tasks.add_argument("--sources", default="sources/confluence")
    tasks.add_argument("--tasks", default="knowledge-review/extraction-tasks")

    accept = knowledge.add_parser("accept-result")
    accept.add_argument("--result", required=True)
    accept.add_argument("--sources", default="sources/confluence")
    accept.add_argument("--extractions", default="extractions/confluence")

    build = knowledge.add_parser("build")
    build.add_argument("--extractions", default="extractions/confluence")
    build.add_argument("--review", default="knowledge-review")

    demo = commands.add_parser("demo")
    demo.add_argument("--export", default="examples/confluence/pages.json")
    demo.add_argument("--output", default="work/qa-brain-demo")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.area == "confluence" and args.command == "scan":
            result = scan(args.export, args.inventory)
        elif args.area == "confluence" and args.command == "sync":
            result = sync(args.export, args.inventory, args.sources)
        elif args.area == "confluence" and args.command == "collect":
            client = ConfluenceClient.from_env(
                args.base_url, timeout=args.timeout, allow_http=args.allow_http,
                allow_insecure_auth=args.allow_insecure_auth,
            )
            result = collect_to_export(client, args.root_page, args.export)
        elif args.area == "confluence" and args.command == "cme-export":
            result = run_cme_export(
                args.page_url, args.output, command=args.command, cme_executable=args.cme_exe,
                config_path=args.config_path, attachments=args.attachments, timeout=args.timeout,
                allow_http=args.allow_http, allow_insecure_auth=args.allow_insecure_auth,
            )
        elif args.area == "confluence" and args.command == "cme-import":
            result = import_cme_export(args.export_root, args.sources, args.inventory)
        elif args.area == "attachments" and args.command == "docling":
            result = parse_assets_with_docling(
                args.sources, args.output, docling_executable=args.docling_exe,
                document_timeout=args.document_timeout, fail_fast=args.fail_fast,
            )
        elif args.area == "tools" and args.command == "doctor":
            result = {"cme": tool_version(args.cme_exe), "docling": tool_version(args.docling_exe)}
        elif args.area == "knowledge" and args.command == "import":
            result = import_extractions(args.export, args.extractions)
        elif args.area == "knowledge" and args.command == "tasks":
            result = create_rich_extraction_tasks(args.sources, args.tasks)
        elif args.area == "knowledge" and args.command == "accept-result":
            result = accept_extraction_result(args.result, args.sources, args.extractions)
        elif args.area == "knowledge" and args.command == "build":
            built = build_knowledge(args.extractions, args.review)
            result = {"entities": len(built["entities"]), "conflicts": len(built["conflicts"]), "queue": built["queue"]}
        else:
            result = run_demo(args.export, args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=asdict))
        return 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"QA Brain 未完成：{exc}")
        return 2
