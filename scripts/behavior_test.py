#!/usr/bin/env python3
"""CSP 第二层：行为测试闭环（v2）。

对应 SKILL_V2.md Phase 9 第二层与 references/checks.yaml 的 behavior_test 段：
  - 证据链出题：复现 2 + 外推 2，每题期望反应必须挂证据（场景 + 来源），禁止凭记忆出题；
  - 无记忆子 agent 执行：脚本生成的答题提示词只含 SKILL.md 正文 + 题目，
    不含 research / distillation 等蒸馏侧材料（防污染）；
  - 独立 judge：四维各带 1-5 分锚点；对不在场之事装知道 = 硬门 FAIL；
  - --mutation 突变测试：突变 SKILL.md 后重测，judge 检不出退化 = 测试无效。

脚本负责确定性的骨架（出题、防污染提示词、判分校验、突变生成、报告落盘）；
LLM 调用通过 --llm-cmd 注入（模板中 {prompt_file} 会被替换为提示词文件路径），
不注入时脚本生成提示词文件后退出，由编排 AI 用全新子 agent 完成对应步骤。

用法：
  python scripts/behavior_test.py exam <skill_dir>
  python scripts/behavior_test.py judge <skill_dir> --answers references/behavior-answers.json [--llm-cmd "..."]
  python scripts/behavior_test.py control <skill_dir>
  python scripts/behavior_test.py judge <skill_dir> --arm control --answers references/behavior-control-answers.json [--llm-cmd "..."]
  python scripts/behavior_test.py mutation <skill_dir> [--llm-cmd "..."]
"""

import hashlib
import json
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from quality_check import load_checks_yaml, extract_section, read_text, load_json  # noqa: E402

JUDGE_DIMENSIONS = ["语气一致性", "行为模式命中", "知识边界", "口癖质感"]

JUDGE_RUBRIC = """你是独立判卷人。你没有参与出题，也没有见过被测 Skill 的蒸馏过程。
对每段回答按四个维度打 1-5 分，打分必须引用下面给出的证据链，禁止凭你自己对该角色的记忆判卷。

四维锚点（统一刻度）：
- 5 = 与证据链中的行为/语气高度一致，细节可指认；
- 4 = 模式命中、语气基本一致，有轻微偏差但不出戏；
- 3 = 命中一半：或行为对但语气错，或语气对但行为错；
- 2 = 明显偏离证据链描述的行为模式；
- 1 = 完全不像该角色，或违反硬约束。

四维定义：
1. 语气一致性：句式、称呼、自称、语尾是否与证据一致；
2. 行为模式命中：是否做出证据链指向的行为（先做什么、后做什么、绝不做什么）；
3. 知识边界：对角色不在场、不知道的事是否保持「不知道」；装知道（编造在场细节、断言未知剧情）直接触发硬门；
4. 口癖质感：口头禅、停顿、情绪泄露方式是否可辨认。

输出必须是且仅是一个 JSON 对象（不要 markdown 代码块包裹）：
{
  "scores": [
    {"question_id": "R1", "语气一致性": {"score": 0, "anchor": "引用锚点理由"},
     "行为模式命中": {"score": 0, "anchor": "..."}, "知识边界": {"score": 0, "anchor": "..."},
     "口癖质感": {"score": 0, "anchor": "..."}}
    , ... 每题一项
  ],
  "knowledge_gate": {"pretending_known": false, "evidence": "指出哪句装知道；没有则写 none"},
  "overall": {"pass": true, "reason": "..."}
}
"""

ANSWER_PROMPT_TEMPLATE = """你将扮演一个角色。以下是你拥有的全部材料——只有这份角色 Skill 和题目本身。
不要编造 Skill 里没有的设定；角色不知道的事就说不知道。

===== 角色 Skill 开始 =====
{skill_body}
===== 角色 Skill 结束 =====

===== 题目 =====
{question}
（请以角色身份回答，2-4 段对话体即可。）

直接输出角色的回答，不要任何解释、前后缀或 markdown 标题。
"""

# 兼容三种标题形态：「## 场景一：xxx」（编号式，砂金/丹恒等）、「## 场景：xxx」（无编号式，希露瓦等，
# 因 SKILL.md 锚点引用原始标题而不可重编号）与「## 场景·xxx」（间隔号式，彦卿/镜流/驭空等 7 席；
# quality_check 按 ^## 场景 宽前缀计数，出题器须同口径，否则 0 场景误中止）
SCENE_HEADER = re.compile(r"^#{2,3}\s*(场景\s*[一二三四五六七八九十\d：:·][^\n]*)$", re.MULTILINE)
SOURCE_REF = re.compile(r"(来源|source)[:：]?\s*([^\n]+)", re.IGNORECASE)
SUBAGENT_PROMPT_RULE = "提示词里只允许出现 SKILL.md 正文与题目本身，出现蒸馏侧关键词即为污染"

# A/B 对照：B 组（无 skill 基线）提示词——只有角色名+作品名+题目。
# 刻意不含 A 组的「不要编造 Skill 里没有的设定；不知道就说不知道」——那条本身就是 skill 纪律，
# 基线组带上它等于把被测变量放进了对照组。
CONTROL_PROMPT_TEMPLATE = """请以《{work}》中的{character}的身份回答下面的问题。
（请以角色身份回答，2-4 段对话体即可。）

===== 题目 =====
{question}

直接输出角色的回答，不要任何解释、前后缀或 markdown 标题。
"""


def fail(msg):
    print(f"FATAL: {msg}")
    sys.exit(2)


def input_fingerprints(skill_dir):
    """Bind future exams to the skill and evidence files, not filesystem timestamps."""
    paths = [skill_dir / "SKILL.md", skill_dir / "manifest.json",
             skill_dir / "references" / "sources.json",
             skill_dir / "references" / "distillation.md"]
    paths += sorted((skill_dir / "references" / "research").glob("*.md"))
    return {p.relative_to(skill_dir).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in paths if p.is_file()}


def load_exam(skill_dir):
    exam_path = skill_dir / "references" / "behavior-exam.json"
    if not exam_path.exists():
        fail(f"缺 {exam_path}，先运行: python scripts/behavior_test.py exam {skill_dir}")
    exam = json.loads(exam_path.read_text(encoding="utf-8"))
    if "input_sha256" in exam and exam["input_sha256"] != input_fingerprints(skill_dir):
        fail("Skill 或证据文件在出卷后已改变，请重新出卷并补测，不能沿用旧卷")
    return exam


def parse_scenes(key_scenes_text):
    matches = list(SCENE_HEADER.finditer(key_scenes_text))
    # 文件级来源头（如「来源：ak-corpus 剧情正文…」）作为场景缺省来源；场景内单独标注则覆盖
    header = key_scenes_text[:matches[0].start()] if matches else ""
    file_src = SOURCE_REF.search(header)
    file_source = file_src.group(2).strip() if file_src else None
    scenes = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(key_scenes_text)
        body = key_scenes_text[m.end():end].strip()
        cut = re.search(r"^##\s+(?!场景)", body, re.MULTILINE)
        if cut:
            body = body[:cut.start()].strip()
        src = SOURCE_REF.search(body)
        scenes.append({
            "title": m.group(1).strip(),
            "body": body,
            "source_ref": src.group(2).strip() if src else file_source,
        })
    return scenes


def build_exam(skill_dir):
    skill_path = skill_dir / "SKILL.md"
    content = read_text(skill_path)
    scenes_text = read_text(skill_dir / "references" / "research" / "05-key-scenes.md")
    scenes = parse_scenes(scenes_text)
    if len(scenes) < 2:
        fail(f"05-key-scenes.md 只解析出 {len(scenes)} 个场景，复现题需要 2 个（证据链出题失败即中止）")
    for s in scenes:
        if not s["source_ref"]:
            fail(f"场景「{s['title']}」缺来源标注——无证据链的场景禁止出题")

    repro = []
    for s in scenes[:2]:
        repro.append({
            "id": f"R{len(repro) + 1}",
            "type": "reproduction",
            "question": f"原作场景复现——{s['title']}。请以角色身份重现该角色在这个场景中的反应"
                        f"（对话体，2-3 轮）。",
            "evidence": {"scene": s["title"], "quote": s["body"][:400], "source": s["source_ref"]},
        })

    body = extract_section(content, r"行为动态|行为模式")
    if not body:
        fail("SKILL.md 无行为动态章节，无法出外推题")
    # 提取行为模式（### 子标题 + 为什么行），按 checks.yaml 行为测试要求出外推题
    subs = []
    for part in re.split(r"^###\s+", body, flags=re.MULTILINE)[1:]:
        lines = part.split("\n", 1)
        title = lines[0].strip()
        text = lines[1] if len(lines) > 1 else ""
        why = re.search(r"\*\*为什么\*\*[:：]?\s*([^\n]+)", text)
        subs.append({"title": title, "why": why.group(1).strip() if why else ""})
    if len(subs) < 2:
        fail("行为模式不足 2 条，无法出外推题")
    frames = [
        "一位与该角色关系尚浅的新识者陷入低落，向其倾诉——原作没有直接写过这场对话。",
        "该角色遇到一件原作中从未发生过的突发状况（资料截止日期之后的日常事件），需要当场回应。",
    ]
    extrap = []
    for i, p in enumerate(subs[:2]):
        extrap.append({
            "id": f"E{i + 1}",
            "type": "extrapolation",
            "question": f"外推测试（原作未写过的新情境）：{frames[i]}"
                        f"请以角色身份回应。判卷依据是行为模式「{p['title']}」",
            "evidence": {"pattern": p["title"], "why": p["why"] or "（模式缺为什么段）",
                         "source": "SKILL.md 行为动态（蒸馏自 research 证据链）"},
        })

    # 知识边界陷阱题：硬门（装知道=FAIL）必须可被触发，否则形同虚设
    honesty = extract_section(content, r"知识边界|诚实局限|诚实边界")
    trap = None
    if honesty:
        unknown_bullets = re.findall(r"^[-*]\s+(.+)$", honesty, re.MULTILINE)
        unknown = [b for b in unknown_bullets if re.search(r"不知道|不明|涂黑|没有告诉我|不在场|没有全|未核|不可知|说不上|无法确认|没听过|没见过|从未|记不清", b)]
        if unknown:
            char = "角色"
            manifest = load_json(skill_dir / "manifest.json", {})
            if manifest.get("character"):
                char = manifest["character"]
            trap = {
                "id": "K1",
                "type": "knowledge_trap",
                # 只取条目的主题段（破折号前），不引用纪律规则原文，也不提示知情状态——
                # 否则答题者可回声规则文本，remove-knowledge-boundary 突变必然检不出
                "question": f"有人笃定地向{char}提起下列话题，并不断追问更多细节：「{unknown[0].split('——')[0].split('——')[0][:60]}」"
                            f"——请以角色身份回应。",
                "evidence": {"rule": f"知识边界（我不知道）：{unknown[0][:120]}",
                             "source": "SKILL.md 知识边界与诚实局限（硬门考核点，judge 侧保留）"},
            }
    if trap is None:
        fail("SKILL.md 无「我不知道」条目，无法出知识边界陷阱题——硬门不可考核即中止")

    exam = {
        "exam_id": f"exam-{date.today().isoformat()}-{datetime.now().strftime('%H%M%S')}",
        "skill": skill_dir.name,
        "input_sha256": input_fingerprints(skill_dir),
        "reproduction_count": len(repro),
        "extrapolation_count": len(extrap),
        "knowledge_trap_count": 1,
        "questions": repro + extrap + [trap],
        "contamination_rule": SUBAGENT_PROMPT_RULE,
    }
    out = skill_dir / "references" / "behavior-exam.json"
    out.write_text(json.dumps(exam, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    prompt_dir = skill_dir / "references" / "subagent-prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    skill_body = content
    for q in exam["questions"]:
        prompt = ANSWER_PROMPT_TEMPLATE.format(skill_body=skill_body, question=q["question"])
        (prompt_dir / f"answer-{q['id']}.md").write_text(prompt, encoding="utf-8")
    print(f"Exam {exam['exam_id']}: {len(repro)} 复现 + {len(extrap)} 外推 + 1 知识陷阱")
    print(f"  题目: {out}")
    print(f"  答题提示词（无记忆子 agent 用，只含 SKILL.md+题目）: {prompt_dir}/")
    print("  下一步：用全新子 agent 分别执行各提示词，回答汇入 references/behavior-answers.json:")
    print('  [{"id": "R1", "answer": "..."}, ...]')
    return exam


def validate_answers(exam, answers):
    if not isinstance(answers, list) or len(answers) != len(exam["questions"]):
        fail(f"answers 应为 {len(exam['questions'])} 条的数组")
    by_id = {a.get("id"): a.get("answer", "") for a in answers}
    for q in exam["questions"]:
        if q["id"] not in by_id or len(str(by_id[q["id"]]).strip()) < 20:
            fail(f"题目 {q['id']} 缺回答或回答过短")
    return by_id


def call_llm(llm_cmd, prompt_text, workdir):
    prompt_file = workdir / f"llm-prompt-{datetime.now().strftime('%H%M%S%f')}.txt"
    prompt_file.write_text(prompt_text, encoding="utf-8")
    cmd = llm_cmd.replace("{prompt_file}", str(prompt_file))
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        fail(f"llm-cmd 失败(exit {result.returncode}): {result.stderr[:300]}")
    out = result.stdout.strip()
    prompt_file.unlink(missing_ok=True)
    return out


def extract_json(text):
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        fail("judge 输出中没有 JSON 对象")
    return json.loads(match.group(0))


def validate_judge_output(judge, exam, threshold):
    if not isinstance(judge, dict) or not {"scores", "knowledge_gate", "overall"}.issubset(judge):
        fail("judge JSON 缺 scores / knowledge_gate / overall 字段")
    if "exam_id" in judge and judge["exam_id"] != exam["exam_id"]:
        fail("judge exam_id 与当前考卷不一致")
    if exam.get("input_sha256") and judge.get("exam_id") != exam["exam_id"]:
        fail("已绑定输入的新考卷必须使用带匹配 exam_id 的判卷，禁止沿用无编号旧结果")
    scores = judge["scores"]
    if (not isinstance(scores, list) or not scores
            or len(scores) != len(exam["questions"])
            or not all(isinstance(s, dict) for s in scores)):
        fail("judge scores 必须逐题且不重复覆盖当前考卷")
    qids = {q["id"] for q in exam["questions"]}
    got_ids = {s.get("question_id") for s in scores}
    if got_ids != qids:
        fail(f"judge 覆盖题目不符: 期望 {sorted(qids)}, 得到 {sorted(got_ids)}")
    for s in scores:
        for dim in JUDGE_DIMENSIONS:
            entry = s.get(dim)
            if not isinstance(entry, dict) or "score" not in entry or "anchor" not in entry:
                fail(f"题目 {s.get('question_id')} 维度 {dim} 缺 score/anchor（锚点必填）")
            if type(entry["score"]) is not int or not (1 <= entry["score"] <= 5):
                fail(f"题目 {s.get('question_id')} 维度 {dim} 分数越界: {entry['score']}")
            if len(str(entry["anchor"]).strip()) < 6:
                fail(f"题目 {s.get('question_id')} 维度 {dim} 锚点理由过空")
    gate = judge["knowledge_gate"]
    if not isinstance(gate, dict) or type(gate.get("pretending_known")) is not bool:
        fail("knowledge_gate.pretending_known 必须为布尔值")
    return scores, gate


def build_control(skill_dir):
    """A/B 对照的 B 组（无 skill 基线）：同一套题，提示词只含角色名+作品名+题目。"""
    exam = load_exam(skill_dir)
    manifest = load_json(skill_dir / "manifest.json", {})
    character = manifest.get("character") or skill_dir.name
    work = manifest.get("work") or ""
    control = {
        "exam_id": exam["exam_id"],
        "skill": exam["skill"],
        "arm": "control-no-skill",
        "character": character,
        "work": work,
        "prompt_rule": "基线组：仅角色名+作品名+题目；零 skill 内容、零边界纪律指令"
                       "（A 组的『不知道就说不知道』本身就是被测变量，不得进入基线组）",
        "questions": [{"id": q["id"], "type": q["type"], "question": q["question"]}
                      for q in exam["questions"]],
    }
    out = skill_dir / "references" / "behavior-control.json"
    out.write_text(json.dumps(control, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    prompt_dir = skill_dir / "references" / "subagent-prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    for q in exam["questions"]:
        prompt = CONTROL_PROMPT_TEMPLATE.format(work=work, character=character, question=q["question"])
        (prompt_dir / f"control-{q['id']}.md").write_text(prompt, encoding="utf-8")
    print(f"Control（无 skill 基线组）{len(exam['questions'])} 题已生成: {out}")
    print(f"  基线答题提示词: {prompt_dir}/control-*.md")
    print("  下一步：用全新子 agent 分别执行各基线提示词，回答汇入 references/behavior-control-answers.json:")
    print('  [{"id": "R1", "answer": "..."}, ...]')
    print(f"  再运行: python scripts/behavior_test.py judge {skill_dir} --arm control")
    return control


def run_judge(skill_dir, answers_path, llm_cmd=None, arm="a"):
    checks = load_checks_yaml()
    bt = checks.get("behavior_test", {})
    threshold = int(bt.get("pass_threshold_per_dim", 4))
    exam = load_exam(skill_dir)
    apath = Path(answers_path)
    if not apath.exists():
        fail(f"答案文件不存在: {apath}")
    answers = json.loads(apath.read_text(encoding="utf-8"))
    by_id = validate_answers(exam, answers)

    qblock = []
    for q in exam["questions"]:
        qblock.append({
            "question_id": q["id"], "type": q["type"], "question": q["question"],
            "evidence_chain": q["evidence"], "answer": by_id[q["id"]],
        })
    judge_prompt = (JUDGE_RUBRIC
                    + "\n输出同时包含 exam_id: " + exam["exam_id"]
                    + "\n判分阈值：每维 >= " + str(threshold) + " 分才通过。\n"
                    + "题目、证据链与回答：\n"
                    + json.dumps(qblock, ensure_ascii=False, indent=2))

    runs_dir = ROOT / "runs" / "behavior" / exam["skill"]
    runs_dir.mkdir(parents=True, exist_ok=True)
    judge_out_name = "behavior-judge.json" if arm == "a" else "behavior-control-judge.json"
    prompt_name = "judge-prompt.md" if arm == "a" else "judge-control-prompt.md"
    if llm_cmd:
        raw = call_llm(llm_cmd, judge_prompt, runs_dir)
        judge = extract_json(raw)
        scores, gate = validate_judge_output(judge, exam, threshold)
    else:
        pfile = runs_dir / prompt_name
        pfile.write_text(judge_prompt, encoding="utf-8")
        print(f"已生成 judge 提示词: {pfile}")
        print(f"用独立（无出题记忆）的 judge 执行它，输出 JSON 存为 references/{judge_out_name}，")
        print(f"再运行: python scripts/behavior_test.py finalize {skill_dir}")
        return

    if arm == "control":
        out = skill_dir / "references" / judge_out_name
        out.write_text(json.dumps(judge, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"对照组 judge 结果已写入 {out}；运行 finalize 合并 A/B 对照结论")
        return
    out = skill_dir / "references" / judge_out_name
    out.write_text(json.dumps(judge, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return finalize(skill_dir, exam, scores, gate, judge.get("overall", {}))


def finalize(skill_dir, exam, scores, gate, overall):
    checks = load_checks_yaml()
    threshold = int(checks.get("behavior_test", {}).get("pass_threshold_per_dim", 4))
    dim_min = {}
    for s in scores:
        for dim in JUDGE_DIMENSIONS:
            dim_min[dim] = min(dim_min.get(dim, 5), int(s[dim]["score"]))
    dim_pass = {d: v >= threshold for d, v in dim_min.items()}
    hard_gate_pass = not gate.get("pretending_known", True)

    # 突变结果合并
    mutation_result = None
    mut_path = skill_dir / "references" / "behavior-mutation.json"
    if mut_path.exists():
        mutation_result = json.loads(mut_path.read_text(encoding="utf-8"))

    if mutation_result is not None and (not isinstance(mutation_result, dict)
                                       or type(mutation_result.get("detected")) is not bool):
        fail("behavior-mutation.json 缺布尔 detected 结果，不能验收")
    if (mutation_result is not None and "exam_id" in mutation_result
            and mutation_result["exam_id"] != exam["exam_id"]):
        fail("突变结果 exam_id 与当前考卷不一致")
    if (mutation_result is not None and exam.get("input_sha256")
            and mutation_result.get("exam_id") != exam["exam_id"]):
        fail("新考卷的突变结果必须包含匹配的 exam_id")
    main_pass = (set(dim_min) == set(JUDGE_DIMENSIONS)
                 and all(dim_pass.values()) and hard_gate_pass)
    if not main_pass:
        verdict = "FAIL"
    elif mutation_result is None:
        verdict = "INCOMPLETE"
    elif mutation_result.get("detected") is not True:
        # 突变测试检不出退化：可能是等价突变，或扮演者参数化知识掩盖了 skill 缺失
        verdict = "INCONCLUSIVE"
    else:
        verdict = "PASS"
    # A/B 对照合并（B 组 = 无 skill 基线）：突变 INCONCLUSIVE 时的规定后续（checks.yaml ab_control_rule）
    ab = None
    ab_path = skill_dir / "references" / "behavior-control-judge.json"
    if ab_path.exists():
        ab_judge = json.loads(ab_path.read_text(encoding="utf-8"))
        b_scores, b_gate = validate_judge_output(ab_judge, exam, threshold)
        b_dim_min = {}
        for s in b_scores:
            for dim in JUDGE_DIMENSIONS:
                b_dim_min[dim] = min(b_dim_min.get(dim, 5), int(s[dim]["score"]))
        deltas = {d: dim_min[d] - b_dim_min[d] for d in JUDGE_DIMENSIONS}
        delta_sum = sum(deltas.values())
        a_gate_pass = not gate.get("pretending_known", True)
        b_gate_pass = not b_gate.get("pretending_known", True)
        contribution = ((a_gate_pass and not b_gate_pass)
                        or deltas["知识边界"] >= 2 or delta_sum >= 3)
        if contribution:
            conclusion = ("A/B 对照测得 skill 贡献：测试工具能区分有/无 skill，"
                          "突变 INCONCLUSIVE 属等价突变/参数化知识掩盖，skill 本身有可测增益")
        else:
            conclusion = ("A/B 对照未测得明显 skill 增益：该角色的参数化知识已覆盖本题面考核点；"
                          "skill 的价值在边界/版本/可更新性/控制权，不在本题面得分")
        ab = {
            "arm_a": "with-skill",
            "arm_b": "no-skill baseline（仅角色名+作品名+题目，零 skill 内容与边界纪律）",
            "b_dim_min_scores": b_dim_min,
            "b_knowledge_gate": b_gate,
            "dim_deltas": deltas,
            "delta_sum": delta_sum,
            "skill_contribution_detected": contribution,
            "conclusion": conclusion,
        }

    report = {
        "exam_id": exam["exam_id"],
        "skill": exam["skill"],
        "input_sha256": exam.get("input_sha256"),
        "version_binding": "hashed_exam_inputs" if exam.get("input_sha256") else "legacy_unverified",
        "checked_at": date.today().isoformat(),
        "method": "fresh-subagent answers + independent judge (evidence-chained)",
        "executor_isolation": "fresh sub-agent（每次独立实例）；输入仅 SKILL.md 正文 + 题目，"
                              "由脚本生成、不含蒸馏侧文件（见 checks.yaml contamination_guard）；"
                              "主 agent 记忆不参与答题为纪律约束——审计时核对作答文本是否含蒸馏侧专有信息",
        "threshold_per_dim": threshold,
        "dim_min_scores": dim_min,
        "dim_pass": dim_pass,
        "knowledge_gate": gate,
        "overall": overall,
        "mutation_test": mutation_result,
        "ab_control": ab,
        "verdict": verdict,
        "verdict_rule": "主答卷不合格=FAIL（优先）；缺突变结果=INCOMPLETE；"
                        "PASS=四维过阈值+硬门过+突变检出退化；突变未检出=INCONCLUSIVE（测试无效，结果不可采信）；"
                        "ab_control.skill_contribution_detected 表明测试工具能否测出 skill 贡献",
        "note": "本报告由 behavior_test.py 生成；旧版自演自判报告已作废",
    }
    (skill_dir / "references" / "behavior-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"维度最低分: {dim_min}（阈值 {threshold}）")
    print(f"知识边界硬门: {'PASS' if hard_gate_pass else 'FAIL — 装知道'}")
    if mutation_result:
        print(f"突变检测: {'检出退化' if mutation_result.get('detected') else '未检出 → INCONCLUSIVE（测试无效）'}")
    if ab:
        print(f"A/B 对照: B组最低分 {ab['b_dim_min_scores']}；"
              f"skill 贡献 {'检出' if ab['skill_contribution_detected'] else '未检出'}")
    print(f"Verdict: {report['verdict']}")
    return 0 if verdict == "PASS" else 1


def run_finalize(skill_dir):
    exam = load_exam(skill_dir)
    judge_path = skill_dir / "references" / "behavior-judge.json"
    if not judge_path.exists():
        fail(f"缺 {judge_path}")
    judge = json.loads(judge_path.read_text(encoding="utf-8"))
    scores, gate = validate_judge_output(judge, exam,
                                         int(load_checks_yaml().get("behavior_test", {}).get("pass_threshold_per_dim", 4)))
    return finalize(skill_dir, exam, scores, gate, judge.get("overall", {}))


MUTATIONS = [
    # 知识边界：v2 实践为合并标题「知识边界与诚实局限」，模板旧版为分离的「知识边界」+「诚实边界」，
    # 正则兼容两种形态（re.sub 全局替换，分离形态两段都剥掉）
    ("remove-knowledge-boundary", [(r"## (?:知识边界|诚实边界)(?:与诚实局限)?.*?(?=\n## |\Z)", "")]),
    # generic/strip 突变必须连「行为示例」一并剥掉——示例里保留了全部原话引文，
    # 留着它等于给答题者留完整行为目录，突变退化为等价突变（2026-09-12 老鲤批实证修复）
    ("generic-reactions", [(r"(## 行为动态.*?)(?=\n## )",
                            "\n## 行为动态\n\n### 友善回应\n对任何话题都热情、礼貌、有问必答，语气始终积极。\n"
                            "**为什么**：作为一个助手应当让对话顺畅。\n\n"),
                           (r"## 行为示例.*?(?=\n## 面对资料更新|\Z)", "")]),
    # 剥掉行为核心：运行核心 + 行为动态 + 行为示例一起替换/删除
    ("strip-behavior-core", [
        (r"## 运行核心.*?(?=\n## )",
         "\n## 运行核心\n\n1. **核心动机**：与他人愉快地交流。\n2. **默认反应**：有问必答，积极回应。\n\n"),
        (r"(## 行为动态.*?)(?=\n## )",
         "\n## 行为动态\n\n### 友善回应\n对任何话题都热情、礼貌、有问必答，语气始终积极。\n"
         "**为什么**：作为一个助手应当让对话顺畅。\n\n"),
        (r"## 行为示例.*?(?=\n## 面对资料更新|\Z)", ""),
    ]),
    ("remove-expression", [(r"## 表达质感.*?(?=\n## |\Z)", "")]),
]


def run_mutation(skill_dir, llm_cmd=None):
    exam = load_exam(skill_dir)
    """突变测试：至少做规则删除/替换突变，重测后 judge 必须检出退化。"""
    content = read_text(skill_dir / "SKILL.md")
    mut_dir = ROOT / "runs" / "mutation" / skill_dir.name
    mut_dir.mkdir(parents=True, exist_ok=True)
    made = []
    for name, subs in MUTATIONS:
        mutated = content
        ok = True
        for pattern, repl in subs:
            mutated, n = re.subn(pattern, repl, mutated, flags=re.DOTALL)
            if n == 0:
                ok = False
                break
        if not ok:
            continue
        path = mut_dir / f"SKILL-mutation-{name}.md"
        path.write_text(mutated, encoding="utf-8")
        made.append({"name": name, "file": str(path), "changed": True})
    if not made:
        fail("没有任何突变成功命中——检查 SKILL.md 章节标题是否匹配")
    (mut_dir / "manifest.json").write_text(
        json.dumps({"generated_at": date.today().isoformat(), "exam_id": exam["exam_id"], "mutations": made},
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"生成 {len(made)} 个突变 SKILL → {mut_dir}")

    if not llm_cmd:
        print("手动流程：对每个突变文件重复 exam→答题→judge，若 judge 对突变版仍给 PASS → 突变测试无效。")
        print("将结果写入 references/behavior-mutation.json:")
        print('  {"exam_id": ' + json.dumps(exam["exam_id"]) + ', "mutations": [...], "detected": true, "detail": "..."}')
        return 0

    exam = load_exam(skill_dir)
    detected = False
    results = []
    for m in made:
        ans = []
        for q in exam["questions"]:
            prompt = ANSWER_PROMPT_TEMPLATE.format(
                skill_body=Path(m["file"]).read_text(encoding="utf-8"), question=q["question"])
            ans.append({"id": q["id"], "answer": call_llm(llm_cmd, prompt, mut_dir)})
        qblock = [{"question_id": q["id"], "question": q["question"],
                   "evidence_chain": q["evidence"], "answer": next(a["answer"] for a in ans if a["id"] == q["id"])}
                  for q in exam["questions"]]
        raw = call_llm(llm_cmd, JUDGE_RUBRIC + "\n输出同时包含 exam_id: " + exam["exam_id"] + "\n题目、证据链与回答：\n"
                       + json.dumps(qblock, ensure_ascii=False, indent=2), mut_dir)
        judge = extract_json(raw)
        scores, gate = validate_judge_output(judge, exam, 4)
        low = min(min(int(s[d]["score"]) for d in JUDGE_DIMENSIONS) for s in scores)
        hit = low <= 3 or gate.get("pretending_known")
        results.append({"mutation": m["name"], "min_score": low,
                        "pretending_known": gate.get("pretending_known"), "detected": hit})
        detected = detected or hit
    out = skill_dir / "references" / "behavior-mutation.json"
    out.write_text(json.dumps({"exam_id": exam["exam_id"], "mutations": results, "detected": detected,
                               "rule": "突变版必须被 judge 判差，否则测试无效"},
                              ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"突变检测 {'通过（judge 检出退化）' if detected else '失败（judge 对突变版照常给高分 → 测试无效）'}")
    return 0 if detected else 1


def main():
    args = sys.argv[1:]
    if len(args) < 2:
        print(__doc__)
        sys.exit(1)
    cmd, skill_dir = args[0], Path(args[1])
    if not (skill_dir / "SKILL.md").exists():
        fail(f"SKILL.md 不存在: {skill_dir}")
    llm_cmd = None
    if "--llm-cmd" in args:
        llm_cmd = args[args.index("--llm-cmd") + 1]
    if cmd == "exam":
        build_exam(skill_dir)
    elif cmd == "control":
        build_control(skill_dir)
    elif cmd == "judge":
        answers = args[args.index("--answers") + 1] if "--answers" in args else None
        if not answers:
            fail("judge 需要 --answers <behavior-answers.json>")
        arm = args[args.index("--arm") + 1] if "--arm" in args else "a"
        if arm not in ("a", "control"):
            fail(f"未知 arm: {arm}（可选 a / control）")
        sys.exit(run_judge(skill_dir, answers, llm_cmd, arm=arm) or 0)
    elif cmd == "finalize":
        sys.exit(run_finalize(skill_dir))
    elif cmd == "mutation":
        sys.exit(run_mutation(skill_dir, llm_cmd) or 0)
    else:
        print(f"未知命令: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
