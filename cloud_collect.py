"""Collect public channel metadata for GitHub Pages, without a running server.

Only web/data/news.json persists between runs. SQLite and collector processes are
temporary; this module never starts the local HTTP server or writes its database.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile

from collectors import collect_source, load_sources
from server import Monitor, TOPICS, canonical_url, clean_text, now_iso, valid_date, window_start

ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / "web" / "data" / "news.json"
PUBLIC_ERROR = "공식채널을 확인하지 못했습니다. 기존 자료는 유지하고 다음 자동 수집에서 다시 확인합니다."
SOURCE_FIELDS = ("id", "company", "name", "region", "platform", "url", "mode", "source_id",
                 "status", "last_checked", "last_success", "last_item_at", "message", "item_count")
ITEM_FIELDS = ("id", "company", "title", "topic", "summary", "published_at", "created_at", "updated_at",
               "url", "source_id", "source_name", "platform", "is_recent")


class CloudMonitor(Monitor):
    def backup(self):
        # History is committed as readable JSON by the workflow, not SQLite files.
        pass


def optional_date(value):
    return valid_date(value) if value else None


def restore_history(monitor, document):
    """Restore stable IDs and source-specific identities, including listing links."""
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("기존 자료의 형식을 확인할 수 없어 갱신을 중단했습니다.")
    items, sources = document.get("items"), document.get("sources")
    if not isinstance(items, list) or not isinstance(sources, list):
        raise ValueError("기존 게시물 또는 채널 목록이 올바르지 않습니다.")
    with monitor.connect() as conn:
        for raw in items:
            item_id = raw.get("id", "")
            if not isinstance(item_id, str) or not re.fullmatch(r"[a-f0-9]{24}", item_id):
                raise ValueError("기존 게시물의 식별자를 확인할 수 없습니다.")
            title = clean_text(raw.get("title"), 350)
            company = raw.get("company")
            if len(title) < 3 or company not in {"TYM", "LS"}:
                raise ValueError("기존 게시물의 제목 또는 회사를 확인할 수 없습니다.")
            published = valid_date(raw.get("published_at"))
            created = valid_date(raw.get("created_at") or published)
            updated = valid_date(raw.get("updated_at") or created)
            topic = raw.get("topic") if raw.get("topic") in TOPICS else "기업소식"
            links = raw.get("sources")
            if not isinstance(links, list) or not links:
                raise ValueError("기존 게시물의 공식채널 링크를 확인할 수 없습니다.")
            conn.execute("INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)", (
                item_id, company, title, re.sub(r"[\W_]+", "", title).casefold(), topic,
                clean_text(raw.get("summary"), 1400), published, created, updated))
            for link in links:
                source_id = str(link.get("source_id") or "")
                if not source_id or len(source_id) > 120:
                    raise ValueError("기존 게시물의 채널 식별자가 올바르지 않습니다.")
                url = canonical_url(link.get("url"))
                external_id = str(link.get("external_id") or url)
                if len(external_id) > 2000:
                    raise ValueError("기존 게시물의 원본 식별자가 너무 깁니다.")
                conn.execute("INSERT INTO item_sources VALUES(?,?,?,?,?)", (
                    source_id, external_id, item_id, url,
                    valid_date(link.get("published_at") or published)))
        for raw in sources:
            state = raw.get("status", "idle")
            if state not in {"ok", "error", "idle", "manual", "dormant"}:
                state = "idle"
            conn.execute("""UPDATE source_state SET status=?,last_checked=?,last_success=?,
                         last_item_at=?,message=? WHERE source_id=?""", (
                state, optional_date(raw.get("last_checked")), optional_date(raw.get("last_success")),
                optional_date(raw.get("last_item_at")),
                PUBLIC_ERROR if state == "error" else clean_text(raw.get("message"), 400), raw.get("id")))
        monitor.last_run = optional_date(document.get("status", {}).get("last_run"))
        if monitor.last_run:
            conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('last_run',?)", (monitor.last_run,))


def import_snapshot(monitor, database):
    """Use SQLite's online read-only backup API, including committed WAL entries."""
    database = Path(database).resolve(strict=True)
    source = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=15)
    destination = sqlite3.connect(monitor.db_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    monitor._init_db()


def export_document(monitor, *, failed_count=0, attempted_count=0, completed=True):
    items, offset = [], 0
    while True:
        page = monitor.query_items({"include_older": "1", "limit": "200", "offset": str(offset)})
        items.extend(page["items"])
        offset += len(page["items"])
        if offset >= page["total"] or not page["items"]:
            break
    links_by_item = {}
    names = {source["id"]: source for source in monitor.sources}
    with monitor.connect() as conn:
        for row in conn.execute("SELECT * FROM item_sources ORDER BY published_at DESC,source_id,external_id"):
            channel = names.get(row["source_id"], {})
            links_by_item.setdefault(row["item_id"], []).append({
                "source_id": row["source_id"], "external_id": row["external_id"], "url": row["url"],
                "published_at": row["published_at"], "name": channel.get("name", row["source_id"]),
                "platform": channel.get("platform", "공식채널")})
    public_items = []
    for row in items:
        item = {key: row.get(key) for key in ITEM_FIELDS}
        item["sources"] = links_by_item.get(row["id"], [])
        public_items.append(item)
    sources = []
    for raw in monitor.source_rows():
        row = {key: raw.get(key) for key in SOURCE_FIELDS}
        if row["status"] == "error":
            row["message"] = PUBLIC_ERROR
        sources.append(row)
    moment = now_iso()
    status = {
        "app": "competitor-monitor", "app_version": "1.0.0-cloud", "hosting": "github-pages",
        "now": moment, "collecting": False, "can_refresh": False,
        "last_run": monitor.last_run, "latest_update": max((row["created_at"] for row in items), default=None),
        "item_count": len(items), "source_count": len(sources),
        "auto_count": sum(source.get("mode") == "auto" for source in monitor.sources),
        "failed_count": failed_count, "attempted_count": attempted_count,
        "collection_ok": completed and (not attempted_count or failed_count < attempted_count),
        "window_start": window_start(), "poll_seconds": 300,
        "collection_interval_seconds": 3600,
        "schedule_note": "매시간 자동 확인하며 GitHub 상황에 따라 실행이 늦어질 수 있습니다.",
        "collection_progress": {"done": attempted_count, "total": attempted_count},
    }
    return {"schema_version": 1, "generated_at": moment, "items": public_items, "sources": sources, "status": status}


def write_document(output, document):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=output.parent,
                                         prefix=".news-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, output)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def bounded_collector(source, timeout=90):
    """A separate process enforces a whole-channel deadline, including DNS."""
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker", source["id"]],
                                capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                                check=False, cwd=ROOT)
        if result.returncode:
            raise ValueError("수집 작업이 정상 종료되지 않았습니다.")
        data = json.loads(result.stdout)
        if not isinstance(data, dict):
            raise ValueError("수집 결과의 형식을 확인할 수 없습니다.")
        return data
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return {"status": "error", "items": [], "message": PUBLIC_ERROR}


def run(output=DEFAULT_OUTPUT, *, snapshot=None, previous=None, sources=None, collector=None, force=False):
    output = Path(output)
    sources = load_sources() if sources is None else sources
    previous = Path(previous) if previous else output
    attempted = []
    with tempfile.TemporaryDirectory(prefix="competitor-monitor-") as directory:
        monitor = CloudMonitor(directory, sources)
        if snapshot:
            import_snapshot(monitor, snapshot)
        elif previous.exists():
            restore_history(monitor, json.loads(previous.read_text(encoding="utf-8-sig")))
        if not snapshot:
            fetch = collector or bounded_collector

            def tracked(source):
                attempted.append(source["id"])
                return fetch(source)

            monitor.collector = tracked
            monitor.collection_lock.acquire()
            monitor._collect(force=force)
        states = {row["id"]: row for row in monitor.source_rows()}
        failed = sum(states[source_id]["status"] == "error" for source_id in attempted)
        completed = bool(snapshot) or monitor.progress["done"] == monitor.progress["total"]
        document = export_document(monitor, failed_count=failed, attempted_count=len(attempted), completed=completed)
        write_document(output, document)
        return (1 if not document["status"]["collection_ok"] else 0), document


def main():
    parser = argparse.ArgumentParser(description="공식채널 소식을 GitHub Pages용 공개 JSON으로 갱신합니다.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--previous", type=Path, help="이전 JSON 경로. 기본값은 출력 파일입니다.")
    parser.add_argument("--snapshot", type=Path, help="기존 SQLite를 읽기 전용으로 가져옵니다. 네트워크 수집은 하지 않습니다.")
    parser.add_argument("--force", action="store_true", help="쉬는 채널도 이번 실행에서 다시 확인합니다.")
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        source = next((row for row in load_sources() if row["id"] == args.worker), None)
        if source is None:
            return 2
        print(json.dumps(collect_source(source), ensure_ascii=True))
        return 0
    try:
        code, document = run(args.output, snapshot=args.snapshot, previous=args.previous, force=args.force)
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error):
        print("기존 자료를 안전하게 읽거나 저장하지 못해 갱신을 중단했습니다. 기존 파일은 덮어쓰지 않았습니다.", file=sys.stderr)
        return 2
    status = document["status"]
    print(f"게시물 {status['item_count']}건 · 확인 {status['attempted_count']}개 · 확인 실패 {status['failed_count']}개")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
