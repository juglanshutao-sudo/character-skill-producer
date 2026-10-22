#!/usr/bin/env python3
"""Fetch the user-designated Japanese Zenless Zone Zero Wiki corpus."""

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from urllib.error import HTTPError
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup

BASE = "https://wikiwiki.jp"
WIKI = f"{BASE}/zenless"
ROSTER_PAGE = "エージェント一覧"
FUTURE_PAGE = "実装予定エージェント"
STORY_INDEXES = ("メインストーリー", "エージェント秘話", "信頼イベント")
TODAY = date.today().isoformat()
USER_AGENT = "CharacterSkillProducer/1.0"


def page_url(title):
    return f"{WIKI}/{urllib.parse.quote(title, safe='/')}"


def fetch_html(url, timeout=30):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last_error = None
    for attempt in range(5):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.read().decode("utf-8", "replace")
        except HTTPError as exc:
            last_error = exc
            if exc.code != 429 or attempt == 4:
                raise
            retry_after = exc.headers.get("Retry-After")
            delay = int(retry_after) if retry_after and retry_after.isdigit() else 10 * (attempt + 1)
            time.sleep(delay)
    raise last_error


def clean_text(node):
    if node is None:
        return ""
    clone = BeautifulSoup(str(node), "html.parser")
    for selector in (
        "script",
        "style",
        ".contents",
        ".anchor_super",
        ".edit_form",
        ".vote",
        ".comment",
        "form",
    ):
        for item in clone.select(selector):
            item.decompose()
    text = clone.get_text("\n", strip=True)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def normalize_page(title, url, html, media_type):
    soup = BeautifulSoup(html, "html.parser")
    content = soup.select_one("#content")
    if content is None:
        raise RuntimeError("page has no #content body")
    for heading in content.find_all(["h2", "h3"]):
        label = heading.get_text(" ", strip=True)
        if label in {"コメント", "情報提供用コメントフォーム", "考察", "攻略"}:
            for sibling in list(heading.find_all_next()):
                sibling.decompose()
            heading.decompose()
            break
    text = clean_text(content)
    return {
        "title": title,
        "url": url,
        "media_type": media_type,
        "language": "ja-JP",
        "retrieved_at": TODAY,
        "text": text,
        "text_chars": len(text),
        "content_hash": "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def story_links(index_title, timeout=30):
    url = page_url(index_title)
    soup = BeautifulSoup(fetch_html(url, timeout), "html.parser")
    content = soup.select_one("#content")
    prefix = f"/zenless/{index_title}/"
    found = {}
    for anchor in content.find_all("a", href=True):
        absolute = urllib.parse.urljoin(url, anchor["href"]).split("#", 1)[0]
        parsed = urllib.parse.urlparse(absolute)
        decoded_path = urllib.parse.unquote(parsed.path)
        if not decoded_path.startswith(prefix):
            continue
        title = decoded_path.removeprefix("/zenless/")
        if title == index_title or "::cmd/" in title:
            continue
        found[absolute] = title
    return [{"title": title, "url": url} for url, title in found.items()]


def roster(timeout=30):
    url = page_url(ROSTER_PAGE)
    soup = BeautifulSoup(fetch_html(url, timeout), "html.parser")
    tables = soup.select("#content table")
    if not tables:
        raise RuntimeError("roster table not found")
    records = []
    for row in tables[0].find_all("tr")[1:]:
        cells = row.find_all(["th", "td"])
        if len(cells) < 18:
            continue
        anchor = cells[1].find("a", href=True)
        if not anchor:
            continue
        japanese_name = anchor.get_text(" ", strip=True).replace(" &｢", "＆「")
        character_url = urllib.parse.urljoin(url, anchor["href"])
        records.append(
            {
                "japanese_name": japanese_name,
                "reading_and_name": cells[1].get_text(" ", strip=True),
                "url": character_url,
                "rarity": cells[2].get_text(" ", strip=True),
                "faction": cells[3].get_text(" ", strip=True),
                "attribute": cells[4].get_text(" ", strip=True),
                "specialty": cells[5].get_text(" ", strip=True),
                "gender": cells[6].get_text(" ", strip=True),
                "version": cells[17].get_text(" ", strip=True),
                "status": "implemented",
            }
        )
    return {
        "schema_version": "1.0",
        "source": "zzz_wikiwiki_roster",
        "title": ROSTER_PAGE,
        "url": url,
        "retrieved_at": TODAY,
        "count": len(records),
        "characters": records,
    }


def future_roster(timeout=30):
    url = page_url(FUTURE_PAGE)
    soup = BeautifulSoup(fetch_html(url, timeout), "html.parser")
    records = []
    for table in soup.select("#content table"):
        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all(["th", "td"])
            if len(cells) >= 2 and cells[0].get_text(" ", strip=True) == "名前":
                name = cells[1].get_text(" ", strip=True)
                if name and not name.startswith("仮称："):
                    records.append(
                        {
                            "japanese_name": name,
                            "url": url,
                            "status": "announced",
                        }
                    )
    unique = {item["japanese_name"]: item for item in records}
    return {
        "schema_version": "1.0",
        "source": "zzz_wikiwiki_future_roster",
        "title": FUTURE_PAGE,
        "url": url,
        "retrieved_at": TODAY,
        "count": len(unique),
        "characters": list(unique.values()),
    }


def crawl(output, workers=8, timeout=30):
    targets = []
    for index in STORY_INDEXES:
        media_type = {
            "メインストーリー": "main_story_transcript",
            "エージェント秘話": "agent_story_transcript",
            "信頼イベント": "trust_event_transcript",
        }[index]
        targets.extend({**item, "media_type": media_type} for item in story_links(index, timeout))
    role_roster = roster(timeout)
    targets.extend(
        {
            "title": item["japanese_name"],
            "url": item["url"],
            "media_type": "character_profile",
        }
        for item in role_roster["characters"]
    )
    targets = list({item["url"]: item for item in targets}.values())
    output_path = Path(output)
    existing = {}
    if output_path.exists():
        try:
            previous = json.loads(output_path.read_text(encoding="utf-8"))
            existing = {item["url"]: item for item in previous.get("pages", [])}
        except json.JSONDecodeError:
            existing = {}
    pages = list(existing.values())
    failures = []

    def fetch_one(item):
        last_error = None
        for attempt in range(3):
            try:
                raw = fetch_html(item["url"], timeout)
                return normalize_page(item["title"], item["url"], raw, item["media_type"])
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(attempt + 1)
        raise last_error

    pending = [item for item in targets if item["url"] not in existing]

    def save_progress():
        payload = {
            "schema_version": "1.0",
            "source": "zzz_wikiwiki_user_designated_corpus",
            "officiality": "user_designated_corpus",
            "language": "ja-JP",
            "indexes": [page_url(title) for title in STORY_INDEXES],
            "pages": sorted(pages, key=lambda item: item["url"]),
            "failures": sorted(failures, key=lambda item: item["url"]),
            "retrieved_at": TODAY,
            "complete": False,
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    save_progress()
    completed_since_save = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_one, item): item for item in pending}
        for future in as_completed(futures):
            item = futures[future]
            try:
                pages.append(future.result())
            except Exception as exc:
                failures.append(
                    {
                        **item,
                        "status": "failed",
                        "error": type(exc).__name__,
                        "message": str(exc),
                    }
                )
            completed_since_save += 1
            if completed_since_save >= 10:
                save_progress()
                completed_since_save = 0
    pages.sort(key=lambda item: item["url"])
    failures.sort(key=lambda item: item["url"])
    payload = {
        "schema_version": "1.0",
        "source": "zzz_wikiwiki_user_designated_corpus",
        "officiality": "user_designated_corpus",
        "language": "ja-JP",
        "indexes": [page_url(title) for title in STORY_INDEXES],
        "pages": pages,
        "failures": failures,
        "retrieved_at": TODAY,
        "complete": True,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "ok": not failures,
        "output": str(output_path),
        "success_count": len(pages),
        "failure_count": len(failures),
    }


def write_result(result, output=None):
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"))


def main():
    parser = argparse.ArgumentParser(description="Fetch the Japanese ZZZ Wiki corpus")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("roster", "future"):
        command = sub.add_parser(name)
        command.add_argument("--output")
        command.add_argument("--timeout", type=int, default=30)
    command = sub.add_parser("page")
    command.add_argument("title")
    command.add_argument("--output")
    command.add_argument("--timeout", type=int, default=30)
    command = sub.add_parser("crawl")
    command.add_argument("--output", required=True)
    command.add_argument("--workers", type=int, default=8)
    command.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()
    try:
        if args.command == "roster":
            result = roster(args.timeout)
        elif args.command == "future":
            result = future_roster(args.timeout)
        elif args.command == "page":
            url = page_url(args.title)
            result = normalize_page(args.title, url, fetch_html(url, args.timeout), "manual_page")
        else:
            result = crawl(args.output, args.workers, args.timeout)
        write_result(result, getattr(args, "output", None) if args.command != "crawl" else None)
        return 0 if result.get("ok", True) else 1
    except Exception as exc:
        write_result({"ok": False, "error": type(exc).__name__, "message": str(exc)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
