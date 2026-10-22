#!/usr/bin/env python3
"""原神官方数据库 gi.yatta.moe（Project Amber / Yatta）JSON API 适配器。

站点为 SPA，正文全部来自后端 JSON 接口（无需登录、无需特殊请求头）：

    GET https://gi.yatta.moe/api/v2/{lang}/avatar             全角色索引
    GET https://gi.yatta.moe/api/v2/{lang}/avatar/{id}        角色档案（fetter + other）
    GET https://gi.yatta.moe/api/v2/{lang}/avatarFetter/{id}  故事全文 + 语音文字版
    GET https://gi.yatta.moe/api/v2/{lang}/furniture/{id}     Companion（尘歌壶同伴对话）
    GET https://gi.yatta.moe/api/v2/{lang}/quest              全任务索引
    GET https://gi.yatta.moe/api/v2/{lang}/quest/{id}         任务对话正文

任务对话结构（分支语义，2026-09-06 实测）：
    data.storyList[<seq>].story[<step>].taskData[]  —— 链式对话：items 为
    {节点id: {type, role, text:[{text, next}, ...], initDialog}}；SingleDialog 为
    单选项链式节点；**MultiDialog 为分支节点**（每个选项携带独立 next，分支链回敛到
    MultiDialog 自身的 next）——全量遍历时必须把每条分支走完；
    story[<step>].narratorData[]  —— 旁白（role 为空），不得混入角色台词。

用法：
    python scripts/genshin_yatta_api.py avatar 10000150 --out examples/原神/_corpus
    python scripts/genshin_yatta_api.py quest-index --out examples/原神/_corpus
    python scripts/genshin_yatta_api.py quest-scan --out examples/原神/_corpus \\
        --names 奥黛塔,苏佩茜娃 --workers 8
    python scripts/genshin_yatta_api.py derive --corpus examples/原神/_corpus \\
        --avatar 10000150 --names 奥黛塔,苏佩茜娃
"""

import argparse
import json
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

BASE = "https://gi.yatta.moe/api/v2"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
TODAY = date.today().isoformat()
COLOR_TAG = re.compile(r"<color=[^>]*>|</color>")
NICKNAME_TAG = re.compile(r"#?\{NICKNAME\}")
REALNAME_WANDERER = re.compile(r"#?\{REALNAME\[ID\(1\)\|HOSTONLY\(true\)\]\}")
REALNAME_SAURIAN = re.compile(r"#?\{REALNAME\[ID\(2\)\|(?:SHOWHOST\(true\)|HOSTONLY\(true\))\]\}")
SEXPRO_PLAYER = re.compile(r"#?\{PLAYERAVATAR#SEXPRO\[[^]]*\]\}")
SEXPRO_MATE = re.compile(r"#?\{MATEAVATAR#SEXPRO\[[^]]*\]\}")
GENDER_F = re.compile(r"#?\{F#[^}]*\}")
GENDER_M = re.compile(r"#?\{M#([^}]*)\}")
RUBY = re.compile(r"#?\{RUBY#\[?[A-Za-z]?\]?([^}]*)\}")
LINK = re.compile(r"\{LINK#\w+\}(.*?)\{/LINK\}", re.DOTALL)
NON_BREAK_SPACE = "{NON_BREAK_SPACE}"
UNKNOWN_MACRO = re.compile(r"#?\{[A-Z][A-Z_]*#[^}]*\}")
REALNAME_OTHER = re.compile(r"#?\{REALNAME\[[^]]*\]\}")
INVALID_FN = re.compile(r'[\\/:*?"<>|]')


# --------------------------------------------------------------------------
# HTTP（站点 TLS 偶发握手超时，必须带退避重试）
# --------------------------------------------------------------------------
def api_get(path, lang="chs", timeout=45, retries=5):
    url = f"{BASE}/{lang}/{path}"
    last_error = None
    for attempt in range(retries):
        req = urllib.request.Request(url)
        req.add_header("User-Agent", USER_AGENT)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - 网络/解析异常统一退避重试
            last_error = exc
            time.sleep(min(1.0 * (2 ** attempt), 10.0))
    raise RuntimeError(f"GET {url} 重试 {retries} 次仍失败: {last_error}")


def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def write_md(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def clean_text(text, strip_tags=True):
    """派生文本（research/md）用：清游戏模板变量与标签（SKILL_V2 语料清洗禁令 #1）。

    替换基准与 gi.yatta.moe 前端渲染逻辑对齐（app.js 文本管线，2026-09-06 逆向后核对）：
    - {NICKNAME} → 旅行者（站点 i18n other.traveler）；
    - {M#X}{F#Y} 性别变体 → 保留 M 支（站点默认 settings/twin=male 时渲染 M 内容）；
    - {RUBY#[X]base} → base；{LINK#…}…{/LINK} → 可见文本；{NON_BREAK_SPACE} → 删除；
    - REALNAME[ID(1)] → 流浪者、REALNAME[ID(2)] → 龙伙伴（站点 i18n wanderer/saurianCompanion）；
      PLAYERAVATAR#SEXPRO → 旅行者、MATEAVATAR#SEXPRO → 血亲（官方对玩家/血亲的规范称呼；
      站点默认渲染时直接丢弃该词，本脚本按 SKILL_V2「替换为规范称呼」处理）；
    - 其余未知大写宏 → 删除并计数（与站点兜底行为一致）。

    raw JSON 落盘不做此处理——原始语料保持原样，清洗发生在派生层。
    """
    if text is None:
        return ""
    text = REALNAME_WANDERER.sub("流浪者", text)
    text = REALNAME_SAURIAN.sub("龙伙伴", text)
    text = REALNAME_OTHER.sub("", text)
    text = SEXPRO_PLAYER.sub("旅行者", text)
    text = SEXPRO_MATE.sub("血亲", text)
    text = NICKNAME_TAG.sub("旅行者", text)
    text = GENDER_F.sub("", text)
    text = GENDER_M.sub(r"\1", text)
    text = RUBY.sub(r"\1", text)
    text = LINK.sub(r"\1", text)
    text = text.replace(NON_BREAK_SPACE, "")
    clean_text.unknown_macro_hits += len(UNKNOWN_MACRO.findall(text))
    text = UNKNOWN_MACRO.sub("", text)
    if text.startswith("#"):
        # 镜像站点渲染：起始 # 为文本标记，非内容（app.js: t.startsWith("#") → substring(1)）
        text = text[1:]
    if strip_tags:
        text = COLOR_TAG.sub("", text)
    return text


clean_text.unknown_macro_hits = 0


def role_label(role):
    # 任务/同伴对话中 role 为空 = 玩家（旅行者）发言
    return (role or "").strip() or "旅行者"


# --------------------------------------------------------------------------
# 对话链扁平化（保留全部分支）
# --------------------------------------------------------------------------
def flatten_talk(items, init_id, lines, prefix=""):
    """把 taskData 的链式对话渲染成带分支标记的文本行。

    - SingleDialog：`角色：文本` 顺序输出；
    - MultiDialog（分支）：每个选项渲染为 `分支N｜选项：...` 后带前缀递归走完该
      选项的链；分支在下游汇合处的共享后文**只渲染一次**（节点级去重，重复到达
      时标注「回到共同后文」）——分支链不立即回敛（MultiDialog 自身常无 next），
      若按分支全量重走会把共享后文重复 2^N 次（实测 77137 曾膨胀 2700 倍）；
    - 每个 taskData 链独立去重；raw JSON 保留完整链表供审计。
    """
    items = items or {}
    emitted = set()

    def walk(node_id, pfx):
        cur = node_id
        while True:
            if cur is None or cur in ("", "finish"):
                return
            key = str(cur)
            node = items.get(key)
            if node is None:
                return
            if key in emitted:
                lines.append(f"{pfx}（→ 回到共同后文，见上文）")
                return
            emitted.add(key)
            arr = node.get("text") or []
            role = role_label(node.get("role"))
            if node.get("type") == "MultiDialog" or len(arr) > 1:
                for i, opt in enumerate(arr, 1):
                    lines.append(f"{pfx}├─ 分支{i}｜选项（{role}）：{clean_text(opt.get('text'))}")
                    walk(opt.get("next"), pfx + f"│  分支{i}＞ ")
                lines.append(f"{pfx}└─ （分支回敛）")
                cur = node.get("next")
                continue
            text = clean_text(arr[0].get("text")) if arr else ""
            lines.append(f"{pfx}{role}：{text}")
            cur = (arr[0] or {}).get("next") if arr else None

    walk(init_id, prefix)


def render_task_data(task_data, lines):
    for td in task_data or []:
        init = td.get("initDialog")
        if init is not None:
            flatten_talk(td.get("items"), init, lines)


def iter_dialog_events(items, init_id):
    """按对话顺序产出 (kind, role, text) 事件流，节点级去重规则同 flatten_talk。

    kind：line=台词 / opt=分支选项行 / conv=分支回敛标记 / dup=去重截断。
    role 为原始 role（空串=旅行者）；文本已过 clean_text。
    """
    items = items or {}
    emitted = set()

    def walk(node_id):
        cur = node_id
        while True:
            if cur is None or cur in ("", "finish"):
                return
            key = str(cur)
            node = items.get(key)
            if node is None:
                return
            if key in emitted:
                yield ("dup", None, None)
                return
            emitted.add(key)
            arr = node.get("text") or []
            role = node.get("role") or ""
            if node.get("type") == "MultiDialog" or len(arr) > 1:
                for opt in arr:
                    yield ("opt", role, clean_text(opt.get("text")))
                    yield from walk(opt.get("next"))
                yield ("conv", None, None)
                cur = node.get("next")
                continue
            text = clean_text(arr[0].get("text")) if arr else ""
            yield ("line", role, text)
            cur = (arr[0] or {}).get("next") if arr else None

    yield from walk(init_id)


def step_events(step):
    """一个剧情步骤的全部事件：taskData 对话链 + narratorData 旁白（按序）。"""
    ev = []
    for td in step.get("taskData") or []:
        init = td.get("initDialog")
        if init is not None:
            ev.extend(iter_dialog_events(td.get("items"), init))
    for n in step.get("narratorData") or []:
        for it in n.get("items") or []:
            ev.append(("narration", "", clean_text(it.get("text"))))
    return ev


# 登场说话人称谓差异（仅收无歧义的别名，按 speaker role 匹配；文本提及仍按本名扫描）
ROLE_ALIASES = {
    "流浪者": ["散兵", "斯卡拉姆齐"],
    "雷电将军": ["影", "巴尔"],
    "达达利亚": ["公子"],
}


def cmd_digest_traveler(args):
    """旅行者专项切片：全任务中 role 为空（=玩家=旅行者）的台词与选项，含 ±2 行上下文。

    旅行者=用户本体（用户 2026-09-07 指令：7 元素同一人）；本切片即「旅行者」skill 的主语料。
    """
    corpus = Path(args.corpus)
    qdir = corpus / "raw" / "yatta" / "quest"
    qidx_path = corpus / "raw" / "yatta" / "quest-index.json"
    qmeta = {}
    if qidx_path.exists():
        qmeta = {int(k): v for k, v in json.loads(
            qidx_path.read_text(encoding="utf-8"))["data"]["items"].items()}
    out = corpus / "digest" / "旅行者.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    md = ["# 旅行者（avatar 10000005 男 / 10000007 女，7 元素同一人）全量登场语料切片", "",
          f"- 生成日期：{TODAY}；来源：`_corpus/raw/yatta/quest/`（gi.yatta.moe 官方任务对话，含分支）",
          "- 收录规则：任务对话中 role 为空者=玩家发言（含 MultiDialog 选项），每处命中保留前后 2 行上下文；",
          "  本切片代表「用户」在剧情中的全部言行——其他角色 skill 以此为『旅行者=用户』的对照基准。",
          ""]
    total_lines = 0
    touched = 0
    for qf in sorted(qdir.glob("*.json"), key=lambda p: int(p.stem)):
        qid = int(qf.stem)
        d = json.loads(qf.read_text(encoding="utf-8"))
        meta = qmeta.get(qid) or {}
        blocks = []
        for seq, sv in (d.get("data", {}).get("storyList") or {}).items():
            sinfo = sv.get("info") or {}
            for _, step in (sv.get("story") or {}).items():
                ev = step_events(step)
                hit = [i for i, (k, r, t) in enumerate(ev)
                       if k in ("line", "opt") and (r or "") == ""]
                if not hit:
                    continue
                want = set()
                for i in hit:
                    for j in range(max(0, i - 2), min(len(ev), i + 3)):
                        want.add(j)
                lines, prev = [], None
                for j in sorted(want):
                    if prev is not None and j > prev + 1:
                        lines.append("…（略）…")
                    k, r, t = ev[j]
                    if k == "narration":
                        lines.append(f"【旁白】{t}")
                    elif k == "opt":
                        lines.append(f"旅行者（选项）：{t}")
                    elif k == "line":
                        lines.append(f"{r or '旅行者'}：{t}")
                    prev = j
                blocks.append((int(seq), sinfo.get("title") or "",
                               step.get("title") or "", lines))
        if not blocks:
            continue
        touched += 1
        title = (meta.get("chapterTitle") or qid)
        md.append(f"## 任务 {qid}《{title}》（类型 {meta.get('type') or '-'}）")
        for seq, seg_t, step_t, lines in blocks:
            head = f"段落{seq}" + (f"·{seg_t}" if seg_t else "") + (f"·{step_t}" if step_t else "")
            md.append(f"### {head}")
            md.extend(lines)
            md.append("")
        total_lines += sum(len(b[3]) for b in blocks)
        if touched % 200 == 0:
            print(f"  … 已处理 {touched} 个命中任务，累计 {total_lines} 行")
    write_md(out, "\n".join(md))
    print(f"[digest-traveler] 正主切片：命中任务 {touched} 个，{total_lines} 行 → {out}")

    # 反主（另一胞胎，深渊一侧）语料：与正主严格分离。
    # 反主在任务对话中的标注：role 「？？？」/「???」（蒙面期）、文本提及「血亲」、后期直呼「荧」/「空」。
    TWIN_ROLES = {"？？？", "???", "荧", "空"}
    twin_quests = set()
    for qf in sorted(qdir.glob("*.json"), key=lambda p: int(p.stem)):
        raw = qf.read_text(encoding="utf-8")
        if "血亲" in raw or "荧" in raw or '"空"' in raw or "空之" in raw:
            twin_quests.add(qf)
    out2 = corpus / "digest" / "反主.md"
    md2 = ["# 反主（另一胞胎，深渊一侧）登场语料切片", "",
           f"- 生成日期：{TODAY}；来源：`_corpus/raw/yatta/quest/`",
           "- 收录规则：凡含「血亲/荧/空」提及的任务中，role 为 ？？？/???/荧/空 的台词，"
           "以及紧邻「血亲」提及的上下文（±2 行）；蒙面期标注存在噪声，蒸馏期须逐条核验归属。",
           "- 与正主（digest/旅行者.md，role 为空=玩家所选胞胎）严格分离，不得互串。",
           ""]
    t2_lines = 0
    t2_quests = 0
    for qf in twin_quests:
        qid = int(qf.stem)
        d = json.loads(qf.read_text(encoding="utf-8"))
        meta = qmeta.get(qid) or {}
        blocks = []
        for seq, sv in (d.get("data", {}).get("storyList") or {}).items():
            sinfo = sv.get("info") or {}
            for _, step in (sv.get("story") or {}).items():
                ev = step_events(step)
                hit = [i for i, (k, r, t) in enumerate(ev)
                       if (k in ("line", "opt") and ((r or "") in TWIN_ROLES))
                       or (t and "血亲" in t)]
                if not hit:
                    continue
                want = set()
                for i in hit:
                    for j in range(max(0, i - 2), min(len(ev), i + 3)):
                        want.add(j)
                lines, prev = [], None
                for j in sorted(want):
                    if prev is not None and j > prev + 1:
                        lines.append("…（略）…")
                    k, r, t = ev[j]
                    if k == "narration":
                        lines.append(f"【旁白】{t}")
                    elif k == "opt":
                        lines.append(f"旅行者（选项）：{t}")
                    else:
                        lines.append(f"{r or '旅行者'}：{t}")
                    prev = j
                blocks.append((int(seq), sinfo.get("title") or "", step.get("title") or "", lines))
        if not blocks:
            continue
        t2_quests += 1
        md2.append(f"## 任务 {qid}《{meta.get('chapterTitle') or qid}》（类型 {meta.get('type') or '-'}）")
        for seq, seg_t, step_t, lines in blocks:
            head = f"段落{seq}" + (f"·{seg_t}" if seg_t else "") + (f"·{step_t}" if step_t else "")
            md2.append(f"### {head}")
            md2.extend(lines)
            md2.append("")
        t2_lines += sum(len(b[3]) for b in blocks)
    write_md(out2, "\n".join(md2))
    print(f"[digest-traveler] 反主切片：命中任务 {t2_quests} 个，{t2_lines} 行 → {out2}")
    return 0


def cmd_digest(args):
    """从已缓存任务原文本地切出每个角色的登场语料：本人台词 + 前后文窗口。"""
    corpus = Path(args.corpus)
    qdir = corpus / "raw" / "yatta" / "quest"
    idx_path = corpus / "raw" / "yatta" / "avatar-index.json"
    if not idx_path.exists():
        roster = released_avatars()
    else:
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
        roster = [(int(k), v["name"], v.get("route") or "", v.get("rank"))
                  for k, v in idx.items()]
    qmeta = {}
    qidx_path = corpus / "raw" / "yatta" / "quest-index.json"
    if qidx_path.exists():
        qmeta = {int(k): v for k, v in json.loads(
            qidx_path.read_text(encoding="utf-8"))["data"]["items"].items()}

    names = [n for _, n, _, _ in roster]
    aliases = {n: [n] + ROLE_ALIASES.get(n, []) for n in names}
    hits = {n: {} for n in names}
    digest_parts = {n: [] for n in names}
    files = sorted(qdir.glob("*.json"), key=lambda p: int(p.stem))
    parsed = 0
    for qf in files:
        raw = qf.read_text(encoding="utf-8")
        touched = [n for n in names if n in raw]
        for n in touched:
            hits[n][int(qf.stem)] = raw.count(n)
        if not touched:
            continue
        qid = int(qf.stem)
        qdata = json.loads(raw)
        parsed += 1
        for n in touched:
            roles = set(aliases[n])
            blocks = []
            for seq, sv in (qdata.get("data", {}).get("storyList") or {}).items():
                sinfo = sv.get("info") or {}
                for _, step in (sv.get("story") or {}).items():
                    ev = step_events(step)
                    hit_idx = [i for i, (k, r, t) in enumerate(ev)
                               if k in ("line", "opt")
                               and (r in roles or n in (t or ""))]
                    if not hit_idx:
                        continue
                    # 命中行 ±3 上下文，窗口合并
                    want = set()
                    for i in hit_idx:
                        for j in range(max(0, i - 3), min(len(ev), i + 4)):
                            want.add(j)
                    lines = []
                    prev = None
                    for j in sorted(want):
                        if prev is not None and j > prev + 1:
                            lines.append("…（略）…")
                        k, r, t = ev[j]
                        if k == "narration":
                            lines.append(f"【旁白】{t}")
                        elif k == "dup":
                            lines.append("（→ 回到共同后文，见上文）")
                        elif k == "opt":
                            who = r or "旅行者"
                            lines.append(f"{who}（选项）：{t}")
                        elif k == "conv":
                            pass
                        else:
                            lines.append(f"{r or '旅行者'}：{t}")
                        prev = j
                    blocks.append((int(seq), step.get("id") or 0,
                                   sinfo.get("title") or "",
                                   step.get("title") or "", lines))
            if blocks:
                meta = qmeta.get(qid) or {}
                title = (meta.get("chapterTitle")
                         or (qdata.get("data", {}).get("info") or {}).get("chapterTitle")
                         or qid)
                digest_parts[n].append(
                    {"qid": qid, "title": title,
                     "type": meta.get("type"), "count": hits[n][qid],
                     "steps": blocks})

    out_dir = corpus / "digest"
    (corpus / "quest-hits-all.json").write_text(
        json.dumps({n: {str(k): v for k, v in hits[n].items()} for n in names},
                   ensure_ascii=False), encoding="utf-8")
    summary = []
    for aid, n, route, rank in roster:
        parts = digest_parts[n]
        dest = out_dir / f"{n}.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        md = [f"# {n}（avatar {aid}）登场语料切片", "",
              f"- 生成日期：{TODAY}；来源：`_corpus/raw/yatta/quest/`（gi.yatta.moe 官方任务对话，含分支）",
              f"- 登场任务 {len(parts)} 个；本人台词按 speaker 精确匹配"
              + ("（含别名：" + "/".join(ROLE_ALIASES[n]) + "）" if ROLE_ALIASES.get(n) else "")
              + "，他人提及按文本匹配；每处命中保留前后 3 行上下文，跨分支去重共享后文。",
              ""]
        for part in parts:
            md.append(f"## 任务 {part['qid']}《{part['title']}》（类型 {part.get('type') or '-'}，提及 {part['count']} 次）")
            for seq, _sid, seg_t, step_t, lines in part["steps"]:
                head = f"段落{seq}" + (f"·{seg_t}" if seg_t else "") + (f"·{step_t}" if step_t else "")
                md.append(f"### {head}")
                md.extend(lines)
                md.append("")
        write_md(dest, "\n".join(md))
        summary.append((n, len(parts), dest.stat().st_size))
    summary.sort(key=lambda x: -x[1])
    print(f"[digest] 解析任务 {parsed} 个；语料切片 → {out_dir}/")
    for n, qn, size in summary[:12]:
        print(f"   {n}: {qn} 任务, {size//1024}KB")
    return 0


def render_story_step(step, lines):
    lines.append(f"### {step.get('title') or '(无标题步骤)'} (id={step.get('id')})")
    for td in step.get("taskData") or []:
        render_task_data([td], lines)
    for n in step.get("narratorData") or []:
        for it in n.get("items") or []:
            # narratorData = 旁白/系统叙述，role 为空：显式标【旁白】与角色台词隔离
            lines.append(f"【旁白】{clean_text(it.get('text'))}")


# --------------------------------------------------------------------------
# 命令：avatar / quest-index / quest-scan / derive
# --------------------------------------------------------------------------
def cmd_avatar(args):
    out = Path(args.out)
    aid = args.avatar_id
    raw_dir = out / "raw" / "yatta" / "avatar"
    detail = api_get(f"avatar/{aid}")
    write_json(raw_dir / f"{aid}_detail.json", detail)
    fetter = api_get(f"avatarFetter/{aid}")
    write_json(raw_dir / f"{aid}_fetter.json", fetter)
    fid = (detail.get("data", {}).get("other") or {}).get("furnitureId")
    furniture = None
    if fid:
        furniture = api_get(f"furniture/{fid}")
        write_json(raw_dir / f"{aid}_furniture.json", furniture)
    print(f"[avatar] {aid} 已落盘: detail/fetter"
          + ("/furniture" if furniture else ""))
    return 0


TRAVELER_IDS = {10000005, 10000007}


def released_avatars():
    """返回 [(aid, name, route, rank)]：已实装、非旅行者、非未公开占位。"""
    items = api_get("avatar")["data"]["items"]
    now = time.time()
    roster = []
    for k, v in items.items():
        if not k.isdigit():
            continue  # 旅行者元素变体键（10000005-pyro 等）不在批量名册内
        aid = int(k)
        name = v.get("name") or ""
        if aid in TRAVELER_IDS or "$" in name:
            continue
        if not v.get("release") or v["release"] >= now:
            continue
        roster.append((aid, name, v.get("route") or "", v.get("rank")))
    roster.sort()
    return roster


def cmd_avatar_all(args):
    out = Path(args.out)
    raw_dir = out / "raw" / "yatta" / "avatar"
    roster = released_avatars()
    write_json(out / "raw" / "yatta" / "avatar-index.json",
               {str(a): {"name": n, "route": r, "rank": rk} for a, n, r, rk in roster})
    print(f"[avatar-all] 待抓取 {len(roster)} 个角色")

    failed = []
    done = 0

    def work(aid):
        dest = raw_dir / f"{aid}_fetter.json"
        if dest.exists() and dest.stat().st_size > 2:
            return aid, True
        detail = api_get(f"avatar/{aid}")
        write_json(raw_dir / f"{aid}_detail.json", detail)
        fetter = api_get(f"avatarFetter/{aid}")
        write_json(dest, fetter)
        fid = (detail.get("data", {}).get("other") or {}).get("furnitureId")
        if fid:
            fdest = raw_dir / f"{aid}_furniture.json"
            if not fdest.exists():
                write_json(fdest, api_get(f"furniture/{fid}"))
        return aid, False

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(work, aid): aid for aid, *_ in roster}
        for fut in as_completed(futures):
            aid = futures[fut]
            try:
                _, cached = fut.result()
            except Exception as exc:  # noqa: BLE001
                failed.append({"avatar_id": aid, "error": str(exc)})
            done += 1
            if done % 20 == 0:
                print(f"  … {done}/{len(roster)}（失败 {len(failed)}）")
    write_json(out / "raw" / "yatta" / "avatar-fetch-failed.json", failed)
    print(f"[avatar-all] 完成：{len(roster)} 角色，失败 {len(failed)}")
    return 0


def _derive_one_avatar(aid, corpus):
    raw = corpus / "raw" / "yatta" / "avatar"
    detail = json.loads((raw / f"{aid}_detail.json").read_text(encoding="utf-8"))["data"]
    fetter = json.loads((raw / f"{aid}_fetter.json").read_text(encoding="utf-8"))["data"]
    fpath = raw / f"{aid}_furniture.json"
    furniture = json.loads(fpath.read_text(encoding="utf-8"))["data"] if fpath.exists() else None
    char_dir = corpus / "avatar" / detail.get("name", str(aid))

    f = detail.get("fetter") or {}
    cv = f.get("cv") or {}
    prof = [
        f"# {detail.get('name')}（avatar {aid}）档案",
        "",
        f"- 来源：https://gi.yatta.moe/chs/archive/avatar/{aid}（检索日期 {TODAY}）",
        f"- 官方数据快照：`_corpus/raw/yatta/avatar/{aid}_detail.json`",
        "",
        "| 字段 | 内容 |",
        "|---|---|",
        f"| 称号 | {f.get('title')} |",
        f"| 简介 | {clean_text(f.get('detail'))} |",
        f"| 元素 | {detail.get('element')} |",
        f"| 武器 | {detail.get('weaponType')} |",
        f"| 所属/_native | {f.get('native')} |",
        f"| 命之座 | {f.get('constellation')} |",
        f"| 生日 | {'-'.join(map(str, detail.get('birthday') or []))} |",
        f"| CV | CHS {cv.get('CHS')} / JP {cv.get('JP')} / EN {cv.get('EN')} / KR {cv.get('KR')} |",
        f"| 实装时间 | {detail.get('release')}（Unix 秒） |",
        "",
    ]
    write_md(char_dir / "资料.md", "\n".join(prof))

    o = detail.get("other") or {}
    other = [f"# {detail.get('name')} 其他（服装/名片/特色料理）", ""]
    for c in o.get("costume") or []:
        other += [f"## 服装：{c.get('name')}", clean_text(c.get("description")), ""]
    nc = o.get("namecard")
    if nc:
        other += [f"## 名片：{nc.get('name')}", clean_text(nc.get("description")), ""]
    sf = o.get("specialFood")
    if sf:
        other += [f"## 特色料理：{sf.get('name')}（{sf.get('rank')} 星）", ""]
    write_md(char_dir / "其他.md", "\n".join(other))

    story = [f"# {detail.get('name')} 角色故事（avatarFetter.story）", "",
             f"- 来源：https://gi.yatta.moe/chs/archive/avatar/{aid} 故事标签（检索日期 {TODAY}）", ""]
    for k, s in (fetter.get("story") or {}).items():
        story += [f"## {s.get('title')}", clean_text(s.get("text")), ""]
    write_md(char_dir / "故事.md", "\n".join(story))

    quotes = [f"# {detail.get('name')} 语音（文字版，共 {len(fetter.get('quotes') or {})} 条）", "",
              f"- 来源：https://gi.yatta.moe/chs/archive/avatar/{aid} 语音标签（检索日期 {TODAY}）", ""]
    for k, q in (fetter.get("quotes") or {}).items():
        quotes += [f"## {q.get('title')}", clean_text(q.get("text")), ""]
        if q.get("tips"):
            quotes += [f"> 备注：{clean_text(q['tips'])}", ""]
    write_md(char_dir / "语音.md", "\n".join(quotes))

    if furniture:
        comp = [f"# {detail.get('name')} Companion（尘歌壶同伴对话）", "",
                f"- 来源：https://gi.yatta.moe/chs/archive/avatar/{aid} Companion 标签（检索日期 {TODAY}）",
                f"- 官方数据快照：`_corpus/raw/yatta/avatar/{aid}_furniture.json`",
                f"- 家具 id：{furniture.get('id')}（{furniture.get('name')}）", ""]
        for k, s in (furniture.get("story") or {}).items():
            lines = []
            for td in s.get("taskData") or []:
                render_task_data([td], lines)
            comp += [f"## 场景 {k}", *lines, ""]
        write_md(char_dir / "Companion对话.md", "\n".join(comp))
    return detail.get("name", str(aid)), (furniture is not None)


def cmd_derive_all(args):
    corpus = Path(args.corpus)
    raw = corpus / "raw" / "yatta" / "avatar"
    details = sorted(raw.glob("*_detail.json"),
                     key=lambda p: int(p.name.split("_")[0]))
    ok, miss_furniture = 0, 0
    for dpath in details:
        aid = int(dpath.name.split("_")[0])
        name, has_f = _derive_one_avatar(aid, corpus)
        ok += 1
        miss_furniture += 0 if has_f else 1
    print(f"[derive-all] {ok} 个角色派生完成（{miss_furniture} 个无 Companion 数据）")
    return 0


def cmd_quest_index(args):
    out = Path(args.out)
    q = api_get("quest")
    path = out / "raw" / "yatta" / "quest-index.json"
    write_json(path, q)
    items = q.get("data", {}).get("items", {})
    print(f"[quest-index] 共 {len(items)} 条任务索引 → {path}")
    return 0


def _fetch_quest(qid):
    return api_get(f"quest/{qid}")


def cmd_quest_scan(args):
    out = Path(args.out)
    names = [n.strip() for n in (args.names or "").split(",") if n.strip()]
    qdir = out / "raw" / "yatta" / "quest"

    idx_path = out / "raw" / "yatta" / "quest-index.json"
    if idx_path.exists():
        items = json.loads(idx_path.read_text(encoding="utf-8"))["data"]["items"]
    else:
        items = api_get("quest")["data"]["items"]
        write_json(idx_path, {"data": {"items": items}})
    qids = sorted(int(k) for k in items)
    print(f"[quest-scan] 任务总数 {len(qids)}；命中姓名 {names}")

    hits = {n: {} for n in names}
    failed = []
    done = 0

    def work(qid):
        dest = qdir / f"{qid}.json"
        if dest.exists() and dest.stat().st_size > 2:
            raw = dest.read_text(encoding="utf-8")
            return qid, raw, True
        data = _fetch_quest(qid)
        raw = json.dumps(data, ensure_ascii=False)
        write_json(dest, data)
        return qid, raw, False

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(work, qid): qid for qid in qids}
        for fut in as_completed(futures):
            qid = futures[fut]
            try:
                _, raw, cached = fut.result()
            except Exception as exc:  # noqa: BLE001
                failed.append({"quest_id": qid, "error": str(exc)})
                done += 1
                if done % 50 == 0:
                    print(f"  … {done}/{len(qids)}（失败 {len(failed)}）")
                continue
            info = items[str(qid)]
            for n in names:
                cnt = raw.count(n)
                if cnt:
                    hits[n][str(qid)] = {
                        "title": info.get("chapterTitle"),
                        "type": info.get("type"),
                        "count": cnt,
                    }
            done += 1
            if not cached and done % 50 == 0:
                print(f"  … {done}/{len(qids)}（失败 {len(failed)}）")

    write_json(out / "quest-hits.json", {
        "retrieved_at": TODAY,
        "names": names,
        "total_quests": len(qids),
        "failed": failed,
        "hits": hits,
    })
    md = [f"# 任务命中索引（{TODAY}）", "",
          f"- 扫描任务总数：{len(qids)}；失败：{len(failed)}", ""]
    for n in names:
        md.append(f"## 「{n}」命中 {len(hits[n])} 个任务")
        md.append("")
        md.append("| 任务ID | 类型 | 章节 | 出现次数 |")
        md.append("|---|---|---|---|")
        for qid, h in sorted(hits[n].items(), key=lambda kv: int(kv[0])):
            md.append(f"| {qid} | {h.get('type') or '-'} | {h.get('title')} | {h['count']} |")
        md.append("")
    write_md(out / "quest-hits.md", "\n".join(md))
    print(f"[quest-scan] 完成：{len(qids)} 任务，失败 {len(failed)}；"
          + "；".join(f"{n}:{len(hits[n])}" for n in names))
    return 0


def _render_quest_md(qid, qdata, meta):
    info = qdata.get("data", {}).get("info", {})
    lines = [
        f"# {info.get('chapterTitle') or meta.get('title') or qid}（任务 id={qid}，类型 {info.get('type') or '-'}）",
        "",
        f"- 来源：https://gi.yatta.moe/chs/archive/quest/{qid}（检索日期 {TODAY}）",
        f"- 官方数据快照：`_corpus/raw/yatta/quest/{qid}.json`",
        "- 说明：`分支` 为玩家选项（MultiDialog），每条分支均已收录；【旁白】为系统叙述，非角色台词；role 为空者按旅行者（玩家）记录。",
        "",
    ]
    for seq, sv in (qdata.get("data", {}).get("storyList") or {}).items():
        sinfo = sv.get("info") or {}
        lines.append(f"## 段落{seq}：{sinfo.get('title') or ''}")
        if sinfo.get("description"):
            lines.append(f"\n> {clean_text(sinfo['description'])}")
        lines.append("")
        for _, step in (sv.get("story") or {}).items():
            render_story_step(step, lines)
            lines.append("")
    return "\n".join(lines)


def cmd_derive(args):
    corpus = Path(args.corpus)
    names = [n.strip() for n in (args.names or "").split(",") if n.strip()]

    if args.avatar_id:
        aid = args.avatar_id
        raw = corpus / "raw" / "yatta" / "avatar"
        detail = json.loads((raw / f"{aid}_detail.json").read_text(encoding="utf-8"))["data"]
        fetter = json.loads((raw / f"{aid}_fetter.json").read_text(encoding="utf-8"))["data"]
        fpath = raw / f"{aid}_furniture.json"
        furniture = json.loads(fpath.read_text(encoding="utf-8"))["data"] if fpath.exists() else None
        char_dir = corpus / "avatar" / detail.get("name", str(aid))

        f = detail.get("fetter") or {}
        cv = f.get("cv") or {}
        prof = [
            f"# {detail.get('name')}（avatar {aid}）档案",
            "",
            f"- 来源：https://gi.yatta.moe/chs/archive/avatar/{aid}（检索日期 {TODAY}）",
            f"- 官方数据快照：`_corpus/raw/yatta/avatar/{aid}_detail.json`",
            "",
            "| 字段 | 内容 |",
            "|---|---|",
            f"| 称号 | {f.get('title')} |",
            f"| 简介 | {clean_text(f.get('detail'))} |",
            f"| 元素 | {detail.get('element')} |",
            f"| 武器 | {detail.get('weaponType')} |",
            f"| 所属/_native | {f.get('native')} |",
            f"| 命之座 | {f.get('constellation')} |",
            f"| 生日 | {'-'.join(map(str, detail.get('birthday') or []))} |",
            f"| CV | CHS {cv.get('CHS')} / JP {cv.get('JP')} / EN {cv.get('EN')} / KR {cv.get('KR')} |",
            f"| 实装时间 | {detail.get('release')}（Unix 秒） |",
            "",
        ]
        write_md(char_dir / "资料.md", "\n".join(prof))

        o = detail.get("other") or {}
        other = [f"# {detail.get('name')} 其他（服装/名片/特色料理）", ""]
        for c in o.get("costume") or []:
            other += [f"## 服装：{c.get('name')}", clean_text(c.get("description")), ""]
        nc = o.get("namecard")
        if nc:
            other += [f"## 名片：{nc.get('name')}", clean_text(nc.get("description")), ""]
        sf = o.get("specialFood")
        if sf:
            other += [f"## 特色料理：{sf.get('name')}（{sf.get('rank')} 星）", ""]
        write_md(char_dir / "其他.md", "\n".join(other))

        story = [f"# {detail.get('name')} 角色故事（avatarFetter.story）", "",
                 f"- 来源：https://gi.yatta.moe/chs/archive/avatar/{aid} 故事标签（检索日期 {TODAY}）", ""]
        for k, s in (fetter.get("story") or {}).items():
            story += [f"## {s.get('title')}", clean_text(s.get("text")), ""]
        write_md(char_dir / "故事.md", "\n".join(story))

        quotes = [f"# {detail.get('name')} 语音（文字版，共 {len(fetter.get('quotes') or {})} 条）", "",
                  f"- 来源：https://gi.yatta.moe/chs/archive/avatar/{aid} 语音标签（检索日期 {TODAY}）", ""]
        for k, q in (fetter.get("quotes") or {}).items():
            quotes += [f"## {q.get('title')}", clean_text(q.get("text")), ""]
            if q.get("tips"):
                quotes += [f"> 备注：{clean_text(q['tips'])}", ""]
        write_md(char_dir / "语音.md", "\n".join(quotes))

        if furniture:
            comp = [f"# {detail.get('name')} Companion（尘歌壶同伴对话）", "",
                    f"- 来源：https://gi.yatta.moe/chs/archive/avatar/{aid} Companion 标签（检索日期 {TODAY}）",
                    f"- 官方数据快照：`_corpus/raw/yatta/avatar/{aid}_furniture.json`",
                    f"- 家具 id：{furniture.get('id')}（{furniture.get('name')}）", ""]
            for k, s in (furniture.get("story") or {}).items():
                lines = []
                for td in s.get("taskData") or []:
                    render_task_data([td], lines)
                comp += [f"## 场景 {k}", *lines, ""]
            write_md(char_dir / "Companion对话.md", "\n".join(comp))
        print(f"[derive] avatar {aid} → {char_dir}/（资料/其他/故事/语音"
              + ("/Companion对话" if furniture else "") + ".md）")

    hits_path = corpus / "quest-hits.json"
    if args.quest_text or hits_path.exists():
        if not hits_path.exists():
            print("[derive] 无 quest-hits.json，跳过任务文本派生")
            return 0
        hits = json.loads(hits_path.read_text(encoding="utf-8"))
        qdir = corpus / "raw" / "yatta" / "quest"
        for n in names:
            hit = (hits.get("hits") or {}).get(n) or {}
            for qid in sorted(hit, key=int):
                src = qdir / f"{qid}.json"
                if not src.exists():
                    continue
                qdata = json.loads(src.read_text(encoding="utf-8"))
                meta = hit[qid]
                title = INVALID_FN.sub("_", meta.get("title") or str(qid))
                write_md(corpus / "quest_text" / n / f"{qid}_{title}.md",
                         _render_quest_md(qid, qdata, meta))
        print(f"[derive] 命中任务对话文本 → {corpus}/quest_text/<姓名>/")
    return 0


def main():
    ap = argparse.ArgumentParser(description="gi.yatta.moe（原神 Project Amber）语料抓取")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("avatar", help="抓取角色档案 + 故事语音 + Companion 原始 JSON")
    p.add_argument("avatar_id", type=int)
    p.add_argument("--out", default="examples/原神/_corpus")
    p.set_defaults(fn=cmd_avatar)

    p = sub.add_parser("avatar-all", help="批量抓取全部已实装角色语料（断点续跑）")
    p.add_argument("--out", default="examples/原神/_corpus")
    p.add_argument("--workers", type=int, default=6)
    p.set_defaults(fn=cmd_avatar_all)

    p = sub.add_parser("derive-all", help="从原始 JSON 派生全部角色 md 语料")
    p.add_argument("--corpus", default="examples/原神/_corpus")
    p.set_defaults(fn=cmd_derive_all)

    p = sub.add_parser("digest", help="从任务缓存切出全部角色登场语料（本人台词+上下文窗口）")
    p.add_argument("--corpus", default="examples/原神/_corpus")
    p.set_defaults(fn=cmd_digest)

    p = sub.add_parser("digest-traveler", help="旅行者专项：正主（role空=玩家）与反主（？？？/血亲/荧/空）分离切片")
    p.add_argument("--corpus", default="examples/原神/_corpus")
    p.set_defaults(fn=cmd_digest_traveler)

    p = sub.add_parser("quest-index", help="抓取全任务索引")
    p.add_argument("--out", default="examples/原神/_corpus")
    p.set_defaults(fn=cmd_quest_index)

    p = sub.add_parser("quest-scan", help="全量抓取任务对话并按姓名建命中索引（可断点续跑）")
    p.add_argument("--out", default="examples/原神/_corpus")
    p.add_argument("--names", default="奥黛塔,苏佩茜娃", help="逗号分隔的命中姓名")
    p.add_argument("--workers", type=int, default=8)
    p.set_defaults(fn=cmd_quest_scan)

    p = sub.add_parser("derive", help="从原始 JSON 派生 md 语料（角色各标签 + 命中任务全文）")
    p.add_argument("--corpus", default="examples/原神/_corpus")
    p.add_argument("--avatar", dest="avatar_id", type=int, default=None)
    p.add_argument("--names", default="奥黛塔,苏佩茜娃")
    p.add_argument("--no-quest-text", dest="quest_text", action="store_false")
    p.set_defaults(fn=cmd_derive, quest_text=True)

    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
