#!/usr/bin/env python3
"""Fetch and index the official Honkai: Star Rail MiHoYo WIKI corpus."""

import argparse
import hashlib
import html
import json
import re
import sys
import urllib.parse
import urllib.request
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

API_BASE = "https://act-api-takumi-static.mihoyo.com/common/blackboard/sr_wiki/v1"
PAGE_BASE = "https://bbs.mihoyo.com/sr/wiki/content/{}/detail?bbs_presentation_style=no_header"
APP_SN = "sr_wiki"
TODAY = date.today().isoformat()


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.links = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style"}:
            self._skip += 1
        if tag == "a" and attrs.get("href"):
            self.links.append((attrs["href"], ""))
        if tag in {"br", "p", "div", "li", "tr", "section", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self._skip:
            self._skip -= 1
        if tag in {"p", "div", "li", "tr", "section", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        value = html.unescape(data).strip()
        if value:
            self.parts.append(value)


class TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.links = []
        self._row = None
        self._cell = None
        self._link = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = {"text": [], "href": None}
            self._row.append(self._cell)
        elif tag == "a" and self._cell is not None:
            self._link = attrs.get("href")

    def handle_endtag(self, tag):
        if tag in {"td", "th"}:
            self._link = None
            self._cell = None
        elif tag == "tr" and self._row:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        value = html.unescape(data).strip()
        if not value or self._cell is None:
            return
        self._cell["text"].append(value)
        if self._link:
            self._cell["href"] = self._link


def request_json(path, params, timeout):
    query = urllib.parse.urlencode(params)
    url = f"{API_BASE}/{path}?{query}"
    request = urllib.request.Request(url, headers={"User-Agent": "CharacterSkillProducer/1.0"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_content(content_id, timeout=30):
    payload = request_json("content/info", {"content_id": content_id, "app_sn": APP_SN}, timeout)
    if payload.get("retcode") != 0 or not payload.get("data", {}).get("content"):
        raise RuntimeError(payload.get("message") or "official WIKI returned no content")
    return payload["data"]["content"]


def content_html(content):
    return "\n".join(part.get("text", "") or "" for part in content.get("contents", []))


def plain_text(value):
    parser = TextParser()
    parser.feed(value or "")
    text = "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def all_content_links(value):
    parser = TextParser()
    parser.feed(value or "")
    found = {}
    for href, _ in parser.links:
        match = re.search(r"/sr/wiki/content/(\d+)/detail", href)
        if match:
            content_id = int(match.group(1))
            found[content_id] = PAGE_BASE.format(content_id)
    return found


def decoded_template(value):
    match = re.search(r'data-data="([^"]+)"', value or "")
    if not match:
        return []
    return json.loads(urllib.parse.unquote(match.group(1)))


def official_task_entries(value):
    templates = decoded_template(value)
    if not templates:
        return []
    categories = templates[0].get("data", {}).get("attr", [])
    entries = {}
    for category in categories:
        category_name = category.get("name_", "")
        link_parser = HTMLParserWithLinks()
        link_parser.feed(category.get("text", "") or "")
        for link in link_parser.links:
            match = re.search(r"/sr/wiki/content/(\d+)/detail", link["href"])
            if not match or not link["text"]:
                continue
            content_id = int(match.group(1))
            item = entries.setdefault(
                content_id,
                {
                    "content_id": content_id,
                    "url": PAGE_BASE.format(content_id),
                    "categories": [],
                    "index_titles": [],
                },
            )
            if category_name and category_name not in item["categories"]:
                item["categories"].append(category_name)
            if link["text"] not in item["index_titles"]:
                item["index_titles"].append(link["text"])
    return sorted(entries.values(), key=lambda item: item["content_id"])


class HTMLParserWithLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self._current = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self._current = {"href": href, "parts": []}

    def handle_data(self, data):
        if self._current:
            value = html.unescape(data).strip()
            if value:
                self._current["parts"].append(value)

    def handle_endtag(self, tag):
        if tag == "a" and self._current:
            self.links.append(
                {
                    "href": self._current["href"],
                    "text": " ".join(self._current["parts"]).strip(),
                }
            )
            self._current = None


def normalize_page(content):
    raw_html = content_html(content)
    text = plain_text(raw_html)
    return {
        "content_id": content["id"],
        "title": content.get("title", ""),
        "url": PAGE_BASE.format(content["id"]),
        "mtime": content.get("mtime"),
        "version": content.get("version"),
        "text": text,
        "text_chars": len(text),
        "content_hash": "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def component_data(content):
    result = {}
    template = content.get("rpg_new_tmp_content") or {}
    for module in template.get("modules", []):
        name = module.get("name") or module.get("id")
        values = []
        for component in module.get("components", []):
            raw = component.get("data")
            if not raw:
                continue
            try:
                data = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                data = {"raw": raw}
            values.append({"component_id": component.get("componentId"), "data": data})
        if values:
            result[name] = values
    return result


def normalize_profile(content):
    modules = component_data(content)
    template = content.get("rpg_new_tmp_content") or {}
    user_info = template.get("base", {}).get("userInfo", {})
    stories = []
    voices = []
    relations = []
    gallery = []
    info = []

    for item in modules.get("角色信息", []):
        info.extend(item["data"].get("list", []))

    for item in modules.get("角色故事", []):
        data = item["data"]
        if data.get("detail"):
            stories.append({"title": "角色简介", "text": data["detail"], "unlock": None})
        for story in data.get("list", []):
            text = story.get("desc") or story.get("content") or ""
            if text:
                stories.append(
                    {
                        "title": story.get("title") or story.get("name") or "角色故事",
                        "text": plain_text(text),
                        "unlock": story.get("append"),
                    }
                )

    for item in modules.get("角色语音", []):
        for group in item["data"].get("list", []):
            for voice in group.get("table", []):
                text = voice.get("content") or ""
                if text:
                    voices.append(
                        {
                            "language": group.get("tabName"),
                            "cv": group.get("cvName"),
                            "type": voice.get("type"),
                            "name": voice.get("name"),
                            "content": plain_text(text),
                        }
                    )

    for item in modules.get("角色关系", []):
        for group in item["data"].get("list", []):
            for relation in group.get("table", []):
                if relation.get("relation") or relation.get("desc"):
                    relations.append(
                        {
                            "role": relation.get("role", {}).get("title"),
                            "relation": relation.get("relation"),
                            "desc": plain_text(relation.get("desc") or ""),
                        }
                    )

    for item in modules.get("角色画廊", []):
        for group in item["data"].get("list", []):
            for entry in group.get("table", []):
                if entry.get("name") or entry.get("url"):
                    gallery.append(
                        {
                            "type": group.get("tabName"),
                            "name": entry.get("name"),
                            "source": entry.get("source"),
                            "url": entry.get("url"),
                        }
                    )

    profile = {
        "content_id": content["id"],
        "title": user_info.get("name") or content.get("title", ""),
        "url": PAGE_BASE.format(content["id"]),
        "mtime": content.get("mtime"),
        "version": content.get("version"),
        "base_type": user_info.get("baseType"),
        "element": user_info.get("elementId"),
        "rarity": user_info.get("rarity"),
        "info": info,
        "stories": stories,
        "voices": voices,
        "relations": relations,
        "gallery": gallery,
        "retrieved_at": TODAY,
    }
    digest_source = json.dumps(profile, ensure_ascii=False, sort_keys=True)
    profile["content_hash"] = "sha256:" + hashlib.sha256(digest_source.encode("utf-8")).hexdigest()
    return profile


def official_summary(content_id, timeout):
    content = fetch_content(content_id, timeout)
    raw_html = content_html(content)
    entries = official_task_entries(raw_html)
    return {
        "ok": True,
        "source": "hsr_official_story",
        "title": content.get("title", ""),
        "content_id": content["id"],
        "url": PAGE_BASE.format(content["id"]),
        "retrieved_at": TODAY,
        "status": "ok",
        "task_count": len(entries),
        "content_hash": "sha256:" + hashlib.sha256(raw_html.encode("utf-8")).hexdigest(),
        "warnings": [
            "task text is stored as HTML in the official API and normalized locally",
            "character coverage is determined by matching the official roster against task text",
        ],
    }


def extract_roster(content_id, timeout):
    content = fetch_content(content_id, timeout)
    parser = TableParser()
    parser.feed(content_html(content))
    records = []
    seen = set()
    for row in parser.rows:
        cells = [[item for item in cell["text"] if item] for cell in row]
        if len(cells) < 5 or len(cells[0]) != 1:
            continue
        title = cells[0][0]
        match = re.search(r"/sr/wiki/content/(\d+)/detail", row[0].get("href") or "")
        if not match or title in seen:
            continue
        seen.add(title)
        records.append(
            {
                "character": title,
                "content_id": int(match.group(1)),
                "url": PAGE_BASE.format(int(match.group(1))),
                "path": cells[1][0] if len(cells) > 1 and cells[1] else None,
                "element": cells[2][0] if len(cells) > 2 and cells[2] else None,
                "body_type": cells[3][0] if len(cells) > 3 and cells[3] else None,
                "availability": cells[4][0] if len(cells) > 4 and cells[4] else None,
            }
        )
    return {
        "ok": True,
        "source": "hsr_official_roster",
        "title": content.get("title", ""),
        "content_id": content["id"],
        "url": PAGE_BASE.format(content["id"]),
        "retrieved_at": TODAY,
        "count": len(records),
        "characters": records,
    }


def crawl(index_id, output, workers, timeout):
    index = fetch_content(index_id, timeout)
    entries = official_task_entries(content_html(index))
    entry_map = {item["content_id"]: item for item in entries}
    pages = []
    failures = []

    def fetch_one(content_id):
        last_error = None
        for attempt in range(3):
            try:
                page = normalize_page(fetch_content(content_id, timeout))
                page.update(
                    {
                        "categories": entry_map[content_id]["categories"],
                        "index_titles": entry_map[content_id]["index_titles"],
                    }
                )
                return page
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(attempt + 1)
        raise last_error

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_one, content_id): content_id for content_id in entry_map}
        for future in as_completed(futures):
            content_id = futures[future]
            try:
                pages.append(future.result())
            except Exception as exc:
                failures.append(
                    {
                        "content_id": content_id,
                        "url": entry_map[content_id]["url"],
                        "categories": entry_map[content_id]["categories"],
                        "index_titles": entry_map[content_id]["index_titles"],
                        "status": "failed",
                        "error": type(exc).__name__,
                        "message": str(exc),
                    }
                )

    pages.sort(key=lambda item: item["content_id"])
    failures.sort(key=lambda item: item["content_id"])
    payload = {
        "schema_version": "1.0",
        "source": "hsr_official_story",
        "index": {
            "content_id": index["id"],
            "title": index.get("title", ""),
            "url": PAGE_BASE.format(index["id"]),
            "retrieved_at": TODAY,
            "task_count": len(entries),
        },
        "pages": pages,
        "failures": failures,
        "retrieved_at": TODAY,
    }
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "ok": not failures,
        "output": str(output),
        "task_count": len(entries),
        "success_count": len(pages),
        "failure_count": len(failures),
    }


def main():
    parser = argparse.ArgumentParser(description="Fetch official HSR WIKI content")
    sub = parser.add_subparsers(dest="command", required=True)

    summary = sub.add_parser("summary")
    summary.add_argument("--content-id", type=int, default=2009)
    summary.add_argument("--timeout", type=int, default=30)

    roster = sub.add_parser("roster")
    roster.add_argument("--content-id", type=int, default=814)
    roster.add_argument("--output")
    roster.add_argument("--timeout", type=int, default=30)

    profile = sub.add_parser("profile")
    profile.add_argument("content_id", type=int)
    profile.add_argument("--output")
    profile.add_argument("--timeout", type=int, default=30)

    crawl_cmd = sub.add_parser("crawl")
    crawl_cmd.add_argument("--index-id", type=int, default=2009)
    crawl_cmd.add_argument("--output", required=True)
    crawl_cmd.add_argument("--workers", type=int, default=8)
    crawl_cmd.add_argument("--timeout", type=int, default=30)

    args = parser.parse_args()
    try:
        if args.command == "summary":
            result = official_summary(args.content_id, args.timeout)
        elif args.command == "roster":
            result = extract_roster(args.content_id, args.timeout)
            if args.output:
                Path(args.output).parent.mkdir(parents=True, exist_ok=True)
                Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        elif args.command == "profile":
            result = {"ok": True, **normalize_profile(fetch_content(args.content_id, args.timeout))}
            if args.output:
                Path(args.output).parent.mkdir(parents=True, exist_ok=True)
                Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            result = crawl(args.index_id, args.output, args.workers, args.timeout)
        sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"))
        return 0 if result.get("ok") else 1
    except Exception as exc:
        sys.stdout.buffer.write(
            json.dumps(
                {"ok": False, "error": type(exc).__name__, "message": str(exc)},
                ensure_ascii=False,
            ).encode("utf-8")
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
