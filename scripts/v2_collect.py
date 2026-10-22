#!/usr/bin/env python3
"""v2 行为闭环装配器：子代理直接写文件，本脚本只做合并/校验/汇总，
消除编排 AI 手工转录 JSON 的开销。

用法：
  python scripts/v2_collect.py merge-answers <skill_dir> [--arm main|control]
      合并 references/answer-frag[-control]/*.json -> references/behavior-answers.json
      (control 写 behavior-control-answers.json)
  python scripts/v2_collect.py mutation-report <skill_dir>
      读 runs/mutation/<skill>/judges/*.json -> references/behavior-mutation.json
  python scripts/v2_collect.py check <skill_dir>
      校验 behavior-judge.json / behavior-control-judge.json 可解析且结构合法
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from behavior_test import JUDGE_DIMENSIONS, validate_judge_output, load_exam  # noqa: E402


def merge_answers(skill_dir: Path, arm: str):
    frag_dir = skill_dir / ("answer-frag" if arm == "main" else "answer-frag-control")
    if not frag_dir.exists():
        sys.exit(f"FATAL: 缺 {frag_dir}（答题子代理应直接写 fragment 文件到这里）")
    answers = []
    for f in sorted(frag_dir.glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "id" not in data or "answer" not in data:
            sys.exit(f"FATAL: fragment 格式非法: {f.name}")
        answers.append(data)
    exam = load_exam(skill_dir)
    want = [q["id"] for q in exam["questions"]]
    got = [a["id"] for a in answers]
    if sorted(got) != sorted(want):
        sys.exit(f"FATAL: 答案覆盖不符 期望{want} 得到{got}")
    answers.sort(key=lambda a: want.index(a["id"]))
    out = skill_dir / "references" / ("behavior-answers.json" if arm == "main" else "behavior-control-answers.json")
    out.write_text(json.dumps(answers, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[OK] {len(answers)} 答案合并 -> {out}")


def mutation_report(skill_dir: Path):
    judges_dir = ROOT / "runs" / "mutation" / skill_dir.name / "judges"
    if not judges_dir.exists():
        sys.exit(f"FATAL: 缺 {judges_dir}")
    exam = load_exam(skill_dir)
    threshold = 4
    muts, detected_any, notes = [], False, []
    for f in sorted(judges_dir.glob("*.json")):
        mid = f.stem
        judge = json.loads(f.read_text(encoding="utf-8"))
        scores, gate = validate_judge_output(judge, exam, threshold)
        below = []
        for s in scores:
            for dim in JUDGE_DIMENSIONS:
                v = int(s[dim]["score"])
                if v < threshold:
                    below.append(f"{s['question_id']}:{dim}({v})")
        if gate.get("pretending_known") is True:
            below.append("knowledge_gate:pretending_known")
        det = bool(below)
        detected_any = detected_any or det
        muts.append({"id": mid, "detected": det, "below": below,
                     "notes": ("退化检出：" + "、".join(below)) if det
                              else "未检出（全维>=4）；若属已知盲区（K1题面泄漏/复现题证据内嵌），见 detail。"})
        notes.append(f"{mid}={'检出' if det else '存活'}")
    report = {
        "exam_id": exam["exam_id"],
        "mutations": muts,
        "detected": detected_any,
        "detail": "；".join(notes) + ("。" if detected_any else "。全部存活 -> 测试无效(INCONCLUSIVE)。"),
    }
    out = skill_dir / "references" / "behavior-mutation.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[OK] {len(muts)} 突变汇总 detected={detected_any} -> {out}")


def check(skill_dir: Path):
    exam = load_exam(skill_dir)
    threshold = 4
    for rel in ("behavior-judge.json", "behavior-control-judge.json"):
        p = skill_dir / "references" / rel
        if not p.exists():
            print(f"[MISS] {p}")
            continue
        judge = json.loads(p.read_text(encoding="utf-8"))
        scores, gate = validate_judge_output(judge, exam, threshold)
        dims = {}
        for s in scores:
            for d in JUDGE_DIMENSIONS:
                dims[d] = min(dims.get(d, 5), int(s[d]["score"]))
        print(f"[OK] {rel} 合法 维度最低分={dims} 硬门pretending_known={gate.get('pretending_known')}")





def answer_all(skill_dir):
    base = skill_dir / "references" / "subagent-prompts"
    sep = "===== 角色 Skill 结束 ====="
    qids = ["R1", "R2", "E1", "E2", "K1"]
    skill = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    blocks = []
    for q in qids:
        txt = (base / ("answer-" + q + ".md")).read_text(encoding="utf-8")
        blocks.append("==== " + q + " ====" + txt.split(sep)[-1].strip())
    nl = chr(10)
    esc_q = chr(34)
    instruction = (nl + nl + "请依次回答上面全部 5 道题。每题答案前用「【题号】」标注（如【R1】）。"
        "只用 Skill 材料作答；角色不知道的事就说不知道。" + nl +
        "答题完成后，用 Write 工具把结果写成 JSON 数组直接保存到指定目标文件："
        "每题一个对象 {'id': 'R1', 'answer': '...'}（answer 内换行用反斜杠n转义，必须是合法 JSON）。"
        "回复只写 DONE。")
    BS = chr(92)
    tgt = str((skill_dir / "references/behavior-answers.json").resolve())
    prompt = ("你将扮演一个角色。以下是你拥有的全部材料——只有这份角色 Skill 和题目本身。" + nl
        + "不要编造 Skill 里没有的设定。" + nl + nl
        + "===== 角色 Skill 开始 =====" + nl + skill + nl
        + "===== 角色 Skill 结束 =====" + nl + nl.join(blocks) + instruction + nl + nl
        + "写入目标：" + tgt)
    out = ROOT / "runs" / "behavior" / skill_dir.name / "answer-all.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(prompt, encoding="utf-8")
    print("[OK]", out)



def mutation_answer_all(skill_dir, mname):
    """单突变抽检：为指定突变生成一卷五题答题提示词（答题代理直写 answers/<name>.txt）。"""
    mutf = ROOT / "runs" / "mutation" / skill_dir.name
    skill = (mutf / ("SKILL-mutation-" + mname + ".md")).read_text(encoding="utf-8")
    base = skill_dir / "references" / "subagent-prompts"
    sep = "===== 角色 Skill 结束 ====="
    qids = ["R1", "R2", "E1", "E2", "K1"]
    blocks = []
    for q in qids:
        txt = (base / ("answer-" + q + ".md")).read_text(encoding="utf-8")
        blocks.append("==== " + q + " ====" + txt.split(sep)[-1].strip())
    nl = chr(10)
    instruction = (nl + nl + "请依次回答上面全部 5 道题。每题答案前用「【题号】」标注（如【R1】）。"
        "只用 Skill 材料作答；角色不知道的事就说不知道。" + nl +
        "答题完成后，用 Write 工具把全部 5 题作答原文写入指定目标文件（纯文本，按【R1】等标签分节）。回复只写 DONE。")
    BS = chr(92)
    tgt = str((mutf / "answers" / (mname + ".txt")).resolve())
    (mutf / "answers").mkdir(parents=True, exist_ok=True)
    (mutf / "judges").mkdir(parents=True, exist_ok=True)
    prompt = ("你将扮演一个角色。以下是你拥有的全部材料——只有这份角色 Skill 和题目本身。" + nl
        + "不要编造 Skill 里没有的设定；角色不知道的事就说不知道。" + nl + nl
        + "===== 角色 Skill 开始 =====" + nl + skill + nl
        + "===== 角色 Skill 结束 =====" + nl + nl.join(blocks) + instruction + nl + nl + "写入目标：" + tgt)
    out = mutf / "answer-prompts" / (mname + ".md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(prompt, encoding="utf-8")
    print("[OK]", out)





def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    cmd, skill_dir = sys.argv[1], Path(sys.argv[2])
    if cmd == "merge-answers":
        merge_answers(skill_dir, sys.argv[3] if len(sys.argv) > 3 else "main")
    elif cmd == "mutation-report":
        mutation_report(skill_dir)
    elif cmd == "check":
        check(skill_dir)
    elif cmd == "mutation-answer-all":
        mutation_answer_all(skill_dir, sys.argv[3])
    elif cmd == "answer-all":
        answer_all(skill_dir)
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
