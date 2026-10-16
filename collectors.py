"""Bounded public-channel collectors. No browser login or media downloads."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

MAX_BYTES = 4 * 1024 * 1024
MAX_ITEMS = 50
REQUEST_TIMEOUT = 12
REQUEST_DEADLINE = 25
SOURCES_PATH = Path(__file__).with_name("sources.json")
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


def load_sources():
    sources = json.loads(SOURCES_PATH.read_text(encoding="utf-8-sig"))
    if not isinstance(sources, list) or len(sources) > 100:
        raise ValueError("공식채널 설정을 읽지 못했습니다.")
    return sources


def safe_link(value, base=""):
    if not isinstance(value, str) or len(value) > 2048:
        return None
    value = value.strip()
    if not value or any(ord(c) < 32 for c in value):
        return None
    absolute = urllib.parse.urljoin(base, value)
    try:
        p = urllib.parse.urlsplit(absolute)
        if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password or p.port not in (None, 80, 443):
            return None
        host = p.hostname.lower().rstrip(".")
        if host == "localhost" or "." not in host or host.endswith((".local", ".internal", ".localhost")):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass
        return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, p.query, ""))
    except ValueError:
        return None


def _validated_fetch_url(url, allowed_hosts):
    result = safe_link(url)
    host = urllib.parse.urlsplit(result or "").hostname
    if not result or host not in allowed_hosts:
        raise ValueError("등록되지 않은 수집 주소를 차단했습니다.")
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0].split("%")[0]).is_global for a in addresses):
        raise ValueError("공개 인터넷 주소만 수집할 수 있습니다.")
    return result


class _Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts):
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validated_fetch_url(newurl, self.allowed_hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_text(url, allowed_hosts):
    url = _validated_fetch_url(url, allowed_hosts)
    request = urllib.request.Request(url, headers={"User-Agent": "CompetitorMonitor/1.0 (public official channels)", "Accept": "text/html,application/xml,text/xml,application/rss+xml", "Accept-Encoding": "identity"})
    opener = urllib.request.build_opener(_Redirects(allowed_hosts))
    started = time.monotonic()
    with opener.open(request, timeout=REQUEST_TIMEOUT) as response:
        media_type = response.headers.get_content_type()
        if media_type not in ("text/html", "text/xml", "application/xml", "application/rss+xml", "application/atom+xml", "text/plain", "application/xhtml+xml"):
            raise ValueError("문서 형식이 달라 수집을 보류했습니다.")
        chunks, size = [], 0
        while True:
            block = response.read(min(65536, MAX_BYTES + 1 - size))
            size += len(block)
            if size > MAX_BYTES or time.monotonic() - started > REQUEST_DEADLINE:
                raise ValueError("응답 크기 또는 수집 시간을 초과했습니다.")
            if not block:
                break
            chunks.append(block)
        encoding = response.headers.get_content_charset() or "utf-8"
        return b"".join(chunks).decode(encoding, errors="replace")


class Node:
    def __init__(self, tag="root", attrs=(), parent=None):
        self.tag, self.attrs, self.parent, self.children = tag, dict(attrs), parent, []

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def text(self):
        if self.tag in ("script", "style", "noscript", "svg"):
            return ""
        return " ".join(c.text() if isinstance(c, Node) else c for c in self.children)

    def has_class(self, name):
        return name in self.attrs.get("class", "").split()

    def find_class(self, name):
        return next((n for n in self.walk() if n.has_class(name)), None)


class Document(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID and len(self.stack) < 200:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def clean_text(value, limit=700):
    if not isinstance(value, str):
        return ""
    if "<" in value:
        value = Document(value).root.text()
    return re.sub(r"\s+", " ", value).strip()[:limit]


def parse_date(value):
    """Only explicit publication fields; never guesses a missing year/date."""
    value = clean_text(value, 200)
    parsed = None
    match = re.fullmatch(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?", value)
    try:
        if match:
            parsed = date(*map(int, match.groups()))
        elif re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", value):
            month, day, year = map(int, value.split("/"))
            parsed = date(year, month, day)
        else:
            months = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"]
            pt = re.fullmatch(r"(\d{1,2}) de ([a-zç]+) de (\d{4})", value.lower())
            if pt and pt[2] in months:
                parsed = date(int(pt[3]), months.index(pt[2]) + 1, int(pt[1]))
            else:
                parsed = parsedate_to_datetime(value).date()
    except (ValueError, TypeError, OverflowError):
        return None
    if parsed.year < 2000 or parsed > date.today():
        return None
    return parsed.isoformat()


def topic_for(title):
    lower = title.lower()
    if any(w in lower for w in ("자율", "기술", "ai ", "autonom", "technology")):
        return "기술"
    if any(w in lower for w in ("출시", "신제품", "new model", "launch", "제품")):
        return "제품"
    if any(w in lower for w in ("이벤트", "공모", "프로모션", "브랜드 필름", "campaign", "event", "시승")):
        return "캠페인"
    return "기업소식"


def publication_value(value):
    normalized = parse_date(value)
    if not normalized:
        return None
    value = clean_text(value, 200)
    timestamp = None
    try:
        if re.match(r"^\d{4}-\d{2}-\d{2}T", value):
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        elif re.search(r"\d{2}:\d{2}", value) and not re.match(r"^\d{4}", value):
            timestamp = parsedate_to_datetime(value)
    except (ValueError, TypeError):
        pass
    if timestamp and timestamp.tzinfo:
        return timestamp.astimezone(timezone.utc).isoformat(timespec="seconds")
    return normalized


def make_item(source, title, url, published, summary=""):
    title, url, published = clean_text(title, 300), safe_link(url, source.get("fetch_url", source["url"])), publication_value(published)
    if not title or not url or not published:
        return None
    return {"external_id": hashlib.sha256(url.encode()).hexdigest()[:32], "source_id": source["id"], "company": source["company"], "title": title, "url": url, "published_at": published, "summary": clean_text(summary), "topic": topic_for(title)}


def parse_feed(source, text):
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise ValueError("지원하지 않는 피드 형식입니다.")
    root = ET.fromstring(text)
    atom = "{http://www.w3.org/2005/Atom}"
    entries = root.findall("./channel/item") or root.findall(atom + "entry")
    items = []
    for entry in entries[:MAX_ITEMS]:
        is_atom = entry.tag == atom + "entry"
        if is_atom:
            title = entry.findtext(atom + "title", "")
            link = next((n.get("href", "") for n in entry.findall(atom + "link") if n.get("rel", "alternate") == "alternate"), "")
            published = entry.findtext(atom + "published", "")
            summary = entry.findtext("{http://search.yahoo.com/mrss/}group/{http://search.yahoo.com/mrss/}description", "")
        else:
            title, link, published = (entry.findtext(k, "") for k in ("title", "link", "pubDate"))
            summary = entry.findtext("description", "")
        item = make_item(source, title, link, published, summary)
        if item:
            items.append(item)
    return items


def parse_cards(source, text):
    root, items = Document(text).root, []
    pattern = re.compile(source["link_pattern"])
    for anchor in root.walk():
        href = anchor.attrs.get("href", "").strip()
        if anchor.tag != "a" or not pattern.search(href):
            continue
        title = anchor.find_class(source["title_class"])
        published = anchor.find_class(source["date_class"])
        # A date from another card or the footer is never used.
        if title is None or published is None:
            continue
        summary = anchor.find_class(source.get("summary_class", "__none__"))
        item = make_item(source, title.text(), href, published.text(), summary.text() if summary else "")
        if item:
            items.append(item)
    return items[:MAX_ITEMS]


def parse_container_cards(source, text):
    root, items = Document(text).root, []
    for card in root.walk():
        if not card.has_class(source["container_class"]):
            continue
        title, published = card.find_class(source["title_class"]), card.find_class(source["date_class"])
        link = next((n.attrs.get("href") for n in (title.walk() if title else []) if n.tag == "a"), None)
        if not title or not published or not link:
            continue
        url = safe_link(link, source["url"])
        if not url or urllib.parse.urlsplit(url).hostname != urllib.parse.urlsplit(source["url"]).hostname:
            continue
        item = make_item(source, title.text(), url, published.text())
        if item:
            items.append(item)
    return items[:MAX_ITEMS]


def parse_tym_us(source, text):
    root, items = Document(text).root, []
    for link in root.walk():
        if link.tag != "a" or not link.has_class("latest-news-card-link"):
            continue
        # This site's article title and date are siblings of the link.
        container = link.parent.parent if link.parent and link.parent.parent else None
        if not container:
            continue
        heading = next((n for n in container.walk() if n.tag == "h3"), None)
        dates = [n.text() for n in link.parent.walk() if n.tag == "span" and parse_date(n.text())]
        if heading and len(dates) == 1:
            item = make_item(source, heading.text(), link.attrs.get("href", ""), dates[0])
            if item:
                items.append(item)
    return items[:MAX_ITEMS]


def parse_ir_list(source, text):
    items = []
    for anchor in Document(text).root.walk():
        match = re.fullmatch(r"getIrNewsDetail\((\d+)\);?", anchor.attrs.get("onclick", "").strip())
        if anchor.tag != "a" or not match or not anchor.parent or anchor.parent.tag != "li":
            continue
        published = anchor.parent.find_class("date")
        heading = anchor.find_class("title") or next((n for n in anchor.walk() if n.tag == "strong"), None)
        if not published or not heading:
            continue
        item = make_item(source, heading.text(), source["url"], published.text(), "원문 링크는 IR 목록으로 연결됩니다.")
        if item:
            # Several popup articles share this real, public list URL.
            item["external_id"] = "ir-" + match[1]
            items.append(item)
    return items[:MAX_ITEMS]


def parse_article(source, text, url):
    root = Document(text).root
    meta = {n.attrs.get("property", n.attrs.get("name", "")): n.attrs.get("content", "") for n in root.walk() if n.tag == "meta"}
    # These are explicit article publication fields, not a site's copyright or modified date.
    published = meta.get("article:published_time", "")
    title = meta.get("og:title", "")
    if published and title:
        return make_item(source, title, url, published, meta.get("og:description", ""))
    for script in root.walk():
        if script.tag != "script" or script.attrs.get("type") != "application/ld+json":
            continue
        try:
            data = json.loads("".join(c for c in script.children if isinstance(c, str)))
        except (ValueError, TypeError):
            continue
        objects = [data] if isinstance(data, dict) else data if isinstance(data, list) else []
        objects = objects + [obj for d in objects if isinstance(d, dict) for obj in d.get("@graph", [])]
        for obj in objects:
            if not isinstance(obj, dict):
                continue
            kinds = obj.get("@type", [])
            kinds = [kinds] if isinstance(kinds, str) else kinds
            detail_webpage = source.get("article_schema") == "WebPage" and "WebPage" in kinds and safe_link(obj.get("url", "")) == safe_link(url)
            if not set(kinds).intersection(("Article", "NewsArticle", "BlogPosting")) and not detail_webpage:
                continue
            item = make_item(source, obj.get("headline", obj.get("name", title)), url, obj.get("datePublished", ""), obj.get("description", ""))
            if item:
                return item
    return None


def _article_links(source, text, allowed_hosts):
    pattern, urls = re.compile(source["link_pattern"]), []
    for node in Document(text).root.walk():
        href = node.attrs.get("href", "").strip()
        if node.tag == "a" and pattern.search(href) and href not in urls:
            urls.append(href)
    items = []
    for url in urls[:6]:
        item = parse_article(source, fetch_text(url, allowed_hosts), url)
        if item:
            items.append(item)
    return items


def collect_source(source):
    # Ignore caller-supplied URLs and parser settings; only registered IDs can be fetched.
    source_id = source.get("id") if isinstance(source, dict) else None
    source = next((s for s in load_sources() if s["id"] == source_id), None)
    if not source:
        return {"items": [], "status": "error", "message": "등록되지 않은 공식채널입니다."}
    if source["mode"] == "manual":
        return {"items": [], "status": "manual", "message": source.get("note", "공식채널에서 직접 확인해 주세요.")}
    try:
        url = source.get("fetch_url", source["url"])
        host = urllib.parse.urlsplit(url).hostname
        allowed_hosts = {host, "www." + host if not host.startswith("www.") else host[4:]}
        text = fetch_text(url, allowed_hosts)
        adapters = {"feed": parse_feed, "cards": parse_cards, "container_cards": parse_container_cards, "tym_us": parse_tym_us, "ir_list": parse_ir_list}
        items = _article_links(source, text, allowed_hosts) if source["adapter"] == "article_links" else adapters[source["adapter"]](source, text)
        items = list({i["external_id"]: i for i in items}.values())
        items.sort(key=lambda i: (i["published_at"], i["external_id"]), reverse=True)
        if not items:
            return {"items": [], "status": "error", "message": "게시일이 확인된 소식을 찾지 못했습니다. 채널 구조나 접근 상태를 확인해야 합니다."}
        return {"items": items, "status": "ok", "message": f"게시일을 확인한 {len(items)}건을 수집했습니다."}
    except urllib.error.HTTPError as exc:
        return {"items": [], "status": "error", "message": f"공식채널 응답 오류({exc.code}). 다음 수집 때 재시도합니다."}
    except (TimeoutError, socket.timeout):
        return {"items": [], "status": "error", "message": "응답이 늦어 수집을 중단했습니다. 다음 수집 때 재시도합니다."}
    except (urllib.error.URLError, OSError):
        return {"items": [], "status": "error", "message": "공식채널에 연결하지 못했습니다. 인터넷 연결을 확인해 주세요."}
    except (ValueError, KeyError, ET.ParseError, RecursionError):
        return {"items": [], "status": "error", "message": "공식채널의 문서 구조 또는 게시일을 확인하지 못했습니다."}
