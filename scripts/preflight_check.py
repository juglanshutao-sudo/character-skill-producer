#!/usr/bin/env python3
"""CSP preflight 对账：文档承诺 / checks.yaml 登记 / 脚本实现 三方一致性核对。

规则（见 references/checks.yaml 的 preflight 段）：
  - checks.yaml 登记的每个结构检查必须在 quality_check.py 有实现；
  - quality_check.py 的每个实现必须在 checks.yaml 登记；
  - doc_section_map 引用的 id 必须已在 checks.yaml 登记；
  - behavior_test / file_manager 的承诺必须有对应命令；
  - 模板标题对账：skill-template.md 的 ## 标题必须能被 quality_check / behavior_test
    的章节正则与 MUTATIONS 模式命中；MUTATIONS 替换文本引入的 ## 标题必须在模板集合内；
  - 模板正文门禁：skill-template.md 围栏内正文必须通过自己的
    variable_leak / empty_placeholder / self_contained 检查（模板出厂即合格）。

用法：python scripts/preflight_check.py
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = ROOT / "standard" / "skill-template.md"
if not TEMPLATE_PATH.exists():
    TEMPLATE_PATH = ROOT / "references" / "skill-template.md"
sys.path.insert(0, str(ROOT / "scripts"))

from quality_check import load_checks_yaml, build_registry  # noqa: E402


def get_template_headings():
    tpl = TEMPLATE_PATH
    if not tpl.exists():
        return None
    text = tpl.read_text(encoding="utf-8")
    return set(re.findall(r"^## (.+)$", text, re.MULTILINE))


def _hits_any_heading(pattern, headings):
    """用与 run_mutation 相同的 re.DOTALL 语义，在由模板标题拼成的合成文档上测试模式。"""
    doc = "\n".join(f"## {h}\n[段落占位内容]\n" for h in sorted(headings))
    return re.search(pattern, doc, re.DOTALL) is not None


def check_template_reconciliation():
    """模板标题 ↔ 脚本章节正则 / MUTATIONS 模式对账，返回问题列表。"""
    headings = get_template_headings()
    if headings is None:
        return [f"缺少 {TEMPLATE_PATH}，无法做模板标题对账"]
    problems = []

    # (a) extract_section 的章节正则必须能命中模板至少一个 ## 标题
    section_regexes = set()
    for src in ("quality_check.py", "behavior_test.py"):
        text = (ROOT / "scripts" / src).read_text(encoding="utf-8")
        section_regexes.update(
            re.findall(r'extract_section\([^,]+,\s*r"([^"]+)"', text))
    for pat in sorted(section_regexes):
        if not _hits_any_heading(pat, headings):
            problems.append(f"章节正则 r\"{pat}\" 命中不了模板任何 ## 标题")

    import behavior_test
    # (b) MUTATIONS 匹配模式必须能命中模板标题（否则突变静默无效）
    for name, subs in behavior_test.MUTATIONS:
        for pat, _repl in subs:
            if not _hits_any_heading(pat, headings):
                problems.append(f"突变 {name} 的模式 r\"{pat}\" 命中不了模板标题")
    # (c) MUTATIONS 替换文本引入的 ## 标题必须在模板标题集合内（突变体仍须符合模板）
    for name, subs in behavior_test.MUTATIONS:
        for _pat, repl in subs:
            for h in re.findall(r"^## (.+)$", repl, re.MULTILINE):
                if h not in headings:
                    problems.append(f"突变 {name} 替换文本引入未登记标题「{h}」")
    return problems


def get_template_body():
    tpl = TEMPLATE_PATH
    if not tpl.exists():
        return None
    text = tpl.read_text(encoding="utf-8")
    m = re.search(r"```markdown\n(.*?)\n```", text, re.DOTALL)
    return m.group(1) if m else None


def check_template_body_gates():
    """模板正文出厂门禁：围栏内正文必须通过自己的 variable_leak / empty_placeholder /
    self_contained 检查。复用 quality_check 的真实实现（临时目录），杜绝逻辑分叉。"""
    body = get_template_body()
    if body is None:
        return ["skill-template.md 中找不到 markdown 围栏正文"]
    import tempfile
    from quality_check import (check_variable_leak, check_empty_placeholder,
                               check_self_contained)
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "SKILL.md").write_text(body, encoding="utf-8")
        gates = {
            "variable_leak": check_variable_leak(Path(td)),
            "empty_placeholder": check_empty_placeholder(Path(td)),
            "self_contained": check_self_contained(body),
        }
    return [f"模板正文 {name}: {detail}"
            for name, (ok, detail) in gates.items() if not ok]


def main():
    data = load_checks_yaml()
    yaml_ids = {c["id"] for c in data["structural_checks"]}

    # 实现对账（registry 的键 = 已实现检查 id；不实际执行检查）
    registry = build_registry("", ROOT / "examples")
    impl_ids = set(registry)

    unimplemented = sorted(yaml_ids - impl_ids)
    unregistered = sorted(impl_ids - yaml_ids)

    map_ids = set()
    for entry in data.get("preflight", {}).get("doc_section_map", []):
        map_ids.update(entry["ids"])
    known_ids = (yaml_ids
                 | set(data.get("behavior_test", {}).keys())
                 | set(data.get("file_manager", {}).keys()))
    map_missing = sorted(map_ids - known_ids)

    import behavior_test
    import file_manager
    missing_bt = sorted(cmd for cmd in ("build_exam", "run_judge", "run_mutation")
                        if not hasattr(behavior_test, cmd))
    missing_fm = sorted(fn for fn in ("cmd_backup", "self_report")
                        if not hasattr(file_manager, fn))

    template_problems = check_template_reconciliation()
    body_gate_problems = check_template_body_gates()

    print(f"checks.yaml 登记数: {len(yaml_ids)}")
    print(f"quality_check 实现数: {len(impl_ids)}")
    print(f"登记但未实现: {unimplemented or '无'}")
    print(f"实现但未登记: {unregistered or '无'}")
    print(f"doc_section_map 引用未登记 id: {map_missing or '无'}")
    print(f"behavior_test 缺命令: {missing_bt or '无'}")
    print(f"file_manager 缺函数: {missing_fm or '无'}")
    print(f"模板标题对账: {template_problems or '一致'}")
    print(f"模板正文门禁: {body_gate_problems or '通过'}")

    drift = bool(unimplemented or unregistered or map_missing
                 or missing_bt or missing_fm or template_problems
                 or body_gate_problems)
    print("PREFLIGHT:", "DRIFT — 先修齐再开工" if drift else "PASS — 三方一致")
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
