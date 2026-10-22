#!/usr/bin/env python3
"""CSP 第一层结构门槛检查（v2）。

检查项总清单以 references/checks.yaml 为单一事实源：
- 本脚本只执行 checks.yaml 中登记且本脚本已实现的条目；
- 登记了但未实现 → --self-report 标记 unimplemented（不假装通过）；
- 本脚本实现了但 checks.yaml 未登记 → 报漂移错误；
- checks.yaml 缺失或不可解析 → 直接报错退出（fail-closed，禁止静默跳过）。

用法：
  python scripts/quality_check.py <skill_directory_or_SKILL.md_path>
  python scripts/quality_check.py --self-report [skill_directory]
"""

import difflib
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECKS_YAML = ROOT / "references" / "checks.yaml"

BLACKLISTED_DOMAINS = ["zhihu.com", "weixin.qq.com", "baike.baidu.com"]

EXPRESSION_MARKERS = ["句式", "词汇", "语尾", "自称", "敬语", "节奏", "沉默", "口癖", "语气", "停顿", "情绪"]
MOTIVE_VERBS = ["想要", "为了", "因为", "渴望", "希望", "守护", "保护", "不愿", "不肯", "必须",
                "复仇", "证明", "恐惧", "害怕", "追求", "拯救", "赎", "梦", "执念", "答应",
                "让", "不再", "想"]
TEMPLATE_NOISE = re.compile(r"^(###|##|\*\*为什么\*\*\s*$|-{3,})")


# ----------------------------------------------------------------
# 零依赖 YAML 子集解析器（两级嵌套 + 列表 + 标量，够 checks.yaml 用）
# ----------------------------------------------------------------
def _strip_comment(line):
    out, in_q, i = [], None, 0
    while i < len(line):
        ch = line[i]
        if in_q:
            out.append(ch)
            if ch == in_q:
                in_q = None
        elif ch in "\"'":
            in_q = ch
            out.append(ch)
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out).rstrip()


def _parse_scalar(s):
    s = s.strip()
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        return [_parse_scalar(part) for part in inner.split(",")] if inner else []
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "~", ""):
        return None
    for cast in (int, float):
        try:
            return cast(s)
        except ValueError:
            pass
    return s


def parse_simple_yaml(text):
    root = {}
    # 每个栈条目: (indent, container, parent, key_in_parent)
    stack = [(-1, root, None, None)]
    for raw in text.splitlines():
        line = _strip_comment(raw)
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        content = line.strip()
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if content.startswith("- "):
            rest = content[2:].strip()
            if isinstance(parent, list):
                lst = parent
            else:
                # 首个列表项：把临时的空 dict 换成 list，保持 key 原缩进
                key_indent, _cont, gparent, gkey = stack.pop()
                lst = []
                gparent[gkey] = lst
                stack.append((key_indent, lst, gparent, gkey))
            if ":" in rest and not rest.startswith(("\"", "'")):
                k, v = rest.split(":", 1)
                item = {k.strip(): _parse_scalar(v)}
                lst.append(item)
                stack.append((indent, item, lst, None))
            else:
                lst.append(_parse_scalar(rest))
        elif ":" in content:
            k, v = content.split(":", 1)
            k, v = k.strip(), v.strip()
            if v == "":
                child = {}
                parent[k] = child
                stack.append((indent, child, parent, k))
            else:
                parent[k] = _parse_scalar(v)
        # 纯标量行不在本子集内，忽略
    return root


def load_checks_yaml():
    if not CHECKS_YAML.exists():
        print(f"FATAL: checks.yaml 缺失: {CHECKS_YAML}\n"
              "检查项总清单是单一事实源，缺它即 fail-closed，禁止静默跳过检查。")
        sys.exit(2)
    try:
        data = parse_simple_yaml(CHECKS_YAML.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"FATAL: checks.yaml 无法解析: {exc}")
        sys.exit(2)
    if not isinstance(data.get("structural_checks"), list):
        print("FATAL: checks.yaml 缺少 structural_checks 清单")
        sys.exit(2)
    return data


# ----------------------------------------------------------------
# 基础工具
# ----------------------------------------------------------------
def read_text(path):
    return path.read_text(encoding="utf-8") if path.exists() else ""


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def resolve_paths(target):
    path = Path(target)
    if path.is_dir():
        return path, path / "SKILL.md"
    return path.parent, path


def extract_section(content, title_regex):
    """返回 (section_body or None)。body 从标题下一行到下一个 ## 前。"""
    match = re.search(rf"^##\s+.*(?:{title_regex}).*$", content, re.MULTILINE)
    if not match:
        return None
    rest = content[match.end():]
    nxt = re.search(r"^##\s+", rest, re.MULTILINE)
    return rest[:nxt.start()] if nxt else rest


def subsections(body):
    """把 ### 子标题段切成 [(title, text)]。"""
    parts = re.split(r"^###\s+", body, flags=re.MULTILINE)
    out = []
    for part in parts[1:]:
        lines = part.split("\n", 1)
        title = lines[0].strip()
        out.append((title, lines[1] if len(lines) > 1 else ""))
    return out


def md_files(skill_dir):
    files = [skill_dir / "SKILL.md"]
    refs = skill_dir / "references"
    if refs.exists():
        files += sorted(p for p in refs.rglob("*.md"))
    return [p for p in files if p.exists()]


# ----------------------------------------------------------------
# 检查实现（函数名 = checks.yaml 里的 id）
# ----------------------------------------------------------------
def _floor(check_id, default):
    """数量下限单一事实源：checks.yaml structural_checks.<id>.threshold。
    yaml 缺失/不可解析时 load_checks_yaml 会 fail-closed 退出，这里只做兜底默认。"""
    try:
        for c in load_checks_yaml().get("structural_checks", []):
            if c.get("id") == check_id:
                return int(float(c.get("threshold", default)))
    except (TypeError, ValueError, AttributeError):
        pass
    return default


NON_PATTERN_HEADING = re.compile(
    r"^(?:(?:核心|内在)(?:矛盾|张力|冲突)|"
    r"(?:矛盾|张力|冲突)(?=$|[一二三四五六七八九十\d\s：:（(—-])|低置信度)"
)


def behavior_patterns(body):
    """Use the same rule set for counts, triples and per-rule examples."""
    return [(title, text) for title, text in subsections(body or "")
            if not NON_PATTERN_HEADING.search(title.strip())]


def check_behavior_patterns_min(content):
    body = extract_section(content, r"行为动态|行为模式")
    count = len(behavior_patterns(body))
    floor = _floor("behavior_patterns_min", 3)
    passed = count >= floor
    return passed, f"行为模式 {count} 条（下限 {floor}，不设上限）"


def check_behavior_patterns_triplet(content):
    body = extract_section(content, r"行为动态|行为模式")
    if not body:
        return False, "FAIL: 无行为动态章节"
    patterns = behavior_patterns(body)
    missing = [t for t, x in patterns if not re.search(r"为什么|为何", x)]
    passed = bool(patterns) and not missing
    detail = f"{len(patterns)} 条模式，缺「为什么」: {missing or '无'}"
    return passed, detail


def check_expression_texture_min(content):
    body = extract_section(content, r"表达质感")
    found = sum(1 for m in EXPRESSION_MARKERS if m in content)
    floor = _floor("expression_texture_min", 4)
    passed = body is not None and found >= floor
    return passed, f"表达质感章节:{body is not None}, 标记 {found} 类（下限 {floor}）"


def check_behavior_examples_min(content):
    examples = re.findall(r"###\s+场景[一二三四五六七八九十\d]", content)
    if not examples:
        examples = re.findall(r"场景[一二三四五六七八九十\d]", content)
    count = len(examples)
    floor = _floor("behavior_examples_min", 20)
    if count < floor:
        return False, f"行为示例 {count} 个（下限 {floor}）"
    # 每条行为模式 >= 3 例（2026-09-07 用户命令）：
    # 每个示例必须带「> 规则：模式名」归属行；按归属行逐模式计数（外推与复现例同权）。
    bm = re.search(r"##\s*行为动态(.*?)(?=\n##\s|\Z)", content, re.DOTALL)
    if not bm:
        return False, "FAIL: 无行为动态章节，无法核对每模式例数"
    subs = behavior_patterns(bm.group(1))
    rule_count = len(subs)
    rule_names = [t for t, _ in subs]
    per_rule = {t: 0 for t in rule_names}
    unattributed = []
    for m in re.finditer(r"###\s+(场景[^\n]*)\n(.*?)(?=\n###\s|\Z)", content, re.DOTALL):
        am = re.search(r"^>\s*规则[：:]\s*(.+)$", m.group(2), re.M)
        if not am:
            unattributed.append(m.group(1).strip())
            continue
        target = am.group(1).strip()
        hit = next((r for r in rule_names if r in target or target in r), None)
        if hit is None:
            unattributed.append(f"{m.group(1).strip()}（规则「{target}」不存在）")
        else:
            per_rule[hit] += 1
    if unattributed:
        return False, "以下示例缺「> 规则：」归属行或归属到不存在的规则: " + "；".join(unattributed[:5])
    starved = {r: n for r, n in per_rule.items() if n < 3}
    if starved:
        return False, (f"以下规则例数不足 3（每模式须 >=3 例）: {starved}"
                       "（见 checks.yaml behavior_examples_min，2026-09-07 用户命令）")
    return True, (f"行为示例 {count} 个（下限 {floor}；模式 {rule_count} 条 × >=3 例，"
                  f"分布 {per_rule}）")


def check_honesty_boundary_min(content):
    # 修复：旧正则 `##\s+.*诚实边界` 的贪婪跨行匹配会吞掉整个文档，
    # 导致条目数取决于尾部措辞。限定在标题行内匹配，捕获到下一个 ## 标题为止。
    match = re.search(r"^##[ \t]+[^\n]*(?:诚实边界|局限)[^\n]*\n(.*?)(?=^##[ \t]|\Z)",
                      content, re.DOTALL | re.MULTILINE)
    if not match:
        return False, "FAIL: 无诚实边界章节"
    count = len(re.findall(r"^[-*]\s+", match.group(1), re.MULTILINE))
    floor = _floor("honesty_boundary_min", 3)
    return count >= floor, f"诚实边界 {count} 条（下限 {floor}）"


def check_contradictions_preserved(content):
    count = len(re.findall(r"矛盾|张力|冲突|一方面.*另一方面|既.*又|虽然.*但是", content))
    return count >= 1, f"矛盾保留 {count} 处（下限 1）"


def check_role_play_rules(content):
    has_rules = "角色扮演规则" in content
    has_exit = bool(re.search(r"退出|exit", content, re.IGNORECASE))
    has_first_person = bool(re.search(r"用「我」|第一人称|直接以", content))
    passed = has_rules and has_exit and has_first_person
    return passed, f"rules:{has_rules}, exit:{has_exit}, 1st-person:{has_first_person}"


def check_source_attribution(content, skill_dir):
    has_source_section = bool(re.search(r"调研来源|来源|source", content, re.IGNORECASE))
    has_sources_json = (skill_dir / "references" / "sources.json").exists()
    passed = has_source_section or has_sources_json
    return passed, f"source section:{has_source_section}, sources.json:{has_sources_json}"


MANIFEST_REQUIRED = ["schema_version", "name", "character", "work", "generated_at",
                     "research_completed_at", "latest_source_checked_at", "covered_until",
                     "source_count", "csp_version", "evidence_density"]
EVIDENCE_DENSITY_LEVELS = {"single_source", "sparse", "normal"}


def check_manifest_fields(skill_dir):
    data = load_json(skill_dir / "manifest.json", {})
    missing = [key for key in MANIFEST_REQUIRED if data.get(key) in (None, "")]
    density = data.get("evidence_density")
    if density and density not in EVIDENCE_DENSITY_LEVELS:
        missing.append(f"evidence_density 非法值:{density}")
    return not missing, f"manifest 缺失/非法: {missing or '无'}"


def source_records(skill_dir):
    data = load_json(skill_dir / "references" / "sources.json", [])
    if isinstance(data, dict) and "records" in data:
        return data["records"]
    return data if isinstance(data, list) else []


def check_sources_json_records(skill_dir):
    path = skill_dir / "references" / "sources.json"
    if not path.exists():
        return False, "FAIL: 缺 references/sources.json"
    records = source_records(skill_dir)
    if not records:
        return False, "FAIL: sources.json 无记录"
    missing_dates = [r.get("id", r.get("source", "?")) for r in records if not r.get("retrieved_at")]
    blacklisted = [r.get("url", "") for r in records
                   if any(d in (r.get("url") or "").lower() for d in BLACKLISTED_DOMAINS)
                   and r.get("source_tier") != "excluded"]
    passed = not missing_dates and not blacklisted
    return passed, f"records:{len(records)}, 缺日期:{len(missing_dates)}, 黑名单:{len(blacklisted)}"


def check_time_boundary_section(content):
    has_section = "资料时间边界" in content or "时间边界" in content
    has_update = "我的资料更新至" in content and "这可能会消耗一些 Token" in content
    has_date = bool(re.search(r"20\d{2}-\d{2}-\d{2}", content))
    passed = has_section and has_update and has_date
    return passed, f"section:{has_section}, update response:{has_update}, date:{has_date}"


def check_research_files(skill_dir):
    research = skill_dir / "references" / "research"
    required = ["01-setting.md", "02-personality.md", "03-expression.md",
                "04-relationships.md", "05-key-scenes.md"]
    missing = [n for n in required if not (research / n).exists()]
    return not missing, f"missing: {missing or '无'}"


# ---- 加固检查 ----
VARIABLE_LEAK = re.compile(r"\{[@A-Za-z_][A-Za-z0-9_.]*\}")


def check_variable_leak(skill_dir):
    leaks = []
    for path in md_files(skill_dir):
        for i, line in enumerate(read_text(path).split("\n"), 1):
            if VARIABLE_LEAK.search(line):
                leaks.append(f"{path.name}:{i}")
    return not leaks, f"变量泄漏 {len(leaks)} 处 {leaks[:5]}"


PLACEHOLDER_LEAD = re.compile(r"(以下|如下|包括|分别是|具体)[^。！？\n]*[：:]\s*$")


def check_empty_placeholder(skill_dir):
    bad = []
    for path in md_files(skill_dir):
        lines = read_text(path).split("\n")
        for i, line in enumerate(lines):
            if PLACEHOLDER_LEAD.search(line):
                nxt = next((l for l in lines[i + 1:] if l.strip()), "")
                if not nxt.strip() or nxt.lstrip().startswith(("#", "##")):
                    bad.append(f"{path.name}:{i + 1}")
    return not bad, f"空占位段 {len(bad)} 处 {bad[:5]}"


def check_semantic_motive_check(content):
    body = extract_section(content, r"运行核心")
    if not body:
        return False, "FAIL: 无运行核心章节（核心动机所在）"
    m = re.search(r"核心动机[^：:\n]*[：:]\s*(.+)", body)
    text = m.group(1).strip() if m else ""
    if not text:
        # 兜底：抓「核心动机」后第一段非空文本
        m2 = re.search(r"核心动机.*?\n+([^#\n][^\n]+)", body, re.DOTALL)
        text = m2.group(1).strip() if m2 else ""
    like_motive = len(text) >= 20 and any(v in text for v in MOTIVE_VERBS)
    return like_motive, f"核心动机 {'PASS' if like_motive else '不像动机（过短或无行动缘由）'}: {text[:40]}"


def check_csp_version_actual(skill_dir):
    data = load_json(skill_dir / "manifest.json", {})
    csp = data.get("csp_version")
    schema = str(data.get("schema_version", ""))
    if not csp or str(csp).lower() == "unknown":
        return False, f"FAIL: csp_version={csp!r}"
    if not re.match(r"^\d+\.\d+", str(csp)):
        return False, f"FAIL: csp_version 非版本号: {csp!r}"
    same_major = csp.split(".")[0] == schema.split(".")[0]
    return same_major, f"csp_version={csp}, schema={schema}, 主版本一致:{same_major}"


RUNTIME_REF = re.compile(r"(详见|参见|阅读|读取|见)\s*references/|references/\S+\.(md|json)")


def check_self_contained(content):
    hits = RUNTIME_REF.findall(content)
    return not hits, f"运行时依赖引用 {len(hits)} 处"


def _similarity_scope(content):
    parts = [extract_section(content, r"行为动态|行为模式") or "",
             extract_section(content, r"表达质感") or ""]
    lines = []
    for part in parts:
        for line in part.split("\n"):
            line = line.strip()
            if len(line) >= 6 and not TEMPLATE_NOISE.match(line):
                lines.append(line)
    return "\n".join(lines)


def _normalize(text):
    strip_chars = " \t\r\n#*-|>「」『』【】（）()：:、，,。.！!？?…·—\"'“”‘’"
    return text.translate({ord(c): None for c in strip_chars})


def _trigrams(text):
    return {text[i:i + 3] for i in range(len(text) - 2)} if len(text) >= 3 else {text}


def find_other_skills(target_dir):
    out = []
    target = target_dir.resolve()
    for path in ROOT.rglob("SKILL.md"):
        if target in path.parents or path.parent == target:
            continue
        parts = {p.name for p in path.parents} | {path.parent.name}
        # CSP = 游戏层 SOP 副本（非角色 skill），不进相似度基线
        if parts & {"backups", "runs", "__pycache__", ".git", "CSP"}:
            continue
        out.append(path)
    return out


def _similarity_threshold():
    """相似度阈值单一事实源：checks.yaml cross_skill_similarity.threshold。
    yaml 缺失/不可解析时 load_checks_yaml 会 fail-closed 退出，这里只做兜底默认。"""
    try:
        for c in load_checks_yaml().get("structural_checks", []):
            if c.get("id") == "cross_skill_similarity":
                return float(c.get("threshold", 0.2))
    except (TypeError, ValueError, AttributeError):
        pass
    return 0.2


def check_cross_skill_similarity(content, skill_dir):
    """双信号相似度：字符 3-gram Jaccard（抓复制）+ difflib 比率（抓改写）。
    阈值双侧校准（阴性 max=0.164 / 阳性三级样本），超标进 needs_human_review 不自动 FAIL。"""
    threshold = _similarity_threshold()
    mine = _normalize(_similarity_scope(content))
    if len(mine) < 50:
        return {"pass": True, "detail": "比对范围过小，跳过", "review": []}
    my_set = _trigrams(mine)
    review = []
    best = ("", 0.0)
    for other in find_other_skills(skill_dir):
        theirs = _normalize(_similarity_scope(read_text(other)))
        if len(theirs) < 50:
            continue
        jaccard = len(my_set & _trigrams(theirs)) / max(1, len(my_set | _trigrams(theirs)))
        ratio = difflib.SequenceMatcher(None, mine[:4000], theirs[:4000]).ratio()
        combined = max(jaccard, ratio)
        if combined > best[1]:
            best = (other.parent.name, combined)
        if combined > threshold:
            review.append({"against": str(other.relative_to(ROOT)), "score": round(combined, 3)})
    detail = (f"最高相似度 {best[1]:.3f}（vs {best[0]}）；阈值 {threshold} 双侧校准"
              f"（阴性 29554 对 max=0.164；阳性对照改名照抄 0.960/轻度改写 0.763/深度改写 0.466），超标仅人工复核")
    return {"pass": True, "detail": detail, "review": review}


# ----------------------------------------------------------------
# 二级例子门禁（选例三标准的机器部分 + 锚点核对）
# ----------------------------------------------------------------
EXAMPLE_HEAD = re.compile(r"^###\s+(场景[一二三四五六七八九十\d][^\n]*)$", re.M)


def _example_blocks(content):
    """返回 [(标题, 块正文)]；块正文 = 标题行到下一个 ### 之间。"""
    heads = list(EXAMPLE_HEAD.finditer(content))
    blocks = []
    for i, m in enumerate(heads):
        nxt = heads[i + 1].start() if i + 1 < len(heads) else len(content)
        blocks.append((m.group(1).strip(), content[m.end():nxt]))
    return blocks


def _signature_terms(content):
    """专属词表：表达质感段的「词汇场」「自称与称呼」条目（多收无害，只用于命中判断）。"""
    terms = set()
    tex = extract_section(content, r"表达质感") or ""
    for line in tex.split("\n"):
        if not ("词汇场" in line or "称呼" in line):
            continue
        for piece in re.split(r"[、，,；;（）()：:\s]", re.sub(r"[「」『』*>]", "", line)):
            piece = piece.strip("。~—·")
            if 2 <= len(piece) <= 12 and "：" not in piece and "**" not in piece:
                terms.add(piece)
        for q in re.findall(r"「([^」]+)」", line):
            if 2 <= len(q) <= 8:
                terms.add(q.strip())
    return terms


def check_example_discriminability(content):
    """选例三标准的机器部分（①专属名词 ②原话引用）：每个行为示例至少命中其一。
    ③换人不成立机器查不了，留给行为测试 judge 与人工复核。"""
    blocks = _example_blocks(content)
    if not blocks:
        return False, "未找到任何 ### 场景X 行为示例"
    terms = _signature_terms(content)
    fails = []
    for title, body in blocks:
        has_quote = bool(re.search(r"[「『][^」』]*我[^」』]*[」』]", body))
        hit = sorted(t for t in terms if t in body)
        if not has_quote and not hit:
            fails.append(f"{title}（无原话引用、未命中专属词表）")
    if fails:
        return False, "通用场面充数嫌疑：" + "；".join(fails)
    return True, f"{len(blocks)} 个例子均含原话引用或专属词命中（词表 {len(terms)} 词）"



UNKNOWN_TRIGGER = re.compile(
    r"不知道|不明|涂黑|没有告诉我|不在场|没有全|未核|不可知|未经历|没见过|从未|没听过|无法确认|说不上|记不清")



def check_scene_source_spread(content, skill_dir):
    """v2.2 取证选取策略：证据场景按来源篇目均匀分布，禁止堆料。
    - scenes>=8 且 story 来源>=3 篇：单一来源占比不得超过 60%，来源不得少于 3 篇；
    - story 来源==2：单一来源不得超过 75%；
    - story 来源<=1：豁免（档案/单篇主导属证据本性，如无剧情发言角色），pass 加注。"""
    import json as _json
    ksp = skill_dir / "references" / "research" / "05-key-scenes.md"
    if not ksp.exists():
        return False, "缺 research/05-key-scenes.md"
    text = read_text(ksp)
    scenes = len(re.findall(r"^## 场景", text, re.M))
    if scenes < 8:
        return False, f"场景数 {scenes}（<8），不足以评估分布"
    from collections import Counter
    srcs = Counter(re.findall(r"来源[：:]\s*(\S+)", text))
    # story 记录数来自 sources.json（media_type 含 story）
    story_n = 0
    sj = skill_dir / "references" / "sources.json"
    if sj.exists():
        try:
            data = _json.loads(sj.read_text(encoding="utf-8"))
            recs = data.get("records", []) if isinstance(data, dict) else data
            story_n = sum(1 for r in recs if "story" in str(r.get("media_type", "")) and r.get("status") == "ok")
        except Exception:
            pass
    top_src, top_n = (srcs.most_common(1)[0] if srcs else ("—", 0))
    pct = top_n / scenes * 100 if scenes else 100
    if story_n <= 1:
        return True, f"豁免：story 来源 {story_n} 篇（证据本性，{top_src} {pct:.0f}%）"
    limit = 60 if story_n >= 3 else 75
    ok = pct <= limit and (story_n < 3 or len(srcs) >= 3)
    if ok:
        return True, f"{len(srcs)} 篇来源，最高 {top_src} {pct:.0f}%（≤{limit}%）"
    return False, f"堆料嫌疑：{top_src} 占 {pct:.0f}%（>{limit}%，story 来源 {story_n} 篇）；跨篇目均匀重采样"


def check_pattern_example_coverage(content):
    """v2.2：每条行为模式 >=3 例，以 > 规则：模式名 双标注统计；标注必须指向真实规则标题。"""
    bm = extract_section(content, r"行为动态|行为模式")
    rule_heads = [h.strip() for h, _ in behavior_patterns(bm)]
    blocks = _example_blocks(content)
    if not rule_heads or not blocks:
        return False, "无行为动态规则或无行为示例"
    counts = {h: 0 for h in rule_heads}
    problems = []
    for title, body in blocks:
        m = re.search(r"^>\s*规则[：:]\s*(.+)$", body, re.M)
        if not m:
            problems.append(f"{title} 缺 规则： 行")
            continue
        target = m.group(1).strip()
        hit = next((h for h in rule_heads if target in h or h in target), None)
        if hit is None:
            problems.append(f"{title} 规则「{target}」不存在")
        else:
            counts[hit] += 1
    under = [h for h, c in counts.items() if c < 3]
    if problems or under:
        return False, "标注问题: %s；例数<3: %s" % (
            "；".join(problems[:3]) or "无", "；".join(under[:5]) or "无")
    return True, "%d 条模式均 >=3 例（共 %d 例双标注）" % (len(rule_heads), len(blocks))


def check_unknown_list_triggers(content):
    """v2.2：「我所不知」必须是含触发词的列表项（知识陷阱题从这里抽取）。"""
    body = extract_section(content, r"知识边界|诚实局限|诚实边界")
    if not body:
        return False, "FAIL: 无知识边界章节"
    m = re.search(r"###\s*我所不知(.*?)(?=\n###|\Z)", body, re.S)
    if not m:
        return False, "缺「我所不知」小节"
    bullets = re.findall(r"^[-*]\s+(.+)$", m.group(1), re.M)
    if len(bullets) < 3:
        return False, "我所不知列表项 %d（下限 3）" % len(bullets)
    hits = [b for b in bullets if UNKNOWN_TRIGGER.search(b)]
    if len(hits) < 3:
        return False, "含触发词的列表项 %d/3（触发词：不知道/不明/不在场/未经历/没见过…）" % len(hits)
    return True, "%d 项（%d 项含触发词）" % (len(bullets), len(hits))


def check_example_anchor(content, skill_dir):
    """每个行为示例必须有锚点行且指向真实存在的证据：
    > 锚点：05-key-scenes.md#场景标题（复现例）或 > 锚点：行为动态·规则名（外推例）"""
    blocks = _example_blocks(content)
    if not blocks:
        return False, "未找到任何 ### 场景X 行为示例"
    bm = re.search(r"##\s*行为动态(.*?)(?=\n##\s|\Z)", content, re.DOTALL)
    rule_heads = [h.strip() for h in re.findall(r"^###\s+(.+)$", bm.group(1), re.M)] if bm else []
    ksp = skill_dir / "references" / "research" / "05-key-scenes.md"
    scene_heads = []
    if ksp.exists():
        scene_heads = [h.strip() for h in re.findall(r"^##\s+(.+)$", read_text(ksp), re.M)]
    problems = []
    for title, body in blocks:
        am = re.search(r"^>\s*锚点[：:]\s*(.+)$", body, re.M)
        if not am:
            problems.append(f"{title} 缺锚点行")
            continue
        a = am.group(1).strip()
        if a.startswith("行为动态"):
            target = a.split("·", 1)[-1].strip()
            if not any(target in h for h in rule_heads):
                problems.append(f"{title} 锚点「{target}」在行为动态规则标题中不存在")
        elif "05-key-scenes" in a:
            target = a.split("#", 1)[-1].strip()
            if not scene_heads:
                problems.append(f"{title} 锚点指向 05-key-scenes.md 但文件缺失或无场景标题")
            elif not any(target in h for h in scene_heads):
                problems.append(f"{title} 锚点「{target}」在 05-key-scenes.md 场景标题中不存在")
        else:
            problems.append(f"{title} 锚点格式无法解析：{a}")
    if problems:
        return False, "；".join(problems)
    return True, f"{len(blocks)} 个例子锚点齐全且指向真实存在的证据"


# ----------------------------------------------------------------
# 检查注册表：id → 实现（与 checks.yaml structural_checks 对账）
# ----------------------------------------------------------------
def build_registry(content, skill_dir):
    return {
        "behavior_patterns_min": lambda: check_behavior_patterns_min(content),
        "behavior_patterns_triplet": lambda: check_behavior_patterns_triplet(content),
        "expression_texture_min": lambda: check_expression_texture_min(content),
        "behavior_examples_min": lambda: check_behavior_examples_min(content),
        "example_discriminability": lambda: check_example_discriminability(content),
        "example_anchor_check": lambda: check_example_anchor(content, skill_dir),
        "pattern_example_coverage": lambda: check_pattern_example_coverage(content),
        "unknown_list_triggers": lambda: check_unknown_list_triggers(content),
        "scene_source_spread": lambda: check_scene_source_spread(content, skill_dir),
        "scene_source_spread": lambda: check_scene_source_spread(skill_dir),
        "honesty_boundary_min": lambda: check_honesty_boundary_min(content),
        "contradictions_preserved": lambda: check_contradictions_preserved(content),
        "role_play_rules": lambda: check_role_play_rules(content),
        "source_attribution": lambda: check_source_attribution(content, skill_dir),
        "manifest_fields": lambda: check_manifest_fields(skill_dir),
        "sources_json_records": lambda: check_sources_json_records(skill_dir),
        "time_boundary_section": lambda: check_time_boundary_section(content),
        "research_files": lambda: check_research_files(skill_dir),
        "variable_leak": lambda: check_variable_leak(skill_dir),
        "empty_placeholder": lambda: check_empty_placeholder(skill_dir),
        "semantic_motive_check": lambda: check_semantic_motive_check(content),
        "csp_version_actual": lambda: check_csp_version_actual(skill_dir),
        "self_contained": lambda: check_self_contained(content),
        "cross_skill_similarity": lambda: check_cross_skill_similarity(content, skill_dir),
    }


def check_scene_source_spread(skill_dir):
    """v2.3（2026-09-08 用户命令）：关键场景来源桶 >=4 且任一桶占比 <=60%——
    剧情按重要程度均匀选取，不得把场景堆在同一个版本的剧情里。"""
    ks = skill_dir / "references" / "research" / "05-key-scenes.md"
    if not ks.exists():
        return False, "FAIL: 无 05-key-scenes.md"
    text = read_text(ks)
    buckets = {}
    heads = list(re.finditer(r"^##\s+场景[^\n]*$", text, re.M))
    if not heads:
        return False, "FAIL: 未解析到场景标题"
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        body = text[m.end():end]
        sm = re.search(r"来源[：:]\s*([^\n]+)", body)
        if not sm:
            continue
        src = sm.group(1)
        ids = re.findall(r"\d{3,6}", src)
        key = ("任务" + ids[0]) if ids else (src.split("·")[0].strip()[:12] or "其他")
        buckets[key] = buckets.get(key, 0) + 1
    total = sum(buckets.values())
    if total == 0:
        return False, "FAIL: 场景均未标注来源"
    if len(buckets) < 4:
        return False, f"来源桶仅 {len(buckets)} 个（须 >=4）: {buckets}"
    top_key, top = max(buckets.items(), key=lambda kv: kv[1])
    if top / total > 0.6:
        return False, f"最大来源桶「{top_key}」占比 {top}/{total} 超 60%: {buckets}"
    return True, (f"来源桶 {len(buckets)} 个（{total} 场景），"
                  f"最大「{top_key}」{top}/{total}（<=60%）")


# ----------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------
def run_checks(skill_dir, skill_path, checks_data):
    content = read_text(skill_path)
    registry = build_registry(content, skill_dir)
    yaml_ids = [c["id"] for c in checks_data["structural_checks"]]
    unregistered = [fid for fid in registry if fid not in yaml_ids]
    if unregistered:
        print(f"FATAL: 以下检查已实现但未在 checks.yaml 登记（漂移）: {unregistered}")
        sys.exit(2)

    results, review = {}, []
    unimplemented = []
    for entry in checks_data["structural_checks"]:
        cid, desc = entry["id"], entry.get("description", "")
        fn = registry.get(cid)
        if fn is None:
            unimplemented.append(cid)
            results[cid] = {"status": "unimplemented", "detail": desc}
            print(f"  {cid:<28} {'----':<6} 未实现（--self-report 可见）")
            continue
        out = fn()
        if isinstance(out, dict):  # review 型（相似度）
            results[cid] = {"status": "pass", "detail": out["detail"]}
            if out.get("review"):
                review += out["review"]
                print(f"  {cid:<28} {'REVIEW':<6} {out['detail']}")
                for item in out["review"]:
                    print(f"      - {item['against']} 相似度 {item['score']}")
            else:
                print(f"  {cid:<28} {'PASS':<6} {out['detail']}")
            continue
        passed, detail = out
        results[cid] = {"status": "pass" if passed else "fail", "detail": detail}
        print(f"  {cid:<28} {'PASS' if passed else 'FAIL':<6} {detail}")

    hard_fails = [cid for cid, r in results.items() if r["status"] == "fail"]
    return results, review, unimplemented, hard_fails


def write_report(skill_dir, checks_data, results, review, unimplemented, hard_fails):
    report_dir = skill_dir / "references"
    if not report_dir.exists():
        return
    executed = [r for r in results.values() if r["status"] != "unimplemented"]
    passed_count = sum(1 for r in executed if r["status"] == "pass")
    report = {
        "checked_at": date.today().isoformat(),
        "checks_yaml_schema": checks_data.get("schema_version"),
        "score": round(passed_count / len(executed), 3) if executed else 0,
        "passed": not hard_fails and not unimplemented,
        "skill_sha256": hashlib.sha256((skill_dir / "SKILL.md").read_bytes()).hexdigest(),
        "checks": results,
        "needs_human_review": review,
        "unimplemented": unimplemented,
        "failed_checks": hard_fails,
    }
    (report_dir / "quality-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def self_report(skill_dir):
    checks_data = load_checks_yaml()
    registry_ids = None
    if skill_dir:
        target = Path(skill_dir)
        sdir, spath = resolve_paths(target)
        if spath.exists():
            registry_ids = build_registry(read_text(spath), sdir).keys()
    print(f"checks.yaml schema: {checks_data.get('schema_version')}  （单一事实源）")
    print(f"{'id':<28} {'状态':<14} 说明")
    print("-" * 88)
    for entry in checks_data["structural_checks"]:
        cid, desc = entry["id"], entry.get("description", "")
        if registry_ids is not None:
            status = "implemented" if cid in registry_ids else "UNIMPLEMENTED"
        else:
            status = "registered"
        print(f"{cid:<28} {status:<14} {desc}")
    for section, keys in (("behavior_test", checks_data.get("behavior_test", {})),
                          ("file_manager", checks_data.get("file_manager", {}))):
        print(f"\n[{section}]")
        for k, v in keys.items():
            print(f"  {k}: {v}")
    if registry_ids is not None:
        drift = [fid for fid in registry_ids if fid not in {c['id'] for c in checks_data['structural_checks']}]
        if drift:
            print(f"\n漂移：脚本存在但 checks.yaml 未登记: {drift}")
            sys.exit(2)
    return 0


def main():
    args = sys.argv[1:]
    if "--self-report" in args:
        target = args[args.index("--self-report") + 1] if len(args) > args.index("--self-report") + 1 else None
        sys.exit(self_report(target))
    if len(args) < 1:
        print("Usage: python quality_check.py <skill_directory_or_SKILL.md_path>\n"
              "       python quality_check.py --self-report [skill_directory]")
        sys.exit(1)

    skill_dir, skill_path = resolve_paths(args[0])
    if not skill_path.exists():
        print(f"FAIL: SKILL.md not found: {skill_path}")
        sys.exit(1)

    checks_data = load_checks_yaml()
    print(f"Quality Check (v2, checks.yaml {checks_data.get('schema_version')}): {skill_path}")
    print("=" * 88)
    results, review, unimplemented, hard_fails = run_checks(skill_dir, skill_path, checks_data)
    print("=" * 88)
    executed = [r for r in results.values() if r["status"] != "unimplemented"]
    passed_count = sum(1 for r in executed if r["status"] == "pass")
    print(f"Result: {passed_count}/{len(executed)} passed"
          + (f"，{len(unimplemented)} 项未实现" if unimplemented else "")
          + (f"，{len(review)} 项需人工复核" if review else ""))
    write_report(skill_dir, checks_data, results, review, unimplemented, hard_fails)
    if hard_fails:
        print("Failures found — iterate before delivery.")
    elif unimplemented:
        print("结构检查无硬失败，但存在未实现检查项 — 不能声明全过。")
    else:
        print("结构门槛全过（相似度复核项另见报告）—— 进入第二层行为测试。")
    sys.exit(1 if hard_fails or unimplemented else 0)


if __name__ == "__main__":
    main()
