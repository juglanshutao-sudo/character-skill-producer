#!/usr/bin/env python3
"""外部语料文本读取的统一入口：编码自动兜底 + 失败显式报错。

背景：用户投喂的原始语料（汉化文本、Galgame 导出、聊天记录等）编码五花八门
（UTF-8/UTF-16/GB18030 都常见），硬编码 utf-8 读取会直接 UnicodeDecodeError，
而天真的「依序试解码」有真陷阱：GB18030 的双字节序列几乎总能碰巧成为合法的
无 BOM UTF-16，静默产出乱码（比抛错更糟）。因此本模块的检测顺序是：

  1. BOM 显式识别（最可靠）：utf-8-sig / utf-16-le / utf-16-be；
  2. utf-8（最常见；合法 UTF-8 是强信号）；
  3. 数据含 0x00 字节 → 尝试无 BOM 的 utf-16-le / utf-16-be
     （GB18030 几乎不含 0x00，这一条把 UTF-16 和 GB18030 分开）；
  4. gb18030（简中 legacy 兜底）；
  5. 全部失败 → 显式报错（绝不静默返回空串/乱码）。

已知原理性限制：无 BOM 且不含 0x00 字节的纯 CJK UTF-16 文件无法与 GB18030
可靠区分（都不带 BOM、日文高位字节无零）——本模块选择报错而不是猜。
遇到时报错信息会说明；请将文件另存为「带 BOM 的 UTF-16」或 UTF-8 再投喂。
（Windows 记事本/PowerShell 另存的 UTF-16 默认带 BOM，实际很少踩到。）

用法：
    from text_io import read_text_auto
    text, enc = read_text_auto(path)                # 全失败时抛 UnicodeDecodeError
    text = read_text_auto(path, errors="skip")      # 全失败时返回 None 并打印告警
"""
import sys
from pathlib import Path


def read_text_auto(path, errors="raise"):
    """编码自动兜底读取。返回 (text, encoding)；skip 模式下全失败返回 None。"""
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise OSError(f"{path}: {exc}") from exc

    # 1) BOM 显式识别（BOM 也可能撒谎/文件损坏：解码失败则落回后续尝试）
    for bom, enc in ((b"\xef\xbb\xbf", "utf-8-sig"),
                     (b"\xff\xfe", "utf-16-le"),
                     (b"\xfe\xff", "utf-16-be")):
        if data.startswith(bom):
            try:
                return data.decode(enc), enc + "(BOM)"
            except UnicodeDecodeError:
                break  # BOM 与内容矛盾，按损坏文件走后续流程

    # 2) utf-8（合法 UTF-8 是强信号）
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass

    # 3) 含 0x00 字节 → 无 BOM UTF-16（GB18030 几乎不含 0x00，以此区分）
    if b"\x00" in data:
        for enc in ("utf-16-le", "utf-16-be"):
            try:
                return data.decode(enc), enc + "(无BOM)"
            except UnicodeDecodeError:
                continue

    # 4) gb18030（简中 legacy 兜底，覆盖 GB2312/GBK）
    try:
        return data.decode("gb18030"), "gb18030"
    except UnicodeDecodeError:
        pass

    msg = (f"{path}: 常见编码均无法解码（BOM/utf-8/utf-16/gb18030）"
           f"——文件可能损坏、加密或为二进制")
    if errors == "skip":
        print(f"[text_io] WARN {msg}", file=sys.stderr)
        return None
    raise UnicodeDecodeError("text_io", data[:32], 0, 1, msg)
