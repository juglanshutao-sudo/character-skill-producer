#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fz.wiki（终末地 WIKI）语料抓取器。

站点形态：Next.js App Router，无 MediaWiki API；页面数据嵌在 HTML 内
self.__next_f.push([1,"..."]) RSC payload（JSON 字符串分片）。
解析：分片按序 json.loads 拼接成 flight 文本 → 字符串感知的花括号配对扫描
→ 逐 span json.loads → 按内容键筛出目标对象。

用法：
  python scripts/endfield_wiki_api.py roster          # 名册 → _corpus/endfield-roster.json
  python scripts/endfield_wiki_api.py operators       # 全部干员档案·语音
  python scripts/endfield_wiki_api.py quests-index    # 任务索引（主线 chapters + 纪事清单）
  python scripts/endfield_wiki_api.py quests          # 主线任务正文（结构化对话）
  python scripts/endfield_wiki_api.py all             # 以上全部（cached：目标存在即跳过）
"""
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CORPUS = REPO / "examples" / "终末地" / "_corpus"
RAW = REPO / "runs" / "endfield"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
from datetime import date
RETRIEVED = date.today().isoformat()

RSC_PUSH = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')
RSC_ROW_STR = re.compile(r'(?m)^([0-9a-f]{1,5}):(?:T[0-9a-f]+,)?("(?:[^"\\]|\\.)*")')
RSC_ROW_TEXT = re.compile(r'(?m)^([0-9a-f]{1,5}):T([0-9a-f]+),')
RSC_REF = re.compile(r"^\$([0-9a-f]{1,5})$")
INLINE_MARKUP = re.compile(r"<@[^>]*>|</>")


def build_row_map(flight: str) -> dict:
    """RSC flight 行解析（顺序状态机）。

    行格式：`<hexid>:<payload>`；换行只是可选分隔。payload 形态：
    `T<datalen>,<raw text>`——datalen 是 UTF-8 字节数（实测），按字节截取再解码；
    JSON 值（字符串/数组/对象/null/数字）用 raw_decode 消费；`I[...]` 模块引用丢弃。
    返回 文本行的 id → 文本。"""
    enc = flight.encode("utf-8")
    # 字符偏移 → 字节偏移前缀表（T 行按字节定界需要换算）
    cum = [0] * (len(flight) + 1)
    acc = 0
    for idx, ch in enumerate(flight):
        acc += len(ch.encode("utf-8"))
        cum[idx + 1] = acc
    rows = {}
    i, n = 0, len(flight)
    row_start = re.compile(r"([0-9a-f]{1,5}):")
    decoder = json.JSONDecoder()
    while i < n:
        while i < n and flight[i] == "\n":
            i += 1
        m = row_start.match(flight, i)
        if not m:
            nxt = flight.find("\n", i)
            if nxt == -1:
                break
            i = nxt + 1
            continue
        rid, i = m.group(1), m.end()
        try:
            c = flight[i] if i < n else ""
            if c == "T":
                j = flight.index(",", i)
                dlen = int(flight[i + 1 : j], 16)
                b = cum[i]
                rows[rid] = enc[b + (j - i) + 1 : b + (j - i) + 1 + dlen].decode("utf-8")
                i = j + 1 + len(rows[rid])
            elif c in '"[{0123456789-tfn':
                val, end = decoder.raw_decode(flight, i)
                if isinstance(val, str):
                    rows[rid] = val
                i = end
            elif c == "I":
                _, end = decoder.raw_decode(flight, i + 1)
                i = end
            else:  # X/C/H 等标记行：跳到行尾
                nxt = flight.find("\n", i)
                if nxt == -1:
                    break
                i = nxt + 1
        except Exception:  # noqa: BLE001 - 单行解析失败跳过，避免整流中断
            nxt = flight.find("\n", i)
            if nxt == -1:
                break
            i = nxt + 1
    return rows


def resolve_refs(value, rows: dict):
    """递归把对象里的 "$hexid" 文本引用替换为行内容。"""
    if isinstance(value, str):
        m = RSC_REF.match(value)
        if m and m.group(1) in rows:
            return rows[m.group(1)]
        return value
    if isinstance(value, list):
        return [resolve_refs(v, rows) for v in value]
    if isinstance(value, dict):
        return {k: resolve_refs(v, rows) for k, v in value.items()}
    return value


def http_get(url: str, retries: int = 3) -> str:
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=40) as resp:
                return resp.read().decode("utf-8")
        except Exception as exc:  # noqa: BLE001 - 记录后重试
            last = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed after {retries} attempts: {last}")


def wiki_url(path: str) -> str:
    return "https://fz.wiki/wiki/" + urllib.parse.quote(path, safe="/")


def decode_rsc(html: str) -> str:
    parts = [json.loads(m.group(1)) for m in RSC_PUSH.finditer(html)]
    return "".join(parts)


def scan_spans(text: str):
    """字符串感知的花括号配对：返回所有平衡 {...} 的 (start, end) span。"""
    spans, starts = [], []
    depth = in_str = i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
            starts.append(i)
        elif c == "}" and depth:
            spans.append((starts.pop(), i + 1))
            depth -= 1
        i += 1
    return spans


def parse_objects(flight: str, marker: str = ""):
    """解析 flight 文本中所有可 json.loads 的对象；marker 非空时只留序列化文本含 marker 的。
    解析后递归解开 RSC 行引用（$hexid → 独立行的字符串值）。"""
    rows = build_row_map(flight)
    out = []
    for s, e in scan_spans(flight):
        chunk = flight[s:e]
        if marker and marker not in chunk:
            continue
        try:
            out.append(resolve_refs(json.loads(chunk), rows))
        except Exception:  # noqa: BLE001 - RSC 引用等不可解析 span 跳过
            continue
    return out


def clean_text(s):
    if isinstance(s, str):
        return INLINE_MARKUP.sub("", s)
    return s


# ---------------------------------------------------------------- 名册

def fetch_roster() -> list:
    html = http_get(wiki_url("干员"))
    (RAW).mkdir(parents=True, exist_ok=True)
    (RAW / "operators-index.html").write_text(html, encoding="utf-8")
    flight = decode_rsc(html)
    rows = []
    seen = set()
    for obj in parse_objects(flight, '"charId"'):
        if isinstance(obj, dict) and obj.get("charId") and obj.get("name"):
            if obj["charId"] in seen:
                continue
            seen.add(obj["charId"])
            rows.append({
                "charId": obj["charId"],
                "name": obj["name"],
                "nameEn": obj.get("nameEn", ""),
                "title": obj.get("title", ""),
                "rarity": obj.get("rarity"),
                "faction": obj.get("faction", ""),
                "race": obj.get("race", ""),
                "profession": obj.get("profession", ""),
                "element": obj.get("element", ""),
                "tags": obj.get("tags", []),
                "page": "/wiki/" + obj.get("title", "").replace(" ", ""),
            })
    rows.sort(key=lambda r: (r["rarity"] or 0, r["charId"]))
    return rows


# ---------------------------------------------------------------- 干员页

def _walk_cards(node, acc):
    if isinstance(node, dict):
        if node.get("type") == "wikiCardItem" and isinstance(node.get("attrs"), dict):
            acc.append(node["attrs"])
        for v in node.values():
            _walk_cards(v, acc)
    elif isinstance(node, list):
        for v in node:
            _walk_cards(v, acc)


def fetch_operator(op: dict) -> dict:
    url = wiki_url("干员/" + op["name"])
    html = http_get(url)
    safe = re.sub(r'[\\/:*?"<>|]', "_", op["name"])
    (RAW).mkdir(parents=True, exist_ok=True)
    (RAW / f"op-{safe}.html").write_text(html, encoding="utf-8")
    flight = decode_rsc(html)

    archive_card = None
    profile_tags = None
    meta = None
    for obj in parse_objects(flight):
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "endfieldCardCharacterArchive" and isinstance(obj.get("attrs"), dict):
            archive_card = obj
            break
        if "profileTags" in obj:
            profile_tags = obj["profileTags"]
        if "meta" in obj and isinstance(obj.get("meta"), list) and obj.get("name") == op["name"]:
            meta = obj["meta"]

    if archive_card:
        profile_tags = archive_card["attrs"].get("profileTags", profile_tags)
        meta = archive_card["attrs"].get("meta", meta)
        cards = []
        _walk_cards(archive_card, cards)
    else:
        cards = []
        for obj in parse_objects(flight, '"itemType":"endfieldCardCharacterArchive__'):
            if isinstance(obj, dict) and obj.get("type") == "wikiCardItem":
                cards.append(obj["attrs"])

    archives, voices = [], []
    for attrs in cards:
        data = attrs.get("data") or {}
        item = attrs.get("itemType", "")
        if item.endswith("__archive"):
            archives.append({
                "title": attrs.get("id", data.get("title", "")),
                "body": clean_text(data.get("body", "")),
                "unlockHint": data.get("unlockHint", ""),
            })
        elif item.endswith("__voice"):
            text = clean_text(data.get("text", ""))
            if text:
                voices.append({
                    "scene": data.get("scene", attrs.get("id", "")),
                    "text": text,
                    "unlockHint": data.get("unlockHint", ""),
                })

    profile = {}
    for tag in profile_tags or []:
        profile[tag.get("label", "")] = {
            "name": tag.get("name", ""),
            "desc": clean_text(tag.get("desc", "")),
        }

    return {
        "charId": op["charId"],
        "name": op["name"],
        "nameEn": op.get("nameEn", ""),
        "rarity": op.get("rarity"),
        "url": url,
        "retrieved_at": RETRIEVED,
        "profile": profile,
        "meta": {m.get("label", ""): m.get("value", "") for m in meta or []},
        "archives": archives,
        "voices": voices,
    }


# ---------------------------------------------------------------- 任务

def fetch_quests_index() -> dict:
    html = http_get(wiki_url("任务"))
    (RAW).mkdir(parents=True, exist_ok=True)
    (RAW / "quests-index.html").write_text(html, encoding="utf-8")
    flight = decode_rsc(html)
    groups = []
    for obj in parse_objects(flight):
        if isinstance(obj, dict) and obj.get("itemType", "").startswith("endfieldCardQuestIndex__groups"):
            groups.append(obj.get("data", {}))
    return {"url": wiki_url("任务"), "retrieved_at": RETRIEVED, "groups": groups}


def flatten_main_quests(index: dict) -> list:
    """从索引取 label=主线任务 的 chapters 树，摊平成任务清单。"""
    quests = []
    for g in index["groups"]:
        if g.get("kind") != "chapters" or g.get("label") != "主线任务":
            continue
        for chapter in g.get("chapters", []):
            ch_label = chapter.get("label", "")
            for proc in chapter.get("processes", []):
                for q in proc.get("quests", []):
                    quests.append({
                        "name": q["name"],
                        "path": q["title"],
                        "chapter": ch_label,
                        "process": proc.get("name", ""),
                        "description": clean_text(q.get("description", "")),
                    })
    return quests


def fetch_quest(item: dict) -> dict:
    url = wiki_url(item["path"])
    html = http_get(url)
    safe = re.sub(r'[\\/:*?"<>|]', "_", item["path"])
    (RAW / "quests").mkdir(parents=True, exist_ok=True)
    (RAW / "quests" / f"{safe.replace('/', '__')}.html").write_text(html, encoding="utf-8")
    flight = decode_rsc(html)

    flow = None
    info = {}
    for obj in parse_objects(flight):
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "endfieldCardQuestFlow":
            flow = obj
        if "typeLabel" in obj and "chapterName" in obj:
            info = obj
        if obj.get("itemType") == "endfieldCardQuestInfo__info" and isinstance(obj.get("data"), dict):
            info = obj["data"]

    beats = []
    if flow:
        cards = []
        _walk_cards(flow, cards)
        for attrs in cards:
            data = attrs.get("data") or {}
            if "beatId" not in data:
                continue
            beat = {"kind": data.get("kind"), "beatId": data.get("beatId")}
            if data.get("objective"):
                beat["objective"] = clean_text(data["objective"])
            loc = data.get("location") or {}
            if loc.get("sceneName"):
                beat["scene"] = loc["sceneName"]
            segs = []
            for seg in data.get("segments", []):
                if not isinstance(seg, dict):
                    continue
                stype = seg.get("type")
                if stype == "line":
                    segs.append({"type": "line", "actor": seg.get("actor", ""),
                                 "text": clean_text(seg.get("text", "")),
                                 "emotion": seg.get("emotion", "")})
                elif stype == "choice":
                    segs.append({"type": "choice",
                                 "options": [clean_text(o.get("text", "")) for o in seg.get("options", [])]})
                elif stype in ("summary", "system", "narration"):
                    segs.append({"type": "summary", "text": clean_text(seg.get("text", ""))})
                elif seg.get("text"):
                    segs.append({"type": stype or "text", "text": clean_text(seg["text"])})
            if segs:
                beat["segments"] = segs
            beats.append(beat)

    return {
        "name": item["name"],
        "path": item["path"],
        "chapter": item["chapter"],
        "process": item.get("process", ""),
        "typeLabel": info.get("typeLabel", "主线任务"),
        "groupLabel": info.get("groupLabel", ""),
        "region": info.get("region", ""),
        "description": clean_text(info.get("description", item.get("description", ""))),
        "prev": info.get("prevName", ""),
        "next": info.get("nextName", ""),
        "url": url,
        "retrieved_at": RETRIEVED,
        "beats": beats,
    }


# ---------------------------------------------------------------- 主流程

def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {path.relative_to(REPO)} ({path.stat().st_size} bytes)")


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def cmd_roster():
    rows = fetch_roster()
    save_json(CORPUS / "endfield-roster.json", {"retrieved_at": RETRIEVED, "count": len(rows), "operators": rows})
    print(f"roster: {len(rows)} entries")


def cmd_operators():
    roster = load_json(CORPUS / "endfield-roster.json")["operators"]
    ok = 0
    for op in roster:
        out = CORPUS / "operators" / f"{re.sub(chr(92)+r'[:*?\"<>|]', '_', op['name'])}.json"
        if out.exists():
            ok += 1
            continue
        data = fetch_operator(op)
        save_json(out, data)
        ok += 1
        time.sleep(0.3)
    print(f"operators: {ok}/{len(roster)}")


def cmd_quests_index():
    index = fetch_quests_index()
    save_json(CORPUS / "endfield-quests-index.json", index)
    main = flatten_main_quests(index)
    save_json(CORPUS / "endfield-main-quest-list.json", {"retrieved_at": RETRIEVED, "count": len(main), "quests": main})
    print(f"quest groups: {[g.get('label') for g in index['groups']]}")
    print(f"main quests: {len(main)}")


def cmd_quests():
    listing = load_json(CORPUS / "endfield-main-quest-list.json")["quests"]
    done = fail = 0
    for item in listing:
        safe = re.sub(r'[\\/:*?"<>|]', "_", item["path"]) + ".json"
        out = CORPUS / "quests" / f"{safe.replace('/', '__')}"
        if out.exists():
            done += 1
            continue
        try:
            data = fetch_quest(item)
            save_json(out, data)
            done += 1
        except Exception as exc:  # noqa: BLE001
            fail += 1
            print(f"FAIL {item['path']}: {exc}")
        time.sleep(0.3)
    print(f"quests: {done} ok, {fail} failed")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    CORPUS.mkdir(parents=True, exist_ok=True)
    RAW.mkdir(parents=True, exist_ok=True)
    if cmd in ("roster", "all"):
        cmd_roster()
    if cmd in ("operators", "all"):
        cmd_operators()
    if cmd in ("quests-index", "all"):
        cmd_quests_index()
    if cmd in ("quests", "all"):
        cmd_quests()
    if cmd not in ("roster", "operators", "quests-index", "quests", "all"):
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
