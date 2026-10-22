"""Per-game URL policy and immutable HTTP evidence for the portable skill."""
import hashlib
import io
import json
import threading
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path


def load_game(config, name):
    data = json.loads(Path(config).read_text(encoding='utf-8'))
    for key, game in data['games'].items():
        if name in [key, game['name'], *game.get('aliases', [])]:
            return key, game
    raise ValueError('Unknown game: ' + name)


def source_for_url(game, url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError('Only configured HTTPS sources are allowed: ' + url)
    path = urllib.parse.unquote(parsed.path)
    if '\\' in path or any(part in ('.', '..') for part in path.split('/')):
        raise ValueError('Ambiguous URL path: ' + url)
    for source in game['sources']:
        if not source.get('enabled', True):
            continue
        for prefix in source['url_prefixes']:
            allowed = urllib.parse.urlsplit(prefix)
            base = urllib.parse.unquote(allowed.path).rstrip('/')
            if parsed.hostname == allowed.hostname and (path == base or path.startswith(base + '/')):
                return source
    raise ValueError('URL is not in this game configuration: ' + url)


class EvidencePolicy:
    """Wrap OpenerDirector.open, including custom openers and redirects.

    This governs urllib calls made in this runner, not arbitrary browser tools.
    """
    def __init__(self, game, folder, max_bytes=64 * 1024 * 1024):
        self.game = game
        self.folder = Path(folder)
        self.max_bytes = max_bytes
        self.lock = threading.Lock()
        self.errors = 0

    def record(self, url, request, body=None, response=None, error=None):
        source = source_for_url(self.game, url)
        record = {'url': url, 'source_id': source['id'],
                  'source_type': source['type'],
                  'retrieved_at': datetime.now(timezone.utc).isoformat(),
                  'method': request.get_method() if isinstance(request, urllib.request.Request) else 'GET',
                  'status': 'failed' if error else 'downloaded_unreviewed'}
        if isinstance(request, urllib.request.Request) and request.data:
            record['request_body_sha256'] = hashlib.sha256(request.data).hexdigest()
        self.folder.mkdir(parents=True, exist_ok=True)
        if body is not None:
            digest = hashlib.sha256(body).hexdigest()
            raw = self.folder / (digest + '.raw')
            with self.lock:
                if not raw.exists():
                    raw.write_bytes(body)
            record.update(content_sha256=digest, raw_file=raw.name, bytes=len(body))
        if response is not None:
            record.update(http_status=response.status, final_url=response.geturl(),
                          content_type=response.headers.get('Content-Type', ''))
        if error:
            record['error'] = str(error)
            with self.lock:
                self.errors += 1
        # Individual files avoid concurrent JSONL appends corrupting evidence.
        (self.folder / (uuid.uuid4().hex + '.json')).write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    def __enter__(self):
        self.original = urllib.request.OpenerDirector.open
        policy = self

        def guarded(opener, request, *args, **kwargs):
            url = request.full_url if isinstance(request, urllib.request.Request) else request
            source_for_url(policy.game, url)
            response = None
            try:
                response = policy.original(opener, request, *args, **kwargs)
                source_for_url(policy.game, response.geturl())
                body = response.read(policy.max_bytes + 1)
                if len(body) > policy.max_bytes:
                    raise ValueError('Response exceeds 64 MiB; fetch individual data files.')
                policy.record(url, request, body, response)
                snapshot = io.BytesIO(body)
                snapshot.headers = response.headers
                snapshot.status = response.status
                snapshot.code = response.code
                snapshot.url = response.geturl()
                snapshot.geturl = lambda: snapshot.url
                snapshot.info = lambda: snapshot.headers
                snapshot.getcode = lambda: snapshot.code
                return snapshot
            except Exception as exc:
                policy.record(url, request, error=exc)
                raise
            finally:
                if response is not None:
                    response.close()

        urllib.request.OpenerDirector.open = guarded
        return self

    def __exit__(self, *unused):
        urllib.request.OpenerDirector.open = self.original
