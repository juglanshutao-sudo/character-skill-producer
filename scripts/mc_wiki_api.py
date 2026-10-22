#!/usr/bin/env python3
"""鸣潮官方 WIKI（wiki.kurobbs.com）公开 JSON API 适配器。

官方站点是 SPA，正文全部来自后端 JSON 接口。核心接口：

    POST https://api.kurobbs.com/wiki/core/catalogue/config/getTree
    POST https://api.kurobbs.com/wiki/core/catalogue/item/getPage
    POST https://api.kurobbs.com/wiki/core/catalogue/item/getEntryDetail

必须携带请求头（缺一即报「wiki库类型不能为空」）：

    source:    h5
    wiki_type: 9          # 9 = 鸣潮；2 = 战双帕弥什
    devcode:   32 位随机串（整进程复用）
    Content-Type: application/x-www-form-urlencoded

注意：/wiki/core/entry/** 与 /wiki/core/post/** 需要登录令牌（220 访问令牌不能为空）。
所有 /wiki/core/catalogue/** 接口公开可读，词条正文走 catalogue/item/getEntryDetail，
因此本适配器无需登录即可拿到官方剧情正文与角色档案全文。

用法：
    python scripts/mc_wiki_api.py tree
    python scripts/mc_wiki_api.py list --catalogue 1105
    python scripts/mc_wiki_api.py entry --id 1370471621924728832
    python scripts/mc_wiki_api.py corpus --out examples/鸣潮/_corpus
"""

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

BASE = "https://api.kurobbs.com"
WIKI_TYPE = "9"
SOURCE = "h5"
DEV_CODE = uuid.uuid4().hex
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
TODAY = date.today().isoformat()

# 官方目录 id（来自 getTree，随官方调整可能变化，用 tree 命令可复核）
CATALOGUE = {
    "character": 1105,        # 游戏图鉴 > 共鸣者
    "main_story": 1330,       # 剧情合集 > 潮汐任务剧情
    "companion_story": 1331,  # 剧情合集 > 伴星任务剧情
    "event_story": 1332,      # 剧情合集 > 活动任务剧情
    "character_guide": 1384,  # 攻略合集 > 角色攻略
}
STORY_CATALOGUES = ("main_story", "companion_story", "event_story")


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def api_post(path, data=None, timeout=30, retries=4):
    """POST form-urlencoded，返回 (ok, payload_or_error)。"""
    url = BASE + path
    payload = urllib.parse.urlencode(data or {}).encode("utf-8")
    last_error = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded;charset=UTF-8")
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("source", SOURCE)
        req.add_header("wiki_type", WIKI_TYPE)
        req.add_header("devcode", DEV_CODE)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            time.sleep(1 + 2 * attempt)
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            last_error = "响应非 JSON: " + raw[:200]
            time.sleep(1 + 2 * attempt)
            continue
        if obj.get("code") == 200:
            return True, obj.get("data")
        last_error = "code=%s msg=%s" % (obj.get("code"), obj.get("msg"))
        # 220 = 需要登录令牌，重试没有意义
        if obj.get("code") == 220:
            return False, last_error
        time.sleep(1 + 2 * attempt)
    return False, last_error


# --------------------------------------------------------------------------
# HTML -> 纯文本（标准库实现，不依赖 bs4）
# --------------------------------------------------------------------------
_BLOCK_TAGS = {
    "p", "div", "br", "tr", "table", "section", "li", "h1", "h2", "h3",
    "h4", "h5", "h6", "blockquote", "hr", "ul", "ol",
}
_SKIP_TAGS = {"script", "style"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "img":
            alt = dict(attrs).get("alt") or ""
            if alt:
                self.parts.append("[图片:%s]" % alt)

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth:
            return
        self.parts.append(data)

    def text(self):
        raw = "".join(self.parts)
        raw = raw.replace("\u00a0", " ").replace("\u200b", "")
        lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in raw.splitlines()]
        out = []
        blank = False
        for ln in lines:
            if not ln:
                if blank or not out:
                    continue
                blank = True
                out.append("")
            else:
                blank = False
                out.append(ln)
        return "\n".join(out).strip()


def html_to_text(fragment):
    """把官方富文本 HTML 片段转成可读纯文本。"""
    if not fragment:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(fragment)
        parser.close()
    except Exception:  # noqa: BLE001 - 富文本片段偶尔不闭合，尽力解析
        pass
    return parser.text()


# --------------------------------------------------------------------------
# 剧情流线性化
# --------------------------------------------------------------------------
def _node_sort_key(node_id):
    """节点 id 形如 `3` 或 `3-2`：先按主序号，再按分支序号。"""
    parts = str(node_id).split("-")
    try:
        main = int(parts[0])
    except (TypeError, ValueError):
        main = 10 ** 6
    try:
        sub = int(parts[1]) if len(parts) > 1 else 0
    except (TypeError, ValueError):
        sub = 0
    return (main, sub)


def linearize_flow(flow):
    """把 story.flow 的图结构展平成有序节点列表。

    官方结构是 DAG：节点通过 pid/cid 相连，同一父节点的多个子节点是分支选项。
    返回 [(node, is_branch_option)]，顺序沿主链推进；分支选项紧跟在其父节点之后。
    """
    raw = (flow or {}).get("raw") or []
    if not raw:
        return []
    nodes = {str(n.get("id")): n for n in raw}
    children = {}
    has_parent = set()
    for node in raw:
        nid = str(node.get("id"))
        for pid in node.get("pid") or []:
            children.setdefault(str(pid), []).append(nid)
            has_parent.add(nid)

    roots = [str(n.get("id")) for n in raw if str(n.get("id")) not in has_parent]
    root_id = ((flow or {}).get("structure") or {}).get("id")
    if root_id and str(root_id) in nodes and str(root_id) in roots:
        roots.remove(str(root_id))
        roots.insert(0, str(root_id))
    roots.sort(key=_node_sort_key)

    ordered = []
    seen = set()

    def visit(nid, as_option):
        if nid in seen or nid not in nodes:
            return
        seen.add(nid)
        ordered.append((nodes[nid], as_option))
        kids = sorted(children.get(nid, []), key=_node_sort_key)
        if not kids:
            return
        # 第一个子节点视为主链延续；其余子节点是同一层的分支选项
        visit(kids[0], False)
        for kid in kids[1:]:
            visit(kid, True)

    for rid in roots:
        visit(rid, False)
    # 兜底：孤立/断链节点按 id 追加，避免丢正文
    for node in sorted(raw, key=lambda n: _node_sort_key(n.get("id"))):
        nid = str(node.get("id"))
        if nid not in seen:
            seen.add(nid)
            ordered.append((node, False))
    return ordered


def story_to_text(story_payload):
    """单个 story 组件 -> 纯文本。"""
    flow = (story_payload or {}).get("flow") or {}
    lines = []
    for node, as_option in linearize_flow(flow):
        title = (node.get("title") or "").strip()
        body = html_to_text(node.get("content") or "")
        if title:
            lines.append(("  · 选项：" + title) if as_option else ("【选项】" + title))
        if body:
            lines.append(body)
    return "\n".join(ln for ln in lines if ln).strip()


# --------------------------------------------------------------------------
# 词条解析
# --------------------------------------------------------------------------
def _iter_components(entry):
    for module in (entry.get("content") or {}).get("modules") or []:
        mtitle = module.get("title") or ""
        for comp in module.get("components") or []:
            yield mtitle, comp


def _module_title(mtitle, comp):
    return mtitle or (comp.get("title") or "")


def render_story_entry(entry):
    """剧情词条 -> (纯文本, 章节内小节数)。"""
    content = entry.get("content") or {}
    story_map = content.get("story") or {}
    out = ["# %s" % (content.get("title") or entry.get("name") or "")]
    meta = []
    if entry.get("lastUpdateTime"):
        meta.append("官方最后更新：%s" % entry["lastUpdateTime"])
    if entry.get("lastEditUserName"):
        meta.append("最后编辑：%s" % entry["lastEditUserName"])
    if meta:
        out.append("> " + " / ".join(meta))
    out.append("")

    sections = 0
    used = set()
    for module in content.get("modules") or []:
        mtitle = module.get("title") or ""
        chunks = []
        for comp in module.get("components") or []:
            if comp.get("type") != "story-component":
                continue
            idx = comp.get("idx")
            if not idx or idx not in story_map:
                continue
            used.add(idx)
            text = story_to_text(story_map[idx])
            if text:
                chunks.append(text)
        if not chunks:
            continue
        sections += 1
        if mtitle:
            out.append("\n## %s" % mtitle)
        out.append("\n".join(chunks))
        out.append("")

    # 兜底：未被模块引用到的 story 组件
    leftovers = [k for k in story_map if k not in used]
    for idx in leftovers:
        text = story_to_text(story_map[idx])
        if text:
            sections += 1
            out.append("\n## （未归类段落 %s）" % idx[:8])
            out.append(text)
            out.append("")
    return "\n".join(out).strip(), sections


def render_character_entry(entry, keep_guide=True):
    """角色词条 -> 分节纯文本字典。

    返回 {section_title: text}。角色档案与语音是人格蒸馏的主证据；
    技能数值/养成材料默认丢弃，避免把游戏机制误当人格。
    """
    content = entry.get("content") or {}
    sections = {}
    role = None
    for _mtitle, comp in _iter_components(entry):
        if comp.get("type") == "role-component":
            role = comp.get("role") or {}
            break
    if role:
        bits = []
        info = " / ".join((x.get("text") or "") for x in role.get("info") or [])
        if info:
            bits.append("基础信息：" + info)
        if role.get("subtitle"):
            bits.append("外文名：" + role["subtitle"])
        if role.get("roleDescriptionTitle"):
            bits.append(role["roleDescriptionTitle"])
        if role.get("roleDescription"):
            bits.append("官方简介：" + role["roleDescription"])
        if bits:
            sections["角色资料"] = "\n".join(bits)

    for mtitle, comp in _iter_components(entry):
        ctype = comp.get("type")
        ctitle = comp.get("title") or ""
        label = _module_title(mtitle, comp) or ctitle

        if ctype == "audio-component":
            blocks = []
            for tab in comp.get("mediaTabs") or []:
                lines = ["— %s —" % (tab.get("title") or "语音")]
                for item in tab.get("mediaList") or []:
                    body = (item.get("content") or "").strip()
                    if not body:
                        continue
                    lines.append("・%s：%s" % (item.get("audioTitle") or "", body))
                if len(lines) > 1:
                    blocks.append("\n".join(lines))
            if blocks:
                sections[("角色语音 / %s" % ctitle).strip()] = "\n\n".join(blocks)
            continue

        if ctype == "tabs-component":
            blocks = []
            for tab in comp.get("tabs") or []:
                body = html_to_text(tab.get("content") or "")
                if not body:
                    continue
                blocks.append("### %s\n%s" % (tab.get("title") or "", body))
            if blocks:
                sections[label] = "\n\n".join(blocks)
            continue

        if ctype in ("basic-component", "text-component"):
            body = html_to_text(comp.get("content") or "")
            if not body:
                continue
            if not keep_guide and _is_build_noise(label, body):
                continue
            sections[label] = body
            continue

        if ctype == "story-component":
            # 角色词条里的剧情组件（如角色任务嵌入）
            continue

    return sections


_BUILD_NOISE = (
    "技能介绍", "角色突破材料", "技能突破材料", "角色统计", "战斗风格",
    "技能说明", "共鸣链", "角色养成", "角色攻略", "其他信息",
)


def _is_build_noise(label, body):
    if any(tag in label for tag in _BUILD_NOISE):
        return True
    # 数值表为主的段落：行数多但每行都很短
    lines = [ln for ln in body.splitlines() if ln.strip()]
    if len(lines) > 12 and sum(len(ln) for ln in lines) / max(1, len(lines)) < 12:
        return True
    return False


# --------------------------------------------------------------------------
# 目录
# --------------------------------------------------------------------------
def fetch_tree():
    ok, data = api_post("/wiki/core/catalogue/config/getTree")
    return data if ok else {"_error": data}


def flatten_tree(node, path=None, out=None):
    if out is None:
        out, path = [], []
    cur = path + [node.get("name") or ""]
    out.append({
        "id": node.get("id"),
        "name": node.get("name"),
        "parentId": node.get("parentId"),
        "path": " / ".join(p for p in cur if p),
    })
    for child in node.get("children") or []:
        flatten_tree(child, cur, out)
    return out


def fetch_catalogue(catalogue_id):
    ok, data = api_post("/wiki/core/catalogue/item/getPage", {"catalogueId": catalogue_id})
    if not ok:
        return {"_error": data}
    results = (data or {}).get("results") or {}
    return {
        "catalogue_id": catalogue_id,
        "total": results.get("total"),
        "records": [
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "entryId": str(r.get("entryId")) if r.get("entryId") else None,
                "content": r.get("content") or {},
            }
            for r in results.get("records") or []
        ],
    }


def fetch_entry(entry_id):
    ok, data = api_post("/wiki/core/catalogue/item/getEntryDetail", {"id": entry_id})
    return data if ok else {"_error": data, "_id": entry_id}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def cmd_tree(args):
    data = fetch_tree()
    if "_error" in data:
        print(json.dumps({"ok": False, "error": data["_error"]}, ensure_ascii=False))
        return 1
    flat = flatten_tree(data)
    if args.out:
        _write(args.out, json.dumps({"fetched_at": TODAY, "tree": data, "flat": flat},
                                    ensure_ascii=False, indent=1))
        print("saved -> %s (%d nodes)" % (args.out, len(flat)))
    for node in flat:
        print("%-6s %s%s" % (node["id"], "  " * (node["path"].count(" / ")), node["name"]))
    return 0


def cmd_list(args):
    data = fetch_catalogue(args.catalogue)
    if "_error" in data:
        print(json.dumps({"ok": False, "error": data["_error"]}, ensure_ascii=False))
        return 1
    if args.out:
        _write(args.out, json.dumps({"fetched_at": TODAY, **data}, ensure_ascii=False, indent=1))
        print("saved -> %s" % args.out)
    for rec in data["records"]:
        print("%s\t%s" % (rec["entryId"], rec["name"]))
    return 0


def cmd_entry(args):
    data = fetch_entry(args.id)
    if "_error" in data:
        print(json.dumps({"ok": False, "error": data["_error"]}, ensure_ascii=False))
        return 1
    if args.raw:
        print(json.dumps(data, ensure_ascii=False))
        return 0
    name = data.get("name") or ""
    if data.get("orgFullName", "").startswith("剧情任务组"):
        text, sections = render_story_entry(data)
        print(text)
        print("\n[小节数 %d]" % sections, file=sys.stderr)
    else:
        sections = render_character_entry(data, keep_guide=args.keep_guide)
        for title, body in sections.items():
            print("\n===== %s =====\n%s" % (title, body))
    return 0


def cmd_corpus(args):
    """抓取官方语料：目录 + 剧情词条 + 角色词条，并导出纯文本。"""
    out = Path(args.out)
    (out / "entries").mkdir(parents=True, exist_ok=True)
    (out / "text" / "story").mkdir(parents=True, exist_ok=True)
    (out / "text" / "character").mkdir(parents=True, exist_ok=True)

    print("[1/5] 目录树")
    tree = fetch_tree()
    if "_error" in tree:
        print("  失败：%s" % tree["_error"])
        return 1
    _write(out / "tree.json", json.dumps({"fetched_at": TODAY, "tree": tree},
                                         ensure_ascii=False, indent=1))

    print("[2/5] 目录条目")
    catalogues = {}
    for key, cid in CATALOGUE.items():
        data = fetch_catalogue(cid)
        if "_error" in data:
            print("  %s(%s) 失败：%s" % (key, cid, data["_error"]))
            continue
        # content 体积大且对蒸馏无用，只保留元信息
        for rec in data["records"]:
            rec["content"] = {
                "star": rec["content"].get("star"),
                "skillAttr": rec["content"].get("skillAttr"),
                "type": rec["content"].get("type"),
            }
        catalogues[key] = data
        print("  %-16s %-5s %d 条" % (key, cid, len(data["records"])))
    _write(out / "catalogues.json", json.dumps({"fetched_at": TODAY, "catalogues": catalogues},
                                               ensure_ascii=False, indent=1))

    jobs = []
    for key in STORY_CATALOGUES:
        for rec in catalogues.get(key, {}).get("records", []):
            if rec.get("entryId"):
                jobs.append(("story", rec["entryId"], rec["name"], key))
    for rec in catalogues.get("character", {}).get("records", []):
        if rec.get("entryId"):
            jobs.append(("character", rec["entryId"], rec["name"], "character"))

    print("[3/5] 抓取 %d 个词条正文" % len(jobs))
    fetched = {}
    failed = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(fetch_entry, j[1]): j for j in jobs}
        done = 0
        for fut in as_completed(futures):
            kind, entry_id, name, cat = futures[fut]
            done += 1
            data = fut.result()
            if "_error" in data:
                failed.append({"kind": kind, "entry_id": entry_id, "name": name,
                               "error": data["_error"]})
                continue
            _write(out / "entries" / ("%s.json" % entry_id),
                   json.dumps(data, ensure_ascii=False, indent=1))
            fetched[entry_id] = data
            if done % 10 == 0 or done == len(jobs):
                print("  %d/%d" % (done, len(jobs)))

    print("[4/5] 导出纯文本")
    story_index, char_index = [], []
    for kind, entry_id, name, cat in jobs:
        if entry_id not in fetched:
            continue
        data = fetched[entry_id]
        if kind == "story":
            text, sections = render_story_entry(data)
            safe = re.sub(r'[\\/:*?"<>|]', "_", name)
            rel = "text/story/%s.md" % safe
            _write(out / rel, text)
            story_index.append({
                "entry_id": entry_id, "name": name, "catalogue": cat,
                "text_path": rel, "chars": len(text), "sections": sections,
                "last_update": data.get("lastUpdateTime"),
                "last_editor": data.get("lastEditUserName"),
                "browse_count": data.get("browseCount"),
            })
        else:
            sections = render_character_entry(data, keep_guide=args.keep_guide)
            for title, body in sections.items():
                safe = re.sub(r'[\\/:*?"<>|]', "_", title)
                rel = "text/character/%s__%s.md" % (name, safe)
                _write(out / rel, body)
            char_index.append({
                "entry_id": entry_id, "name": name,
                "sections": {k: len(v) for k, v in sections.items()},
                "chars": sum(len(v) for v in sections.values()),
                "last_update": data.get("lastUpdateTime"),
                "last_editor": data.get("lastEditUserName"),
            })

    print("[5/5] 写索引")
    _write(out / "index.json", json.dumps({
        "fetched_at": TODAY,
        "api_base": BASE,
        "wiki_type": WIKI_TYPE,
        "catalogue_ids": CATALOGUE,
        "story": story_index,
        "character": char_index,
        "failed": failed,
    }, ensure_ascii=False, indent=1))
    print("完成：剧情 %d 章 / 角色 %d 条 / 失败 %d"
          % (len(story_index), len(char_index), len(failed)))
    return 0


def main():
    parser = argparse.ArgumentParser(description="鸣潮官方 WIKI 公开 API 适配器")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("tree", help="拉取目录树")
    p.add_argument("--out")
    p.set_defaults(func=cmd_tree)

    p = sub.add_parser("list", help="列出目录下的条目")
    p.add_argument("--catalogue", type=int, default=CATALOGUE["character"])
    p.add_argument("--out")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("entry", help="读取单个词条正文")
    p.add_argument("--id", required=True)
    p.add_argument("--raw", action="store_true", help="输出原始 JSON")
    p.add_argument("--keep-guide", action="store_true", help="角色词条保留养成/技能段落")
    p.set_defaults(func=cmd_entry)

    p = sub.add_parser("corpus", help="抓取全部官方语料")
    p.add_argument("--out", default="examples/鸣潮/_corpus")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--keep-guide", action="store_true")
    p.set_defaults(func=cmd_corpus)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
