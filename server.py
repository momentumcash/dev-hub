"""Dev Hub: a private, standard-library-only desk for game makers."""
import json
import mimetypes
import os
import re
import tempfile
import threading
import time
import uuid
import sys
import webbrowser
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
ROOT_ID = __import__('hashlib').sha1(str(ROOT).encode()).hexdigest()[:16]
SORTS = ('up-and-coming', 'top-trending', 'top-earning')
MAX_DATA = 5 * 1024 * 1024
MAX_UPLOAD = 8 * 1024 * 1024


def fetch_json(url):
    with urlopen(Request(url, headers={'User-Agent': 'DevHub/1.0'}), timeout=12) as response:
        return json.load(response)


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=str(path.parent), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(value, out, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, str(path))
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def valid_data(value):
    if not isinstance(value, dict) or not ({'games', 'inspo'} <= set(value) <= {'games', 'inspo', 'rev'}):
        return False
    if 'rev' in value and (not isinstance(value['rev'], int) or isinstance(value['rev'], bool) or value['rev'] < 0):
        return False
    for key in ('games', 'inspo'):
        if not isinstance(value[key], list) or len(value[key]) > 5000:
            return False
        ids = set()
        for item in value[key]:
            if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id'] or item['id'] in ids:
                return False
            ids.add(item['id'])
            for field in ('name', 'title', 'next', 'notes', 'url', 'cover', 'image', 'stage', 'gameId'):
                if field in item and (not isinstance(item[field], str) or len(item[field]) > 100000):
                    return False
            if 'tags' in item and (not isinstance(item['tags'], list) or any(not isinstance(t, str) or len(t) > 100 for t in item['tags'])):
                return False
            if 'stats' in item and item['stats'] is not None:
                if not isinstance(item['stats'], dict):
                    return False
                pct = item['stats'].get('likePct')
                if pct is not None and (isinstance(pct, bool) or not isinstance(pct, (int, float)) or not 0 <= pct <= 100):
                    return False
            if key == 'games' and (not item.get('name') or item.get('stage', 'Idea') not in ('Idea', 'Building', 'Testing', 'Live')):
                return False
    return True


class Hub:
    def __init__(self, data_dir=None, fetcher=None):
        self.data = Path(data_dir or ROOT / 'data')
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / 'uploads').mkdir(exist_ok=True)
        self.fetch = fetcher or fetch_json
        self.lock = threading.RLock()
        self.tlock = threading.Lock()  # trends fetches never block saves
        self.cache = read_json(self.data / 'trends-cache.json')
        if not (self.data / 'hub.json').exists():
            atomic_json(self.data / 'hub.json', read_json(self.data / 'example.json', read_json(ROOT / 'data/example.json', {'games': [], 'inspo': []})))

    def enrich(self, rows):
        rows = [dict(x) for x in rows if not x.get('isSponsored') and x.get('universeId')]
        ids = list(dict.fromkeys(str(x['universeId']) for x in rows))
        info, icons, large = {}, {}, {}
        for start in range(0, len(ids), 50):
            csv = ','.join(ids[start:start + 50])
            for game in self.fetch('https://games.roblox.com/v1/games?universeIds=' + csv).get('data', []):
                info[str(game['id'])] = game
            for icon in self.fetch('https://thumbnails.roblox.com/v1/games/icons?universeIds=' + csv + '&returnPolicy=PlaceHolder&size=150x150&format=Png&isCircular=false').get('data', []):
                icons[str(icon['targetId'])] = icon.get('imageUrl', '')
            try:
                for icon in self.fetch('https://thumbnails.roblox.com/v1/games/icons?universeIds=' + csv + '&returnPolicy=PlaceHolder&size=512x512&format=Png&isCircular=false').get('data', []):
                    large[str(icon['targetId'])] = icon.get('imageUrl', '')
            except Exception:
                pass
        result = []
        for row in rows:
            uid = str(row['universeId'])
            detail = info.get(uid, {})
            place = detail.get('rootPlaceId') or row.get('rootPlaceId') or row.get('placeId')
            up, down = row.get('totalUpVotes', row.get('upVotes', 0)) or 0, row.get('totalDownVotes', row.get('downVotes', 0)) or 0
            result.append({'universeId': row['universeId'], 'placeId': place, 'name': detail.get('name', row.get('name', 'Untitled game')), 'playing': detail.get('playing', row.get('playerCount', row.get('playing', 0))), 'likePct': round(100 * up / (up + down)) if up + down else (round(row['likeRatio'] * 100) if isinstance(row.get('likeRatio'), (int, float)) else row.get('likePct')), 'created': detail.get('created'), 'visits': detail.get('visits', row.get('visits', 0)), 'genre': detail.get('genre_l1') or row.get('genreL1') or (detail.get('genre') if detail.get('genre') not in (None, 'All') else None) or 'Other', 'icon': icons.get(uid, ''), 'iconLarge': large.get(uid) or icons.get(uid, ''), 'url': 'https://www.roblox.com/games/' + str(place) if place else '', 'likes': up if up + down else None})
        return result

    def trends(self, refresh=False):
        with self.tlock:
            now = time.time()
            if self.cache and now - self.cache.get('updated', 0) < 1200 and not refresh:
                return dict(self.cache, stale=False)
            try:
                lists = {}
                for sort in SORTS:
                    raw = self.fetch('https://apis.roblox.com/explore-api/v1/get-sort-content?sortId=' + sort + '&sessionId=x&device=all&country=all')
                    lists[sort] = self.enrich(raw.get('games', raw.get('data', [])))
                previous = read_json(self.data / 'trends-prev.json', {}) or {}
                # The baseline rotates by ITS OWN age, so movement compares against roughly a day ago.
                if self.cache and now - previous.get('updated', 0) > 20 * 3600:
                    previous = self.cache
                    atomic_json(self.data / 'trends-prev.json', previous)
                for sort, rows in lists.items():
                    ranks = {str(r['universeId']): n + 1 for n, r in enumerate(previous.get('lists', {}).get(sort, []))}
                    for n, row in enumerate(rows):
                        old = ranks.get(str(row['universeId']))
                        row['movement'] = old - n - 1 if old else 'NEW'
                self.cache = {'updated': now, 'lists': lists}
                atomic_json(self.data / 'trends-cache.json', self.cache)
                return dict(self.cache, stale=False)
            except Exception:
                cached = self.cache or read_json(self.data / 'trends-cache.json')
                return dict(cached, stale=True) if cached else {'error': 'offline'}

    def game(self, value):
        if value.isdigit():
            place = value
        else:
            parsed = urlsplit(value)
            match = re.fullmatch(r'/games/(\d+)(?:/[^?]*)?', parsed.path)
            if parsed.scheme != 'https' or parsed.hostname not in ('roblox.com', 'www.roblox.com') or not match:
                raise ValueError('Use a Roblox game link or place ID.')
            place = match.group(1)
        uid = self.fetch('https://apis.roblox.com/universes/v1/places/' + place + '/universe')['universeId']
        row = {'universeId': uid, 'placeId': int(place)}
        try:
            votes = (self.fetch('https://games.roblox.com/v1/games/votes?universeIds=' + str(uid)).get('data') or [{}])[0]
            row['upVotes'], row['downVotes'] = votes.get('upVotes', 0) or 0, votes.get('downVotes', 0) or 0
        except Exception:
            pass
        return self.enrich([row])[0]


def make_server(port=8765, data_dir=None, fetcher=None, bind_and_activate=True):
    hub = Hub(data_dir, fetcher)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, value, content_type='application/json'):
            body = json.dumps(value).encode() if content_type == 'application/json' else value
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Dev-Hub', ROOT_ID)
            self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' data: https://*.rbxcdn.com https://*.roblox.com; style-src 'self' 'unsafe-inline' https://api.fontshare.com; font-src 'self' https://cdn.fontshare.com; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def safe_request(self):
            host = self.headers.get('Host', '')
            origin = self.headers.get('Origin')
            return host in ('127.0.0.1:' + str(self.server.server_port), 'localhost:' + str(self.server.server_port)) and (not origin or origin in ('http://' + host,))

        def do_GET(self):
            if not self.safe_request():
                return self.send(403, {'error': 'Local requests only.'})
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
            if '\\' in path or '\x00' in path or '..' in path.split('/'):
                return self.send(403, {'error': 'Path blocked.'})
            query = parse_qs(parsed.query)
            if path == '/api/data':
                with hub.lock:
                    current = read_json(hub.data / 'hub.json') or {'games': [], 'inspo': []}
                    current.setdefault('rev', 0)
                    return self.send(200, current)
            if path == '/api/trends':
                return self.send(200, hub.trends(query.get('refresh') == ['1']))
            if path == '/api/game':
                try:
                    return self.send(200, hub.game(query.get('url', [''])[0]))
                except ValueError as error:
                    return self.send(400, {'error': str(error)})
                except Exception:
                    return self.send(503, {'error': 'Roblox is offline. Try again later.'})
            base = hub.data / 'uploads' if path.startswith('/uploads/') else ROOT / 'app'
            relative = path[len('/uploads/'):] if path.startswith('/uploads/') else path.lstrip('/') or 'index.html'
            target = (base / relative).resolve()
            if base.resolve() not in target.parents or not target.is_file():
                return self.send(404, {'error': 'Not found.'})
            return self.send(200, target.read_bytes(), mimetypes.guess_type(str(target))[0] or 'application/octet-stream')

        def body(self, limit):
            try:
                size = int(self.headers.get('Content-Length', '-1'))
            except ValueError:
                size = -1
            if size < 0:
                self.send(400, {'error': 'A body length is required.'})
                return None
            if size > limit:
                self.send(413, {'error': 'That file is too big.'})
                return None
            return self.rfile.read(size)

        def do_PUT(self):
            if not self.safe_request():
                return self.send(403, {'error': 'Local requests only.'})
            if self.path != '/api/data':
                return self.send(404, {'error': 'Not found.'})
            raw = self.body(MAX_DATA)
            if raw is None:
                return
            try:
                value = json.loads(raw)
                if not valid_data(value):
                    raise ValueError()
            except (ValueError, UnicodeError):
                return self.send(400, {'error': 'Use a games list and an inspo list with valid items.'})
            try:
                with hub.lock:
                    current = read_json(hub.data / 'hub.json') or {}
                    rev = current.get('rev', 0)
                    # A tab working from an older copy must reload first, so it can't wipe newer changes.
                    if 'rev' in value and value['rev'] != rev:
                        current.setdefault('rev', rev)
                        return self.send(409, {'error': 'Updated from another tab, try again.', 'data': current})
                    value['rev'] = rev + 1
                    atomic_json(hub.data / 'hub.json', value)
            except OSError:
                return self.send(500, {'error': 'Could not save to this folder. Check its permissions and free space.'})
            self.send(200, {'ok': True, 'rev': value['rev']})

        def do_POST(self):
            if not self.safe_request():
                return self.send(403, {'error': 'Local requests only.'})
            if self.path != '/api/upload':
                return self.send(404, {'error': 'Not found.'})
            kind = self.headers.get('Content-Type', '').split(';')[0]
            extensions = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp', 'image/gif': 'gif'}
            if kind not in extensions:
                return self.send(415, {'error': 'Choose a PNG, JPEG, WebP or GIF image.'})
            raw = self.body(MAX_UPLOAD)
            if raw is None:
                return
            signatures = {'image/png': raw.startswith(b'\x89PNG\r\n\x1a\n'), 'image/jpeg': raw.startswith(b'\xff\xd8\xff'), 'image/gif': raw.startswith((b'GIF87a', b'GIF89a')), 'image/webp': raw.startswith(b'RIFF') and raw[8:12] == b'WEBP'}
            if not signatures[kind]:
                return self.send(400, {'error': 'That file does not look like an image.'})
            name = uuid.uuid4().hex + '.' + extensions[kind]
            (hub.data / 'uploads' / name).write_bytes(raw)
            self.send(201, {'url': '/uploads/' + name})

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler, bind_and_activate=bind_and_activate)
    if not bind_and_activate:
        server.server_port = port
    server.hub = hub
    return server


def running_copy():
    for port in range(8765, 8776):
        try:
            with urlopen('http://127.0.0.1:%d/' % port, timeout=0.4) as response:
                if response.headers.get('X-Dev-Hub') == ROOT_ID:
                    return 'http://127.0.0.1:%d' % port
        except Exception:
            continue
    return None


def main():
    existing = running_copy()
    if existing:
        print('Dev Hub is already open at ' + existing + '. Opening it.', flush=True)
        if '--no-browser' not in sys.argv:
            webbrowser.open(existing)
        return
    for port in range(8765, 8776):
        try:
            server = make_server(port)
            break
        except PermissionError:
            print('Dev Hub cannot open a local port here. Run it from a normal terminal window.', flush=True)
            return
        except OSError:
            continue
    else:
        print('Dev Hub could not find an open port. Close another copy and try again.')
        return
    url = 'http://127.0.0.1:' + str(server.server_port)
    print('Dev Hub is running at ' + url + ', keep this window open.', flush=True)
    if '--no-browser' not in sys.argv:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
