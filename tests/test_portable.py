"""Portable runtime tests; no live websites or model calls."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.request
import urllib.response
from email.message import Message

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import csp
from site_access import EvidencePolicy, load_game, source_for_url


class FakeHTTPS(urllib.request.HTTPSHandler):
    def __init__(self, redirect=None):
        super().__init__()
        self.seen = []
        self.redirect = redirect

    def https_open(self, request):
        self.seen.append(request.full_url)
        headers = Message()
        headers['Content-Type'] = 'application/json'
        if self.redirect:
            headers['Location'] = self.redirect
        response = urllib.response.addinfourl(io.BytesIO(b'{"fixture":true}'), headers,
                                              request.full_url, 302 if self.redirect else 200)
        response.msg = 'Found' if self.redirect else 'OK'
        return response


class PortableTests(unittest.TestCase):
    def test_all_six_games_have_valid_guides_and_sources(self):
        data = json.loads((ROOT / 'references/game-sources.json').read_text(encoding='utf-8'))
        self.assertEqual(len(data['games']), 6)
        for key, game in data['games'].items():
            self.assertTrue((ROOT / 'references' / game['guide']).is_file())
            self.assertEqual(load_game(ROOT / 'references/game-sources.json', game['name'])[0], key)
            for source in game['sources']:
                for url in source['url_prefixes']:
                    self.assertEqual(source_for_url(game, url)['id'], source['id'])

    def test_other_game_and_lookalike_urls_rejected(self):
        _, game = load_game(ROOT / 'references/game-sources.json', '崩铁')
        for url in ['https://wiki.biligame.com/ys/api.php',
                    'https://wiki.biligame.com.evil.example/sr/api.php',
                    'http://wiki.biligame.com/sr/api.php',
                    'https://wiki.biligame.com/sr/%2e%2e/ys/api.php',
                    'https://wiki.biligame.com/sr-extra/api.php',
                    'https://user:pass@wiki.biligame.com/sr/api.php']:
            with self.assertRaises(ValueError):
                source_for_url(game, url)

    def test_disabled_source_and_new_config(self):
        game = {'sources': [{'id': 'custom', 'type': 'user_designated',
                             'url_prefixes': ['https://example.org/text/'], 'enabled': False}]}
        with self.assertRaises(ValueError):
            source_for_url(game, 'https://example.org/text/1')
        game['sources'][0]['enabled'] = True
        self.assertEqual(source_for_url(game, 'https://example.org/text/1')['id'], 'custom')

    def test_custom_opener_preserves_raw_and_metadata(self):
        _, game = load_game(ROOT / 'references/game-sources.json', '原神')
        handler = FakeHTTPS()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), handler)
        with tempfile.TemporaryDirectory() as td:
            with EvidencePolicy(game, td):
                with opener.open('https://gi.yatta.moe/api/v2/chs/avatar') as response:
                    self.assertEqual(json.loads(response.read()), {'fixture': True})
            records = [json.loads(p.read_text(encoding='utf-8')) for p in Path(td).glob('*.json')]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]['source_type'], 'third_party_game_data')
            raw = Path(td) / records[0]['raw_file']
            self.assertEqual(hashlib.sha256(raw.read_bytes()).hexdigest(), records[0]['content_sha256'])

    def test_cross_site_redirect_stopped_before_second_request(self):
        _, game = load_game(ROOT / 'references/game-sources.json', '原神')
        handler = FakeHTTPS('https://example.org/other')
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), handler)
        with tempfile.TemporaryDirectory() as td:
            with EvidencePolicy(game, td) as policy:
                with self.assertRaises(ValueError):
                    opener.open('https://gi.yatta.moe/redirect')
            self.assertEqual(len(handler.seen), 1)
            self.assertEqual(policy.errors, 1)

    def test_init_does_not_overwrite_existing_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / '工作 space'
            with contextlib.redirect_stdout(io.StringIO()):
                csp.initialize(root)
            self.assertEqual((root / 'SKILL_V2.md').read_bytes(), (ROOT / 'references/SKILL_V2.md').read_bytes())
            sentinel = root / 'examples/keep.txt'
            sentinel.write_text('keep', encoding='utf-8')
            with self.assertRaises(ValueError):
                csp.initialize(root)
            self.assertEqual(sentinel.read_text(encoding='utf-8'), 'keep')

    def test_import_preserves_bytes_and_unknown_original_date(self):
        _, game = load_game(ROOT / 'references/game-sources.json', '绝区零')
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            original = root / 'original.txt'
            original.write_bytes(b'local original fixture')
            with contextlib.redirect_stdout(io.StringIO()):
                record = csp.import_material(root, game, original, source_note='user supplied; version unknown')
            self.assertIsNone(record['retrieved_at'])
            self.assertEqual(record['source_type'], 'user_provided_unverified')
            self.assertEqual((root / 'examples/绝区零/_corpus/imported' / record['raw_file']).read_bytes(), original.read_bytes())
            with self.assertRaises(ValueError):
                csp.import_material(root, game, original, source_url='https://gi.yatta.moe/')
            with self.assertRaises(ValueError):
                csp.import_material(root, game, original)

    def test_moved_package_boots_and_question_prompts_stay_in_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            temp = Path(td)
            installed = temp / '不同目录 portable skill'
            shutil.copytree(ROOT, installed, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            work = temp / '新工作区 space'
            env = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
            def call(script, *args):
                result = subprocess.run([sys.executable, '-B', str(script), *map(str, args)],
                                        cwd=temp, env=env, capture_output=True, text=True, encoding='utf-8')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result
            before = {p.relative_to(installed).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in installed.rglob('*') if p.is_file()}
            call(installed / 'scripts/csp.py', 'init', '--workspace', work)
            call(work / 'scripts/preflight_check.py')
            skill = work / 'examples/原神/genshin-fixture-skill'
            prompts = skill / 'references/subagent-prompts'
            prompts.mkdir(parents=True)
            (skill / 'SKILL.md').write_text('Synthetic character instructions.', encoding='utf-8')
            for qid in ('R1', 'R2', 'E1', 'E2', 'K1'):
                (prompts / ('answer-' + qid + '.md')).write_text(
                    '===== 角色 Skill 结束 =====\n' + qid + ': actual fixture question', encoding='utf-8')
            mutdir = work / 'runs/mutation' / skill.name
            mutdir.mkdir(parents=True)
            (mutdir / 'SKILL-mutation-generic.md').write_text('Mutated fixture.', encoding='utf-8')
            call(work / 'scripts/v2_collect.py', 'answer-all', skill)
            call(work / 'scripts/v2_collect.py', 'mutation-answer-all', skill, 'generic')
            for prompt in [work / 'runs/behavior' / skill.name / 'answer-all.md',
                           mutdir / 'answer-prompts/generic.md']:
                body = prompt.read_text(encoding='utf-8')
                for qid in ('R1', 'R2', 'E1', 'E2', 'K1'):
                    self.assertIn(qid + ': actual fixture question', body)
                self.assertIn(str(work), body)
                self.assertNotIn('Character_Skill_Producer-main', body)
            after = {p.relative_to(installed).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in installed.rglob('*') if p.is_file()}
            self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
