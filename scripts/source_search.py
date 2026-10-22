#!/usr/bin/env python3
"""Unified local source discovery entrypoint for CSP."""

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

from source_registry import is_cross_media_work, is_excluded_url, source_hints_for_work, source_tier

ROOT = Path(__file__).resolve().parent
REGISTRY_PATH = ROOT.parent / "standard" / "channel-registry.json"
if not REGISTRY_PATH.exists():
    REGISTRY_PATH = ROOT.parent / "references" / "channel-registry.json"
TODAY = date.today().isoformat()
HSR_OFFICIAL_STORY_URL = (
    "https://bbs.mihoyo.com/sr/wiki/content/2009/detail"
    "?bbs_presentation_style=no_header"
)

# BWIKI game slug mapping — mirrors bwiki_api.py GAME_SLUG_MAP
BWIKI_GAME_SLUGS = {
    "原神": "ys", "Genshin": "ys",
    "星穹铁道": "sr", "Star Rail": "sr",
    "明日方舟": "arknights", "Arknights": "arknights",
    "蔚蓝档案": "ba", "Blue Archive": "ba",
    "碧蓝航线": "blhx", "Azur Lane": "blhx",
    "崩坏3": "bh3", "Honkai Impact": "bh3",
    "鸣潮": "wutheringwaves", "Wuthering Waves": "wutheringwaves",
}


def sha256_text(text):
    if not text:
        return None
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def run_python_script(script, args, timeout):
    cmd = [sys.executable, str(ROOT / script), *args, "--timeout", str(timeout)]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
        stdout = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
        stderr = proc.stderr.decode("utf-8", errors="replace") if proc.stderr else ""
        payload = json.loads(stdout) if stdout.strip() else {}
        payload["retrieved_by"] = " ".join(cmd)
        payload["command_status"] = proc.returncode
        if stderr.strip():
            payload.setdefault("warnings", []).append(stderr.strip())
        return payload
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "source": script,
            "status": "failed",
            "error": "timeout",
            "message": str(exc),
            "retrieved_by": " ".join(cmd),
        }
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "source": script,
            "status": "failed",
            "error": "json_decode_error",
            "message": str(exc),
            "retrieved_by": " ".join(cmd),
        }


def normalize_moegirl(raw, query, work):
    url = raw.get("page_url") or raw.get("url")
    excluded = is_excluded_url(url)
    extract = raw.get("extract", "") or ""
    status = "ok" if raw.get("ok") and not excluded else "failed"
    return {
        "id": f"moegirl-{raw.get('resolved_title') or query}".replace(" ", "-"),
        "source": "moegirl",
        "title": raw.get("resolved_title"),
        "resolved_title": raw.get("resolved_title"),
        "url": url,
        "query": query,
        "work": work,
        "source_tier": "excluded" if excluded else source_tier("moegirl"),
        "officiality": "excluded" if excluded else "secondary",
        "media_type": "wiki",
        "language": "zh",
        "retrieved_at": TODAY,
        "status": status,
        "warnings": raw.get("warnings", []),
        "attempted_modes": raw.get("attempted_modes") or [raw.get("mode")],
        "retrieved_by": raw.get("retrieved_by"),
        "pageid": raw.get("pageid"),
        "extract_chars": raw.get("extract_chars", len(extract)),
        "content_hash": sha256_text(extract),
        "error": raw.get("error"),
    }


def discover_moegirl(query, work, timeout):
    raw = run_python_script("moegirl_api.py", [query], timeout)
    return normalize_moegirl(raw, query, work)


def resolve_bwiki_game(work):
    """Resolve a work name to a BWIKI game slug."""
    if not work:
        return None
    for name, slug in BWIKI_GAME_SLUGS.items():
        if name.lower() in work.lower() or work.lower() in name.lower():
            return slug
    return None


def normalize_bwiki(raw, query, work):
    url = raw.get("page_url") or raw.get("url")
    excluded = is_excluded_url(url)
    extract = raw.get("extract", "") or ""
    status = "ok" if raw.get("ok") and not excluded else "failed"
    return {
        "id": f"bwiki-{raw.get('resolved_title') or query}".replace(" ", "-"),
        "source": "bwiki",
        "title": raw.get("resolved_title"),
        "resolved_title": raw.get("resolved_title"),
        "url": url,
        "query": query,
        "work": work,
        "source_tier": "excluded" if excluded else source_tier("bwiki"),
        "officiality": "excluded" if excluded else "secondary",
        "media_type": "wiki",
        "language": "zh",
        "retrieved_at": TODAY,
        "status": status,
        "warnings": raw.get("warnings", []),
        "attempted_modes": raw.get("attempted_modes") or [raw.get("mode")],
        "retrieved_by": raw.get("retrieved_by"),
        "pageid": raw.get("pageid"),
        "extract_chars": raw.get("extract_chars", len(extract)),
        "content_hash": sha256_text(extract),
        "error": raw.get("error"),
    }


def discover_bwiki(query, work, timeout):
    game = resolve_bwiki_game(work)
    if not game:
        return {
            "id": f"bwiki-{query}".replace(" ", "-"),
            "source": "bwiki",
            "title": None,
            "url": None,
            "query": query,
            "work": work,
            "source_tier": source_tier("bwiki"),
            "officiality": "secondary",
            "media_type": "wiki",
            "language": "zh",
            "retrieved_at": TODAY,
            "status": "failed",
            "error": "unknown_game",
            "warnings": [f"could not resolve BWIKI game slug from work '{work}'; pass --sources bwiki with a known game name"],
            "attempted_modes": ["discover"],
        }
    raw = run_python_script("bwiki_api.py", [query, "--game", game], timeout)
    return normalize_bwiki(raw, query, work)


def discover_hsr_official_story(query, work, timeout):
    raw = run_python_script("hsr_official_api.py", ["summary", "--content-id", "2009"], timeout)
    if raw.get("ok"):
        return {
            "id": "hsr-official-story-index",
            "source": "hsr_official_story",
            "title": raw.get("title") or "剧情任务合集",
            "url": raw.get("url") or HSR_OFFICIAL_STORY_URL,
            "query": query,
            "work": work,
            "source_tier": "highest",
            "officiality": "official",
            "media_type": "official_story_index",
            "language": "zh",
            "retrieved_at": TODAY,
            "status": "ok",
            "task_count": raw.get("task_count"),
            "content_hash": raw.get("content_hash"),
            "warnings": raw.get("warnings", []),
            "retrieved_by": raw.get("retrieved_by"),
        }
    record = hsr_official_story_source(query, work)
    record["warnings"].append(raw.get("message") or raw.get("error") or "official API failed")
    record["status"] = "registered"
    return record


def placeholder_source(source, query, work, reason="adapter_not_implemented"):
    return {
        "id": f"{source}-{query}".replace(" ", "-"),
        "source": source,
        "title": None,
        "url": None,
        "query": query,
        "work": work,
        "source_tier": source_tier(source),
        "officiality": "secondary",
        "media_type": "wiki" if source in {"mediawiki", "fandom", "wikipedia", "bwiki"} else "database",
        "language": None,
        "retrieved_at": TODAY,
        "status": "failed",
        "error": reason,
        "warnings": [f"{source} adapter is not implemented yet; use search skill or user material as fallback and record results"],
        "attempted_modes": ["placeholder"],
    }


def hsr_official_story_source(query, work):
    """Register the user-specified official HSR story index.

    The page is rendered dynamically and may require the browser session.
    Keep it as an explicit highest-tier source until a stable public API is
    available; downstream research records the linked task pages it used.
    """
    return {
        "id": "hsr-official-story-index",
        "source": "hsr_official_story",
        "title": "剧情任务合集",
        "url": HSR_OFFICIAL_STORY_URL,
        "query": query,
        "work": work,
        "source_tier": "highest",
        "officiality": "official",
        "media_type": "official_story_index",
        "language": "zh",
        "retrieved_at": TODAY,
        "status": "registered",
        "warnings": [
            "dynamic MiHoYo WIKI page; inspect the index and linked task pages in a browser",
            "record each linked task page used during distillation",
        ],
        "attempted_modes": ["index_registered"],
    }


def parse_sources(value, work):
    if value:
        return [part.strip() for part in value.split(",") if part.strip()]
    return source_hints_for_work(work)


def load_registry():
    """Load a usable registry; absent or invalid data is blocked by registry_verdict."""
    try:
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("channels"), list):
        return None
    ids = set()
    for channel in data["channels"]:
        if not isinstance(channel, dict) or not isinstance(channel.get("id"), str):
            return None
        if not channel["id"] or channel["id"] in ids:
            return None
        ids.add(channel["id"])
        for key in ("applies_to", "disabled_for"):
            values = channel.get(key, ["*"] if key == "applies_to" else [])
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                return None
    return data


def registry_verdict(registry, source, work):
    """Return (allowed, status, reason) for a requested source id under the registry.

    Missing registries are blocked. Without --work only globally allowed
    channels without per-work restrictions can run. Channels are matched by id, then scoped by
    applies_to (substring match on work names, "*" = generic), then by
    per-work disabled_for, then by global status.
    """
    if registry is None:
        return False, "no_registry", "channel-registry.json missing or invalid; collection blocked"
    for channel in registry.get("channels", []):
        if channel.get("id") != source:
            continue
        status = channel.get("status", "unknown")
        if status not in {"verified", "available"}:
            return False, status, f"channel '{source}' is not verified/available: {status}"
        applies = channel.get("applies_to", ["*"])
        if not work and ("*" not in applies or channel.get("disabled_for")):
            return False, "work_required", f"channel '{source}' requires --work to evaluate scope"
        if "*" not in applies:
            hit = any(
                w and (w.lower() in work.lower() or work.lower() in w.lower())
                for w in applies
            )
            if not hit:
                return (
                    False,
                    "out_of_scope",
                    f"channel '{source}' is registered for {applies}, not for work '{work}'",
                )
        for w in channel.get("disabled_for", []):
            if w and (w.lower() in work.lower() or work.lower() in w.lower()):
                return False, "disabled", channel.get("notes", f"'{source}' disabled for '{work}' by registry")
        return True, status, "registry_match"
    return False, "unregistered", f"channel '{source}' is not registered; add it to channel-registry.json before use"


def registry_block_record(source, query, work, status, reason):
    record = placeholder_source(source, query, work, reason)
    record["error"] = f"channel_{status}"
    record["registry_status"] = status
    record["warnings"] = [f"blocked by channel registry: {reason}"]
    return record


def emit_json(payload):
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    sys.stdout.buffer.write(text.encode("utf-8"))


def main():
    parser = argparse.ArgumentParser(description="Discover CSP character sources through local adapters")
    parser.add_argument("query", help="character name, alias, or page title")
    parser.add_argument("--work", default="", help="work/franchise name")
    parser.add_argument("--mode", default="discover", choices=["discover"], help="discovery mode")
    parser.add_argument("--sources", help="comma-separated source adapters, e.g. moegirl,mediawiki")
    parser.add_argument("--timeout", type=int, default=20, help="per-adapter timeout seconds")
    args = parser.parse_args()

    requested_sources = parse_sources(args.sources, args.work)
    registry = load_registry()
    records = []

    for source in requested_sources:
        allowed, status, reason = registry_verdict(registry, source, args.work)
        if not allowed:
            records.append(registry_block_record(source, args.query, args.work, status, reason))
            continue
        if source == "hsr_official_story":
            records.append(discover_hsr_official_story(args.query, args.work, args.timeout))
        elif source == "moegirl":
            records.append(discover_moegirl(args.query, args.work, args.timeout))
        elif source == "bwiki":
            records.append(discover_bwiki(args.query, args.work, args.timeout))
        elif source == "official":
            records.append(placeholder_source(source, args.query, args.work, "manual_or_official_material_required"))
        else:
            records.append(placeholder_source(source, args.query, args.work))

    result = {
        "ok": any(record.get("status") in {"ok", "registered"} for record in records),
        "query": args.query,
        "work": args.work,
        "mode": args.mode,
        "retrieved_at": TODAY,
        "cross_media_hint": is_cross_media_work(args.work),
        "registry_applied": registry is not None,
        "sources_requested": requested_sources,
        "records": records,
        "warnings": []
        if any(record.get("status") in {"ok", "registered"} for record in records)
        else ["no local adapter returned a successful source"],
    }
    if registry is None:
        result["warnings"].append(
            f"channel registry missing or invalid at {REGISTRY_PATH}; all requested channels were blocked"
        )

    emit_json(result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
