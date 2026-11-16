"""Offline contract tests; never contacts Roblox or opens a browser."""
import base64
import http.client
import json
import io
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from server import MAX_DATA, MAX_UPLOAD, SORTS, Hub, main, make_server

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')


class FakeFetcher:
    def __init__(self):
        self.calls = []
        self.offline = False

    def __call__(self, url):
        self.calls.append(url)
        if self.offline:
            raise OSError('offline')
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        if '/places/' in url:
            return {'universeId': 101}
        if 'get-sort-content' in url:
            return {'games': [{'universeId': 101, 'rootPlaceId': 201, 'name': 'EXAMPLE – Test world', 'playerCount': 42, 'totalUpVotes': 90, 'totalDownVotes': 10}, {'universeId': 999, 'isSponsored': True}]}
        ids = query.get('universeIds', [''])[0].split(',')
        assert len(ids) <= 50
        if 'thumbnails' in url:
            return {'data': [{'targetId': int(uid), 'imageUrl': 'https://example.rbxcdn.com/icon.png'} for uid in ids]}
        if '/games/votes' in url:
            return {'data': [{'id': int(uid), 'upVotes': 75, 'downVotes': 25} for uid in ids]}
        if 'games.roblox.com' in url:
            return {'data': [{'id': int(uid), 'rootPlaceId': 201, 'name': 'EXAMPLE – Test world', 'playing': 42, 'visits': 12345, 'created': '2026-11-01T00:00:00Z', 'genre': 'Adventure'} for uid in ids]}
        raise AssertionError('Unexpected URL: ' + url)


class Smoke(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fake = FakeFetcher()
        self.in_memory = False
        try:
            self.server = make_server(0, self.temp.name, self.fake)
        except PermissionError:
            self.in_memory = True
            self.server = make_server(8765, self.temp.name, self.fake, bind_and_activate=False)
        if not self.in_memory:
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()

    def tearDown(self):
        if not self.in_memory:
            self.server.shutdown()
            self.thread.join()
        self.server.server_close()
        self.temp.cleanup()

    def request(self, method, path, value=None, headers=None):
        if self.in_memory:
            body = value.encode() if isinstance(value, str) else value or b''
            request_headers = {'Host': '127.0.0.1:' + str(self.server.server_port), 'Content-Length': str(len(body))}
            request_headers.update(headers or {})
            raw = (method + ' ' + path + ' HTTP/1.0\r\n' + ''.join(k + ': ' + v + '\r\n' for k, v in request_headers.items()) + '\r\n').encode() + body
            class MemorySocket:
                def __init__(self):
                    self.output = bytearray()
                def makefile(self, *args):
                    return io.BytesIO(raw)
                def sendall(self, chunk):
                    self.output.extend(chunk)
            socket = MemorySocket()
            thread = threading.Thread(target=self.server.RequestHandlerClass, args=(socket, ('127.0.0.1', 1), self.server))
            thread.start()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            head, body = bytes(socket.output).split(b'\r\n\r\n', 1)
            status = int(head.split(b' ')[1])
            return status, json.loads(body) if b'Content-Type: application/json' in head else body
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        connection.request(method, path, body=value, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        content_type = response.getheader('Content-Type', '')
        status = response.status
        connection.close()
        return status, json.loads(body) if content_type == 'application/json' else body

    def test_assets_and_traversal(self):
        for path in ('/', '/app.js', '/app.css', '/art/obby.jpg', '/art/pets.jpg', '/art/bg.jpg', '/tokens.css'):
            self.assertEqual(self.request('GET', path)[0], 200)
        for path in ('/uploads/../server.py', '/../server.py', '/%2e%2e/server.py', '/uploads/%2e%2e%2fhub.json', '/uploads/..%5cserver.py', '/server.py', '/missing'):
            self.assertIn(self.request('GET', path)[0], (403, 404))
        self.assertEqual(self.request('GET', '/api/data', headers={'Host': 'other.invalid'})[0], 403)

    def test_save_reload_and_shape(self):
        status, original = self.request('GET', '/api/data')
        self.assertEqual(status, 200)
        self.assertEqual(len(original['games']), 2)
        original['games'].append({'id': 'my-game', 'name': 'A brand new idea', 'stage': 'Idea', 'next': 'First jump'})
        self.assertEqual(self.request('PUT', '/api/data', json.dumps(original))[0], 200)
        original['rev'] = original.get('rev', 0) + 1
        self.assertEqual(self.request('GET', '/api/data')[1], original)
        self.assertEqual(json.loads((Path(self.temp.name) / 'hub.json').read_text()), original)
        self.assertEqual(json.loads((Hub(self.temp.name, self.fake).data / 'hub.json').read_text()), original)
        for invalid in ({}, {'games': {}, 'inspo': []}, {'games': [{'id': 'x', 'name': 'x', 'stage': 'wrong'}], 'inspo': []}, {'games': [], 'inspo': [{'id': 'x', 'tags': [1]}]}):
            self.assertEqual(self.request('PUT', '/api/data', json.dumps(invalid))[0], 400)
        self.assertEqual(self.request('PUT', '/api/data', '{broken')[0], 400)
        self.assertEqual(self.request('PUT', '/api/data', b'', {'Content-Length': str(MAX_DATA + 1)})[0], 413)
        self.assertEqual(self.request('PUT', '/api/data', '{}', {'Origin': 'https://other.invalid'})[0], 403)
        self.assertEqual(self.request('GET', '/api/data')[1], original)

    def test_upload_and_persist_inspo(self):
        status, result = self.request('POST', '/api/upload', PNG, {'Content-Type': 'image/png'})
        self.assertEqual(status, 201)
        self.assertEqual(self.request('GET', result['url']), (200, PNG))
        self.assertEqual(len(list((Path(self.temp.name) / 'uploads').iterdir())), 1)
        data = self.request('GET', '/api/data')[1]
        data['inspo'].append({'id': 'pasted', 'title': 'Pasted image', 'image': result['url'], 'tags': ['test']})
        self.assertEqual(self.request('PUT', '/api/data', json.dumps(data))[0], 200)
        self.assertEqual(self.request('GET', '/api/data')[1]['inspo'][-1]['image'], result['url'])
        self.assertEqual(self.request('POST', '/api/upload', b'', {'Content-Type': 'image/png', 'Content-Length': str(MAX_UPLOAD + 1)})[0], 413)
        self.assertEqual(self.request('POST', '/api/upload', b'bad', {'Content-Type': 'image/png'})[0], 400)
        self.assertEqual(self.request('POST', '/api/upload', b'bad', {'Content-Type': 'text/html'})[0], 415)

    def test_trends_enrichment_cache_and_offline(self):
        status, result = self.request('GET', '/api/trends')
        self.assertEqual(status, 200)
        self.assertEqual(set(result['lists']), set(SORTS))
        row = result['lists'][SORTS[0]][0]
        self.assertEqual(len(result['lists'][SORTS[0]]), 1)
        self.assertEqual(row['likePct'], 90)
        self.assertEqual(row['visits'], 12345)
        self.assertEqual(row['url'], 'https://www.roblox.com/games/201')
        calls = len(self.fake.calls)
        self.request('GET', '/api/trends')
        self.assertEqual(len(self.fake.calls), calls)
        self.fake.offline = True
        self.assertTrue(self.request('GET', '/api/trends?refresh=1')[1]['stale'])
        self.server.hub.cache = None
        self.assertTrue(self.request('GET', '/api/trends')[1]['stale'])
        (Path(self.temp.name) / 'trends-cache.json').unlink()
        self.assertEqual(self.request('GET', '/api/trends')[1], {'error': 'offline'})

    def test_previous_day_and_movement(self):
        result = self.request('GET', '/api/trends')[1]
        result['updated'] = time.time() - 21 * 3600
        for rows in result['lists'].values():
            rows.insert(0, {'universeId': 555})
        self.server.hub.cache = result
        fresh = self.request('GET', '/api/trends?refresh=1')[1]
        self.assertEqual(fresh['lists'][SORTS[0]][0]['movement'], 1)
        self.assertTrue((Path(self.temp.name) / 'trends-prev.json').exists())

    def test_startup_port_fallback(self):
        fake_server = MagicMock()
        fake_server.server_port = 8766
        fake_server.serve_forever.side_effect = KeyboardInterrupt
        with patch('server.make_server', side_effect=[OSError('busy'), fake_server]) as factory, patch('server.threading.Timer') as timer, patch('server.print') as output:
            main()
            self.assertEqual([c.args[0] for c in factory.call_args_list], [8765, 8766])
            self.assertIn('http://127.0.0.1:8766', output.call_args.args[0])
            timer.return_value.start.assert_called_once()
            fake_server.server_close.assert_called_once()

    def test_game_lookup_and_batches(self):
        for value in ('201', 'https://www.roblox.com/games/201/Example'):
            status, row = self.request('GET', '/api/game?url=' + value)
            self.assertEqual(status, 200)
            self.assertEqual(row['universeId'], 101)
            self.assertEqual(row['playing'], 42)
        for value in ('https://other.invalid/games/201', 'bad', 'https://www.roblox.com.evil/games/201'):
            self.assertEqual(self.request('GET', '/api/game?url=' + value)[0], 400)
        self.assertEqual(len(self.server.hub.enrich([{'universeId': n + 1} for n in range(123)])), 123)
        self.fake.offline = True
        self.assertEqual(self.request('GET', '/api/game?url=201')[0], 503)


    def test_stale_tab_gets_409(self):
        _, a = self.request('GET', '/api/data')
        _, b = self.request('GET', '/api/data')
        a['games'][0]['next'] = 'tab A'
        self.assertEqual(self.request('PUT', '/api/data', json.dumps(a))[0], 200)
        b['games'][0]['next'] = 'tab B (older copy)'
        status, body = self.request('PUT', '/api/data', json.dumps(b))
        self.assertEqual(status, 409)
        self.assertEqual(body['data']['games'][0]['next'], 'tab A')
        self.assertEqual(self.request('GET', '/api/data')[1]['games'][0]['next'], 'tab A')

    def test_game_lookup_has_like_percent(self):
        status, game = self.request('GET', '/api/game?url=201')
        self.assertEqual(status, 200)
        self.assertEqual(game['likePct'], 75)

    def test_baseline_rotates_by_its_own_age(self):
        hub = Hub(self.temp.name, self.fake)
        prev = Path(self.temp.name) / 'trends-prev.json'
        self.assertEqual(hub.trends(refresh=True)['lists'][SORTS[0]][0]['movement'], 'NEW')
        second = hub.trends(refresh=True)  # no baseline yet -> the first snapshot becomes it
        self.assertEqual(second['lists'][SORTS[0]][0]['movement'], 0)
        first_updated = json.loads(prev.read_text())['updated']
        hub.trends(refresh=True)  # baseline is fresh -> kept, even though the cache keeps refreshing
        self.assertEqual(json.loads(prev.read_text())['updated'], first_updated)
        old = json.loads(prev.read_text()); old['updated'] = time.time() - 21 * 3600
        prev.write_text(json.dumps(old))
        hub.trends(refresh=True)  # baseline older than 20 h -> rotates to the latest snapshot
        self.assertGreater(json.loads(prev.read_text())['updated'], time.time() - 3600)

if __name__ == '__main__':
    print('Tests use a real threaded HTTP server; if local binding is denied, the same handler runs in threaded memory transport.', flush=True)
    unittest.main(verbosity=2)
