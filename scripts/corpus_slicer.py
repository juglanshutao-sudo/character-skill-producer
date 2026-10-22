#!/usr/bin/env python3
"""通用长文本切片器：把任意大文本切成适合逐片蒸馏的语料片。

用途：用户提供完整剧本/小说/长访谈时的第一步加工。CSP 的 5-agent 研究架构
是为百科级摘要语料设计的，塞不下几十万字原文——先用本工具切片，逐片交给
无记忆子 agent 提取证据（产物的证据链挂「源文件+片号+行号」，与 05-key-scenes
锚点体系兼容），再走正常蒸馏流程。

设计要点：
  - 编码自动兜底（text_io.read_text_auto，BOM/utf-8/utf-16/gb18030）；
  - 结构感知切分：优先在段落空行/章节标记处断开，边界附近无结构点才硬切；
  - 片间带 overlap（默认 300 字符），防止跨片的对话/场景被腰斩后丢失归属；
  - 每片落盘 + manifest（源文件 sha256、片数、每片字数与哈希），蒸馏留痕；
  - 不做内容清洗——清洗纪律（丢弃未登场场景/其他线路结局/引擎指令等）由
    投喂前的手工清洗或后续逐片提取时的证据链过滤承担，SKILL_V2 有清单。

用法：
    python scripts/corpus_slicer.py <input.txt> --out <切片输出目录> \
        [--role 角色名] [--slice-chars 15000] [--overlap 300]
输出：
    <out>/slices/0001.txt ...          语料片（头部带元信息注释行）
    <out>/manifest.json                源与片的哈希/字数清单（蒸馏留痕）
"""
import argparse
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from text_io import read_text_auto

# 结构断点优先级：章节标记 > 空行段落边界
CHAPTER_RE = re.compile(r"^(?:第[一二三四五六七八九十百千\d]+[章节幕回]|#{1,3}\s|==+\s|◇{3,}|◆{3,}|---+\s*$)", re.M)


def sha256_of(data):
    return hashlib.sha256(data).hexdigest()


def find_breakpoints(text, target, window):
    """在 [target-window, target+window] 内找最佳断点：章节标记 > 空行。返回绝对偏移或 -1。"""
    lo, hi = max(0, target - window), min(len(text), target + window)
    best = -1
    # 章节标记优先（取窗口内最后一个）
    for m in CHAPTER_RE.finditer(text, lo, hi):
        best = m.start()
    if best >= 0:
        return best
    # 空行段落边界次之
    for m in re.finditer(r"\n\s*\n", text[lo:hi]):
        best = lo + m.end()
    return best


def slice_text(text, slice_chars, overlap):
    """切出 [(start, end)] 片区间（含 overlap 回看），结构感知。"""
    ranges = []
    start = 0
    n = len(text)
    while start < n:
        end = start + slice_chars
        if end >= n:
            ranges.append((start, n))
            break
        bp = find_breakpoints(text, end, window=min(2000, slice_chars // 4))
        end = bp if bp > start else end
        ranges.append((start, end))
        start = max(end - overlap, start + 1)  # 回看 overlap；保底前进
    return ranges


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="原始长文本文件")
    parser.add_argument("--out", required=True, help="切片输出目录")
    parser.add_argument("--role", default="", help="角色名（写入 manifest，便于留痕）")
    parser.add_argument("--slice-chars", type=int, default=15000,
                        help="单片目标字符数（默认 15000，约 7.5k-11k token）")
    parser.add_argument("--overlap", type=int, default=300, help="片间回看字符数")
    args = parser.parse_args()

    src = Path(args.input)
    if not src.exists():
        print(f"FATAL: 输入文件不存在: {src}")
        return 2
    text, enc = read_text_auto(src)
    if not text.strip():
        print(f"FATAL: 输入文件为空: {src}")
        return 2

    out = Path(args.out)
    slices_dir = out / "slices"
    slices_dir.mkdir(parents=True, exist_ok=True)

    ranges = slice_text(text, args.slice_chars, args.overlap)
    entries = []
    for i, (a, b) in enumerate(ranges, 1):
        body = text[a:b]
        head = (f"<!-- slice: {i:04d} | source: {src.name} | chars: {a}-{b} | "
                f"role: {args.role or '(未指定)'} -->\n")
        piece = head + body
        p = slices_dir / f"{i:04d}.txt"
        p.write_text(piece, encoding="utf-8")
        entries.append({
            "slice_id": f"{i:04d}",
            "file": p.relative_to(out).as_posix(),
            "char_range": [a, b],
            "chars": b - a,
            "sha256": sha256_of(piece.encode("utf-8")),
        })

    manifest = {
        "tool": "corpus_slicer.py",
        "generated_at": date.today().isoformat(),
        "role": args.role,
        "source_file": str(src),
        "source_sha256": sha256_of(text.encode("utf-8")),
        "source_chars": len(text),
        "source_encoding": enc,
        "slice_chars": args.slice_chars,
        "overlap": args.overlap,
        "slice_count": len(entries),
        "slices": entries,
        "cleaning_checklist": "投喂前应完成：丢弃角色未登场场景/其他线路结局/引擎指令/无关长段（SKILL_V2 语料清洗清单）；未清洗时逐片提取须以证据链过滤",
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"切片完成：{len(entries)} 片（源 {len(text)} 字符，编码 {enc}）-> {out}/slices/")
    print(f"下一步：python scripts/corpus_resume.py {out} init")
    return 0


if __name__ == "__main__":
    sys.exit(main())
