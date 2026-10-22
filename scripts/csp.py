"""Portable CSP: initialize a workspace, select sources, fetch, or run adapters."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import urllib.request
import uuid
from datetime import datetime, timezone

from site_access import EvidencePolicy, load_game, source_for_url

PACKAGE = Path(__file__).resolve().parents[1]
ADAPTERS = {'genshin': {'genshin_yatta_api'}, 'hsr': {'hsr_official_api', 'bwiki_api'},
            'zzz': {'zzz_wiki_api'}, 'wuwa': {'mc_wiki_api'},
            'arknights': {'bwiki_api', 'moegirl_api'}, 'endfield': {'endfield_wiki_api'}}


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def initialize(target):
    target = Path(target).expanduser().resolve()
    if target == PACKAGE or PACKAGE in target.parents or target in PACKAGE.parents:
        raise ValueError('Choose a workspace separate from the installed skill.')
    if target.exists() and any(target.iterdir()):
        raise ValueError('Workspace must be new or empty; existing files are never overwritten.')
    target.mkdir(parents=True, exist_ok=True)
    for folder in ('scripts', 'references'):
        shutil.copytree(PACKAGE / folder, target / folder,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copy2(PACKAGE / 'references/SKILL_V2.md', target / 'SKILL_V2.md')
    shutil.copy2(PACKAGE / 'requirements.txt', target / 'requirements.txt')
    for name in ('examples', 'runs', 'backups'):
        (target / name).mkdir()
    (target / 'csp-workspace.json').write_text(json.dumps({
        'schema_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
        'standard_sha256': hashlib.sha256((target / 'SKILL_V2.md').read_bytes()).hexdigest(),
        'source_config': 'references/game-sources.json'}, indent=2) + '\n', encoding='utf-8')
    (target / '.gitignore').write_text('examples/\nruns/\nbackups/\n__pycache__/\n', encoding='utf-8')
    emit({'workspace': str(target), 'next': 'Read references/workflow.md, then select a game.'})


def workspace(path):
    root = Path(path).expanduser().resolve()
    if not (root / 'csp-workspace.json').is_file():
        raise ValueError('Run init first; this is not a portable CSP workspace.')
    return root


def import_material(root, game, filename, source_url=None, source_note=None, retrieved_at=None):
    if not source_url and not source_note:
        raise ValueError('Supply a source URL or a provenance note for local material.')
    source = source_for_url(game, source_url) if source_url else None
    path = Path(filename).expanduser().resolve()
    body = path.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    folder = root / 'examples' / game['name'] / '_corpus/imported'
    folder.mkdir(parents=True, exist_ok=True)
    raw = folder / (digest + '.raw')
    if not raw.exists():
        raw.write_bytes(body)
    record = {'status': 'imported_unreviewed', 'source_url': source_url,
              'source_id': source['id'] if source else 'user_material',
              'source_type': source['type'] if source else 'user_provided_unverified',
              'provenance_note': source_note, 'original_filename': path.name,
              'retrieved_at': retrieved_at,
              'imported_at': datetime.now(timezone.utc).isoformat(),
              'content_sha256': digest, 'raw_file': raw.name, 'bytes': len(body)}
    (folder / (uuid.uuid4().hex + '.json')).write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    emit(record)
    return record


def run_adapter(root, game_key, game, adapter, arguments):
    adapter = adapter.removesuffix('.py')
    if adapter not in ADAPTERS[game_key]:
        raise ValueError('Adapter is not available for this game: ' + adapter)
    if adapter == 'zzz_wiki_api' and importlib.util.find_spec('bs4') is None:
        raise ValueError('ZZZ needs beautifulsoup4. Install this workspace requirements.txt.')
    if not arguments:
        raise ValueError('Adapter command is required; inspect references/games first.')
    # Adapters write relative paths into a self-contained workspace, never the skill installation.
    os.chdir(root)
    sys.path.insert(0, str(root / 'scripts'))
    sys.argv = [str(root / 'scripts' / (adapter + '.py')), *arguments]
    with EvidencePolicy(game, root / 'examples' / game['name'] / '_corpus/http') as policy:
        try:
            runpy.run_path(sys.argv[0], run_name='__main__')
            result = 0
        except SystemExit as exc:
            result = exc.code if isinstance(exc.code, int) else (1 if exc.code else 0)
    # Some legacy crawlers print errors yet return zero. Surface failed requests conservatively.
    return result or (1 if policy.errors else 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init')
    init.add_argument('--workspace', required=True)
    sub.add_parser('doctor')
    sources = sub.add_parser('sources')
    sources.add_argument('--game', required=True)
    sources.add_argument('--workspace')
    check = sub.add_parser('check-url')
    check.add_argument('--game', required=True)
    check.add_argument('--workspace', required=True)
    check.add_argument('--url', required=True)
    fetch = sub.add_parser('fetch')
    fetch.add_argument('--game', required=True)
    fetch.add_argument('--workspace', required=True)
    fetch.add_argument('--url', required=True)
    fetch.add_argument('--json-file', help='POST body file; only use documented read-only API queries')
    fetch.add_argument('--timeout', type=int, default=30)
    imported = sub.add_parser('import-file')
    imported.add_argument('--game', required=True)
    imported.add_argument('--workspace', required=True)
    imported.add_argument('--file', required=True)
    imported.add_argument('--source-url')
    imported.add_argument('--source-note')
    imported.add_argument('--retrieved-at', help='Actual original retrieval date if known; not today by default')
    run = sub.add_parser('run')
    run.add_argument('--game', required=True)
    run.add_argument('--workspace', required=True)
    run.add_argument('adapter')
    run.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command == 'init':
        initialize(args.workspace)
        return 0
    if args.command == 'doctor':
        checks = {'python': sys.version.split()[0], 'python_supported': sys.version_info >= (3, 10),
                  'zzz_html_parser_available': importlib.util.find_spec('bs4') is not None,
                  'shared_standard_exists': (PACKAGE / 'references/SKILL_V2.md').is_file()}
        emit(checks)
        code = subprocess.call([sys.executable, '-B', str(PACKAGE / 'scripts/preflight_check.py')])
        return code or (0 if all(v for k, v in checks.items() if k != 'python') else 1)
    root = workspace(args.workspace) if args.workspace else PACKAGE
    key, game = load_game(root / 'references/game-sources.json', args.game)
    if args.command == 'sources':
        emit({'game_id': key, **game})
    elif args.command == 'check-url':
        emit(source_for_url(game, args.url))
    elif args.command == 'import-file':
        import_material(root, game, args.file, args.source_url, args.source_note, args.retrieved_at)
    elif args.command == 'fetch':
        body = None
        if args.json_file:
            body = json.dumps(json.loads(Path(args.json_file).read_text(encoding='utf-8'))).encode('utf-8')
        req = urllib.request.Request(args.url, data=body,
                                     headers={'User-Agent': 'CharacterSkillProducer/portable',
                                              'Content-Type': 'application/json'})
        folder = root / 'examples' / game['name'] / '_corpus/http'
        with EvidencePolicy(game, folder):
            with urllib.request.urlopen(req, timeout=args.timeout) as response:
                payload = response.read()
        emit({'bytes': len(payload), 'evidence_directory': str(folder),
              'status': 'downloaded_unreviewed', 'note': 'Inspect content before treating it as character evidence.'})
    else:
        return run_adapter(root, key, game, args.adapter, args.arguments)
    return 0


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    try:
        sys.exit(main())
    except (ValueError, OSError, KeyError) as error:
        print('CSP ERROR: ' + str(error), file=sys.stderr)
        sys.exit(1)
