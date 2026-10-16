"""Small, local-first competitor news server. Python standard library only."""
from __future__ import annotations

import argparse
import calendar
from contextlib import contextmanager
import hashlib
import html
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

APP = "competitor-monitor"
VERSION = "1.0.0"
ROOT = Path(__file__).resolve().parent
KST = timezone(timedelta(hours=9))
TOPICS = {"제품", "캠페인", "기업소식", "기술"}
LOG = logging.getLogger(APP)


def now_iso():
    return datetime.now(KST).isoformat(timespec="seconds")


def window_start():
    return (datetime.now(KST).date() - timedelta(days=13)).isoformat()


def six_months_ago():
    day = datetime.now(KST).date()
    month = day.month - 6
    year = day.year
    if month < 1:
        year -= 1
        month += 12
    return day.replace(year=year, month=month,
                       day=min(day.day, calendar.monthrange(year, month)[1])).isoformat()


def valid_date(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("게시일을 확인할 수 없습니다.")
    value = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    parsed = parsed.astimezone(KST)
    if parsed.date() > datetime.now(KST).date() or parsed.year < 2000:
        raise ValueError("유효한 과거 게시일이 아닙니다.")
    return parsed.isoformat(timespec="seconds")


def clean_text(value, maximum):
    value = html.unescape(str(value or ""))
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)
    return re.sub(r"\s+", " ", value).strip()[:maximum]


def canonical_url(value):
    value = str(value or "").strip()
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("게시물 링크가 올바르지 않습니다.")
    host = parts.hostname.lower()
    try:
        if ipaddress.ip_address(host).is_private:
            raise ValueError("공식 게시물에 로컬 주소를 사용할 수 없습니다.")
    except ValueError as error:
        if "공식 게시물" in str(error):
            raise
    if host in {"localhost", "0.0.0.0"} or host.endswith(".local"):
        raise ValueError("공식 게시물에 로컬 주소를 사용할 수 없습니다.")
    pairs = [(key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "igshid", "si"}]
    if host in {"www.youtube.com", "youtube.com", "m.youtube.com"} and parts.path == "/watch":
        pairs = [(key, val) for key, val in pairs if key == "v"]
        host = "www.youtube.com"
    return urlunsplit((parts.scheme, host + (":" + str(parts.port) if parts.port else ""),
                       parts.path or "/", urlencode(pairs), ""))[:2000]


def local_addresses():
    addresses = {"127.0.0.1"}
    for name in {socket.gethostname(), socket.getfqdn()}:
        try:
            addresses.update(entry[4][0] for entry in socket.getaddrinfo(name, None, socket.AF_INET))
        except OSError:
            pass
    return sorted(addresses)


class Monitor:
    def __init__(self, data_dir, sources, config=None, collector=None):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "monitor.sqlite3"
        self.sources = sources
        self.config = config or {}
        self.collector = collector
        self.stop_event = threading.Event()
        self.collection_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.started_at = now_iso()
        self.instance_id = secrets.token_hex(16)
        self.shutdown_token = secrets.token_urlsafe(32)
        self.last_run = None
        self.next_run = None
        self.current_source = None
        self.progress = {"done": 0, "total": 0}
        self.addresses = local_addresses()
        self.port = int(self.config.get("port", 8765))
        self.host = self.config.get("host", "0.0.0.0")
        self.httpd = None
        self.interval = max(900, int(self.config.get("collection_interval_seconds", 3600)))
        self.ready_file = None
        self._init_db()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self):
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS items (
                    id TEXT PRIMARY KEY, company TEXT NOT NULL, title TEXT NOT NULL,
                    title_key TEXT NOT NULL, topic TEXT NOT NULL, summary TEXT NOT NULL,
                    published_at TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS items_date ON items(published_at DESC, id);
                CREATE INDEX IF NOT EXISTS items_company ON items(company, published_at DESC);
                CREATE INDEX IF NOT EXISTS items_title ON items(company, title_key);
                CREATE TABLE IF NOT EXISTS item_sources (
                    source_id TEXT NOT NULL, external_id TEXT NOT NULL, item_id TEXT NOT NULL,
                    url TEXT NOT NULL, published_at TEXT NOT NULL,
                    PRIMARY KEY(source_id, external_id),
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE INDEX IF NOT EXISTS item_sources_item ON item_sources(item_id);
                CREATE INDEX IF NOT EXISTS item_sources_url ON item_sources(url);
                CREATE TABLE IF NOT EXISTS source_state (
                    source_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'idle',
                    last_checked TEXT, last_success TEXT, last_item_at TEXT,
                    message TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            """)
            for source in self.sources:
                conn.execute("INSERT OR IGNORE INTO source_state(source_id,status,message) VALUES(?,?,?)",
                             (source["id"], "manual" if source.get("mode") == "manual" else "idle",
                              source.get("message", source.get("note", ""))))
                if source.get("mode") == "manual":
                    conn.execute("UPDATE source_state SET status='manual' WHERE source_id=?", (source["id"],))
            conn.execute("UPDATE source_state SET status='idle',message='재시작 후 다시 확인합니다.' WHERE status='collecting'")
            row = conn.execute("SELECT value FROM settings WHERE key='last_run'").fetchone()
            self.last_run = row[0] if row else None

    def ingest(self, source, rows):
        saved, rejected = 0, 0
        moment = now_iso()
        with self.connect() as conn:
            for raw in rows[:300]:
                try:
                    title = clean_text(raw.get("title"), 350)
                    if len(title) < 3:
                        raise ValueError("제목 없음")
                    published = valid_date(raw.get("published_at"))
                    url = canonical_url(raw.get("url"))
                    external_id = str(raw.get("external_id") or url)[:2000]
                    company = source["company"]
                    title_key = re.sub(r"[\W_]+", "", title).casefold()
                    existing = conn.execute("SELECT item_id FROM item_sources WHERE source_id=? AND external_id=? LIMIT 1",
                                            (source["id"], external_id)).fetchone()
                    listing_link = url.rstrip('/') == canonical_url(source['url']).rstrip('/')
                    if not existing and not listing_link:
                        existing = conn.execute("SELECT s.item_id FROM item_sources s JOIN items i ON i.id=s.item_id WHERE s.url=? AND i.company=? LIMIT 1", (url, company)).fetchone()
                    if not existing:
                        existing = conn.execute("SELECT id FROM items WHERE company=? AND title_key=? AND substr(published_at,1,10)=? LIMIT 1",
                                                (company, title_key, published[:10])).fetchone()
                    identity = company + "|" + (source['id'] + '|' + external_id if listing_link else url)
                    item_id = existing[0] if existing else hashlib.sha256(identity.encode()).hexdigest()[:24]
                    topic = raw.get("topic") if raw.get("topic") in TOPICS else "기업소식"
                    summary = clean_text(raw.get("summary"), 1400)
                    conn.execute("""INSERT INTO items(id,company,title,title_key,topic,summary,published_at,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                        summary=CASE WHEN length(excluded.summary)>length(items.summary) THEN excluded.summary ELSE items.summary END,
                        updated_at=excluded.updated_at""",
                                 (item_id, company, title, title_key, topic, summary, published, moment, moment))
                    conn.execute("""INSERT INTO item_sources(source_id,external_id,item_id,url,published_at) VALUES(?,?,?,?,?)
                        ON CONFLICT(source_id,external_id) DO UPDATE SET url=excluded.url,published_at=excluded.published_at""",
                                 (source["id"], external_id, item_id, url, published))
                    saved += 1
                except (ValueError, TypeError, KeyError, OverflowError):
                    rejected += 1
        return saved, rejected

    def source_rows(self):
        with self.connect() as conn:
            states = {row["source_id"]: dict(row) for row in conn.execute("SELECT * FROM source_state")}
            counts = {row[0]: row[1] for row in conn.execute("SELECT source_id,count(DISTINCT item_id) FROM item_sources GROUP BY source_id")}
        result = []
        for source in self.sources:
            row = {key: source.get(key) for key in ("id", "company", "name", "region", "platform", "url", "mode")}
            row.update(states.get(source["id"], {}))
            row["item_count"] = counts.get(source["id"], 0)
            if source.get("mode") == "manual":
                row["status"] = "manual"
                row["message"] = source.get("message") or source.get("note") or "자동 수집을 지원하지 않아 공식채널에서 직접 확인합니다."
            result.append(row)
        return result

    def query_items(self, parameters):
        def param(name, default=""):
            value = parameters.get(name, [default])
            return value[0] if isinstance(value, list) else value
        def number(name, default, low, high):
            try:
                return max(low, min(high, int(param(name, str(default)))))
            except (ValueError, TypeError):
                return default
        company, topic = param("company", "all"), param("topic", "all")
        query = str(param("q"))[:200].strip()
        include_older = param("include_older", "0") == "1" or bool(query)
        limit, offset = number("limit", 100, 1, 200), number("offset", 0, 0, 1000000)
        clauses, args = [], []
        if company in {"TYM", "LS"}:
            clauses.append("company=?")
            args.append(company)
        if topic in TOPICS:
            clauses.append("topic=?")
            args.append(topic)
        if query:
            clauses.append("(title LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\')")
            term = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            args.extend([term, term])
        base = " AND ".join(clauses) or "1=1"
        cutoff = window_start()
        with self.connect() as conn:
            older_count = conn.execute("SELECT count(*) FROM items WHERE " + base + " AND substr(published_at,1,10)<?", args + [cutoff]).fetchone()[0]
            where = base + ("" if include_older else " AND substr(published_at,1,10)>=?")
            filtered_args = args + ([] if include_older else [cutoff])
            total = conn.execute("SELECT count(*) FROM items WHERE " + where, filtered_args).fetchone()[0]
            rows = [dict(row) for row in conn.execute("SELECT * FROM items WHERE " + where + " ORDER BY published_at DESC,id ASC LIMIT ? OFFSET ?",
                                                    filtered_args + [limit, offset])]
            names = {source["id"]: source for source in self.sources}
            for row in rows:
                links = []
                for link in conn.execute("SELECT * FROM item_sources WHERE item_id=? ORDER BY published_at DESC,source_id", (row["id"],)):
                    source = names.get(link["source_id"], {})
                    links.append({"source_id": link["source_id"], "name": source.get("name", link["source_id"]),
                                  "platform": source.get("platform", "공식채널"), "url": link["url"], "published_at": link["published_at"]})
                row["sources"] = links
                first = links[0] if links else {}
                row.update(url=first.get("url", ""), source_id=first.get("source_id", ""), source_name=first.get("name", ""), platform=first.get("platform", ""), is_recent=row["published_at"][:10] >= cutoff)
                row.pop("title_key", None)
        return {"items": rows, "total": total, "older_count": older_count, "window_start": cutoff, "now": now_iso()}

    def disk_usage(self, pattern):
        total = 0
        for file in self.data_dir.glob(pattern):
            try:
                if file.is_file():
                    total += file.stat().st_size
            except OSError:
                pass
        return total

    def status(self, can_refresh=False):
        with self.connect() as conn:
            item_count = conn.execute("SELECT count(*) FROM items").fetchone()[0]
            latest = conn.execute("SELECT max(created_at) FROM items").fetchone()[0]
        with self.state_lock:
            current = self.current_source
            result = {"app": APP, "app_version": VERSION, "instance_id": self.instance_id,
                      "now": now_iso(), "started_at": self.started_at, "collecting": self.collection_lock.locked(),
                      "last_run": self.last_run, "next_run": self.next_run, "current_source_id": current["id"] if current else None,
                      "current_source_name": current["name"] if current else None, "collection_progress": dict(self.progress)}
        result.update({"item_count": item_count, "latest_update": latest, "source_count": len(self.sources),
                       "auto_count": sum(source.get("mode") == "auto" for source in self.sources),
                       "storage_bytes": self.disk_usage("monitor.sqlite3*"), "logs_bytes": self.disk_usage("monitor.log*"),
                       "backup_bytes": self.disk_usage("backups/*.sqlite3"), "can_refresh": can_refresh,
                       "local_url": "http://localhost:" + str(self.port),
                       "lan_urls": ["http://" + address + ":" + str(self.port) for address in self.addresses if not ipaddress.ip_address(address).is_loopback] if self.host == "0.0.0.0" else [],
                       "window_start": window_start(), "poll_seconds": 60})
        return result

    def request_collection(self, force=False):
        if self.stop_event.is_set() or not self.collection_lock.acquire(blocking=False):
            return False
        thread = threading.Thread(target=self._collect, args=(force,), name="official-channel-collector", daemon=True)
        thread.start()
        return True

    def _collect(self, force):
        try:
            automatic = [source for source in self.sources if source.get("mode") == "auto"]
            with self.state_lock:
                self.progress = {"done": 0, "total": len(automatic)}
            states = {row["id"]: row for row in self.source_rows()}
            for index, source in enumerate(automatic):
                if self.stop_event.is_set():
                    break
                previous = states.get(source["id"], {})
                dormant_checked = previous.get("last_checked")
                if not force and previous.get("status") == "dormant" and dormant_checked:
                    if datetime.fromisoformat(dormant_checked) > datetime.now(KST) - timedelta(days=7):
                        with self.state_lock:
                            self.progress["done"] = index + 1
                        continue
                with self.state_lock:
                    self.current_source = source
                with self.connect() as conn:
                    conn.execute("UPDATE source_state SET status='collecting' WHERE source_id=?", (source["id"],))
                moment = now_iso()
                try:
                    result = self.collector(source)
                    if result.get("status") == "manual":
                        state, message = "manual", result.get("message") or "공식채널에서 직접 확인이 필요합니다."
                        latest = previous.get("last_item_at")
                        success = previous.get("last_success")
                    elif result.get("status") != "ok":
                        raise ValueError(result.get("message") or "채널 확인에 실패했습니다.")
                    else:
                        rows = result.get("items") or []
                        saved, rejected = self.ingest(source, rows)
                        if saved == 0:
                            raise ValueError("게시일이 확인된 글을 찾지 못했습니다. 새 소식 없음으로 판단하지 않습니다.")
                        with self.connect() as conn:
                            latest = conn.execute("SELECT max(published_at) FROM item_sources WHERE source_id=?", (source["id"],)).fetchone()[0]
                        state = "dormant" if latest[:10] < six_months_ago() else "ok"
                        success = moment
                        message = ("최근 6개월 게시물이 없어 기본 수집을 쉬고, 주 1회 재확인합니다." if state == "dormant"
                                   else str(saved) + "개 게시물을 확인했습니다.")
                        if rejected:
                            message += " 날짜·링크 확인이 안 된 " + str(rejected) + "건은 제외했습니다."
                    with self.connect() as conn:
                        conn.execute("UPDATE source_state SET status=?,last_checked=?,last_success=?,last_item_at=?,message=? WHERE source_id=?",
                                     (state, moment, success, latest, clean_text(message, 400), source["id"]))
                    LOG.info("source=%s status=%s", source["id"], state)
                except Exception as error:
                    message = clean_text(str(error), 300) or type(error).__name__
                    with self.connect() as conn:
                        conn.execute("UPDATE source_state SET status='error',last_checked=?,message=? WHERE source_id=?", (moment, message, source["id"]))
                    LOG.warning("source=%s failure=%s", source["id"], message)
                finally:
                    with self.state_lock:
                        self.progress["done"] = index + 1
                self.stop_event.wait(0.3)
            if not self.stop_event.is_set():
                with self.state_lock:
                    self.last_run = now_iso()
                    self.next_run = (datetime.now(KST) + timedelta(seconds=self.interval)).isoformat(timespec="seconds")
                with self.connect() as conn:
                    conn.execute("INSERT INTO settings(key,value) VALUES('last_run',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (self.last_run,))
                self.backup()
        except Exception:
            LOG.exception("collection stopped unexpectedly")
        finally:
            with self.state_lock:
                self.current_source = None
            self.collection_lock.release()

    def backup(self):
        backup_dir = self.data_dir / "backups"
        backup_dir.mkdir(exist_ok=True)
        target = backup_dir / ("monitor-" + datetime.now(KST).date().isoformat() + ".sqlite3")
        if not target.exists():
            with self.connect() as original:
                copy = sqlite3.connect(target)
                try:
                    original.backup(copy)
                finally:
                    copy.close()
        keep = max(1, min(7, int(self.config.get("backup_count", 3))))
        owned = sorted(backup_dir.glob("monitor-????-??-??.sqlite3"), reverse=True)
        for path in owned[keep:]:
            if path.parent.resolve() == backup_dir.resolve() and path.is_file():
                path.unlink()

    def scheduler(self):
        if self.config.get("collect_on_start", True):
            self.request_collection()
        with self.state_lock:
            self.next_run = (datetime.now(KST) + timedelta(seconds=self.interval)).isoformat(timespec="seconds")
        while not self.stop_event.wait(10):
            if self.next_run and datetime.now(KST) >= datetime.fromisoformat(self.next_run):
                self.request_collection()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class Handler(BaseHTTPRequestHandler):
    server_version = "CompetitorMonitor/1"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    @property
    def monitor(self):
        return self.server.monitor

    def is_local(self):
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def valid_host(self):
        raw = self.headers.get("Host", "").lower()
        try:
            parsed = urlsplit("http://" + raw)
            return parsed.port == self.monitor.port and parsed.hostname in ({"localhost", "127.0.0.1", socket.gethostname().lower(), socket.getfqdn().lower()} | set(self.monitor.addresses))
        except ValueError:
            return False

    def reply(self, status, body, mime="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self.valid_host():
            return self.reply(400, {"error": "이 PC의 접속 주소를 사용해 주세요."})
        parsed = urlsplit(self.path)
        try:
            if parsed.path == "/api/status":
                return self.reply(200, self.monitor.status(self.is_local()))
            if parsed.path == "/api/sources":
                return self.reply(200, {"sources": self.monitor.source_rows()})
            if parsed.path == "/api/items":
                return self.reply(200, self.monitor.query_items(parse_qs(parsed.query)))
            files = {"/": ("index.html", "text/html; charset=utf-8"), "/index.html": ("index.html", "text/html; charset=utf-8"),
                     "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/styles.css": ("styles.css", "text/css; charset=utf-8"),
                     "/favicon.svg": ("favicon.svg", "image/svg+xml")}
            target = files.get(parsed.path)
            if target and (ROOT / "web" / target[0]).is_file():
                return self.reply(200, (ROOT / "web" / target[0]).read_bytes(), target[1])
            return self.reply(404, {"error": "페이지를 찾을 수 없습니다."})
        except Exception:
            LOG.exception("read request failed")
            return self.reply(500, {"error": "자료를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요."})

    def do_POST(self):
        if not self.valid_host() or not self.is_local():
            return self.reply(403, {"error": "이 PC에서만 실행할 수 있는 관리 기능입니다."})
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + self.headers.get("Host", ""):
            return self.reply(403, {"error": "허용되지 않은 요청입니다."})
        if self.headers.get("Sec-Fetch-Site") in {"cross-site", "same-site"}:
            return self.reply(403, {"error": "다른 페이지에서 보낸 관리 요청은 허용하지 않습니다."})
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            return self.reply(415, {"error": "JSON 요청이 필요합니다."})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 4096:
                return self.reply(413, {"error": "요청이 너무 큽니다."})
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            return self.reply(400, {"error": "요청 형식을 확인해 주세요."})
        path = urlsplit(self.path).path
        if path == "/api/refresh":
            started = self.monitor.request_collection(force=True)
            return self.reply(202 if started else 409, {"started": started, "error": None if started else "이미 수집 중입니다."})
        if path == "/api/shutdown":
            token = str(body.get("token", ""))
            if not secrets.compare_digest(token, self.monitor.shutdown_token) or body.get("instance_id") != self.monitor.instance_id:
                return self.reply(403, {"error": "실행 중인 프로그램 확인에 실패했습니다."})
            self.reply(200, {"stopping": True})
            self.monitor.stop_event.set()
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        return self.reply(404, {"error": "알 수 없는 관리 요청입니다."})

    def log_message(self, message, *args):
        if args and isinstance(args[0], str) and "/api/status" in args[0]:
            return
        LOG.info("http %s", clean_text(message % args, 300))


def main():
    config_path = ROOT / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8-sig")) if config_path.exists() else {}
    parser = argparse.ArgumentParser(description="경쟁사 신규 컨텐츠 로컬 서버")
    parser.add_argument("--host", default=config.get("host", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=config.get("port", 8765))
    parser.add_argument("--data-dir", default=os.path.expandvars(config.get("data_directory", str(ROOT / "data"))))
    parser.add_argument("--ready-file", default=None)
    parser.add_argument("--no-collect", action="store_true")
    args = parser.parse_args()
    config.update(host=args.host, port=args.port)
    if args.no_collect:
        config["collect_on_start"] = False
    data_dir = Path(args.data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(data_dir / "monitor.log", maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.setLevel(logging.INFO)
    LOG.addHandler(handler)
    from collectors import load_sources, collect_source
    monitor = Monitor(data_dir, load_sources(), config, collect_source)
    try:
        server = Server((args.host, args.port), Handler)
    except OSError as error:
        LOG.error("port binding failed: %s", error)
        print("접속 번호 " + str(args.port) + "번을 사용할 수 없습니다. 다른 프로그램은 종료하지 않았습니다.", flush=True)
        raise SystemExit(2)
    monitor.httpd = server
    monitor.port = server.server_address[1]
    server.monitor = monitor
    ready_file = Path(args.ready_file).resolve() if args.ready_file else data_dir / "runtime.json"
    monitor.ready_file = ready_file
    ready_file.parent.mkdir(parents=True, exist_ok=True)
    runtime = {"app": APP, "pid": os.getpid(), "instance_id": monitor.instance_id, "shutdown_token": monitor.shutdown_token,
               "host": args.host, "port": monitor.port, "server_path": str(Path(__file__).resolve()),
               "local_url": "http://localhost:" + str(monitor.port), "started_at": monitor.started_at}
    temporary = ready_file.with_suffix(".tmp")
    temporary.write_text(json.dumps(runtime, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, ready_file)
    worker = threading.Thread(target=monitor.scheduler, name="monitor-scheduler", daemon=True)
    worker.start()
    def stop_signal(signum, frame):
        monitor.stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGINT, stop_signal)
    signal.signal(signal.SIGTERM, stop_signal)
    LOG.info("server started port=%s instance=%s", monitor.port, monitor.instance_id)
    print("READY http://localhost:" + str(monitor.port), flush=True)
    try:
        server.serve_forever(poll_interval=0.3)
    finally:
        monitor.stop_event.set()
        server.server_close()
        try:
            current = json.loads(ready_file.read_text(encoding="utf-8"))
            if current.get("instance_id") == monitor.instance_id:
                ready_file.unlink()
        except (OSError, ValueError):
            pass
        LOG.info("server stopped")


if __name__ == "__main__":
    main()
