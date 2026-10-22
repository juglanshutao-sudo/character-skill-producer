#!/usr/bin/env python3
"""CSP 更新保护：备份与自报（v2）。

对应 SKILL_V2.md「更新已有 Skill」与 references/checks.yaml 的 file_manager 段：
  - backup：更新前强制快照整个 skill 目录至 backups/<name>-<日期时间>/，
    复制后核对文件数与字节数，不符即报错（拒绝在无有效备份时修改）；
  - restore：从备份恢复；
  - list：列出已有备份；
  - --self-report：自报能力与最近备份状态。

用法：
  python scripts/file_manager.py backup <skill_dir>
  python scripts/file_manager.py restore <backup_dir> <target_dir>
  python scripts/file_manager.py list [skill_name]
  python scripts/file_manager.py --self-report
"""

import hashlib
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKUPS = ROOT / "backups"
BACKUP_LOG = BACKUPS / "backup-log.json"


def fail(msg):
    print(f"FATAL: {msg}")
    sys.exit(2)


def snapshot_state(src):
    """收集文件相对路径 → (字节数, sha256 前 12 位)。"""
    state = {}
    for path in sorted(src.rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            state[str(path.relative_to(src)).replace("\\", "/")] = (
                len(data), hashlib.sha256(data).hexdigest()[:12])
    return state


def load_log():
    if BACKUP_LOG.exists():
        try:
            return json.loads(BACKUP_LOG.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"backups": []}
    return {"backups": []}


def append_log(entry):
    BACKUPS.mkdir(parents=True, exist_ok=True)
    log = load_log()
    log["backups"].append(entry)
    BACKUP_LOG.write_text(json.dumps(log, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cmd_backup(skill_dir):
    skill_dir = Path(skill_dir)
    if not (skill_dir / "SKILL.md").exists():
        fail(f"不是有效的 skill 目录（缺 SKILL.md）: {skill_dir}")
    name = skill_dir.name
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = BACKUPS / f"{name}-{stamp}"
    if dest.exists():
        fail(f"备份目标已存在: {dest}")

    before = snapshot_state(skill_dir)
    if not before:
        fail("源目录没有可备份的文件")

    BACKUPS.mkdir(parents=True, exist_ok=True)
    shutil.copytree(skill_dir, dest)
    after = snapshot_state(dest)

    missing = set(before) - set(after)
    extra = set(after) - set(before)
    changed = [k for k in set(before) & set(after) if before[k] != after[k]]
    if missing or extra or changed:
        # 完整性核对失败：删除残缺备份，拒绝继续
        shutil.rmtree(dest, ignore_errors=True)
        fail(f"备份完整性核对失败 missing={list(missing)[:5]} extra={list(extra)[:5]} "
             f"changed={changed[:5]}——已删除残缺备份，本次更新不得进行")

    entry = {
        "name": name,
        "source": str(skill_dir.resolve()),
        "backup": str(dest.resolve()),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "files": len(before),
        "total_bytes": sum(n for n, _ in before.values()),
        "verified": True,
    }
    append_log(entry)
    print(f"备份完成并核对通过: {dest}")
    print(f"  文件数 {entry['files']}，共 {entry['total_bytes']} 字节")
    print("  现在才允许修改原目录。")
    return 0


def cmd_restore(backup_dir, target_dir):
    backup_dir, target_dir = Path(backup_dir), Path(target_dir)
    if not backup_dir.exists() or not (backup_dir / "SKILL.md").exists():
        fail(f"备份目录无效: {backup_dir}")
    if target_dir.exists():
        fail(f"目标已存在，拒绝覆盖: {target_dir}（先手动移走再恢复）")
    shutil.copytree(backup_dir, target_dir)
    print(f"已恢复 {backup_dir} → {target_dir}")
    return 0


def cmd_list(name_filter=None):
    log = load_log()["backups"]
    rows = [b for b in log if not name_filter or b["name"] == name_filter]
    if not rows:
        print("（无备份记录）")
        return 0
    print(f"{'名称':<24} {'时间':<20} {'文件数':>6} {'字节':>12}  路径")
    for b in rows:
        print(f"{b['name']:<24} {b['created_at']:<20} {b['files']:>6} {b['total_bytes']:>12}  {b['backup']}")
    return 0


def self_report():
    checks_path = ROOT / "references" / "checks.yaml"
    print(f"file_manager.py 自报（{datetime.now().isoformat(timespec='seconds')}）")
    print("  命令: backup / restore / list / --self-report")
    print(f"  备份根目录: {BACKUPS}（{'存在' if BACKUPS.exists() else '尚未创建'}）")
    print(f"  checks.yaml: {'存在' if checks_path.exists() else '缺失！'}"
          f"（file_manager 段承诺 backup_before_update / integrity_verify）")
    log = load_log()["backups"]
    ok = sum(1 for b in log if b.get("verified"))
    print(f"  备份记录: {len(log)} 条，核对通过 {ok} 条")
    return 0


def main():
    args = sys.argv[1:]
    if "--self-report" in args:
        sys.exit(self_report())
    if len(args) < 1:
        print(__doc__)
        sys.exit(1)
    cmd = args[0]
    if cmd == "backup" and len(args) >= 2:
        sys.exit(cmd_backup(args[1]))
    if cmd == "restore" and len(args) >= 3:
        sys.exit(cmd_restore(args[1], args[2]))
    if cmd == "list":
        sys.exit(cmd_list(args[1] if len(args) >= 2 else None))
    print(__doc__)
    sys.exit(1)


if __name__ == "__main__":
    main()
