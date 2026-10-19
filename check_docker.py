import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import urllib.error
import urllib.request
import zipfile

APP = Path(__file__).resolve().parent
ENV = dict(os.environ)

def run(*args):
    return subprocess.run(args, cwd=APP, env=ENV, check=True, capture_output=True, text=True).stdout.strip()

with tempfile.TemporaryDirectory(prefix='kindle-container-check-') as temp:
    root = Path(temp)
    (root / 'device/documents').mkdir(parents=True)
    (root / 'data').mkdir()
    ENV.update(KINDLE_MOUNT=str(root / 'device'), KINDLE_DATA=str(root / 'data'),
               KINDLE_IMAGE=os.environ.get('KINDLE_IMAGE', 'kindle-desk:0.2.0'),
               LOCAL_UID=str(os.getuid()), LOCAL_GID=str(os.getgid()))
    config = json.loads(run('docker', 'compose', '--env-file', '/dev/null', '-f', str(APP / 'compose.yaml'), 'config', '--format', 'json'))
    config['name'] = 'kindle-desk-smoke-' + str(os.getpid())
    svc = config['services']['kindle']
    svc['ports'][0]['published'] = '0'
    svc['healthcheck'] = {'interval': '1s', 'timeout': '5s', 'start_period': '10s', 'retries': 5}
    for mount in svc['volumes']:
        mount['source'] = str(root / ('device' if mount['target'] == '/kindle' else 'data'))
    path = root / 'compose.json'
    path.write_text(json.dumps(config))
    command = ['docker', 'compose', '-f', str(path)]
    def compose(*args):
        return run(*command, *args)
    try:
        print(compose('up', '-d', '--no-build', '--wait', '--wait-timeout', '60'), flush=True)
        address = compose('port', 'kindle', '8765')
        base = 'http://' + address
        token = ''
        def request(path, data=None, status=200, host='127.0.0.1:8765'):
            headers = {'Host': host, 'X-Desk-Token': token}
            req = urllib.request.Request(base + path, data=data, headers=headers)
            try:
                response = urllib.request.urlopen(req, timeout=180)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                body = response.read()
                assert response.status == status, (path, response.status, body[:500])
                return body
        state = json.loads(request('/api/state'))
        assert state['connected'] and state['converter'] and not state['can_eject'], state
        token = state['token']
        assert b'new-folder' in request('/')
        assert b'0.2' in request('/')
        request('/api/folders', json.dumps({'parent': '', 'name': 'Literature'}).encode())
        request('/api/state', host='evil.example', status=403)
        clipping = 'Test Book\n- Your Highlight on Location 1 | Added on Monday\n\nA test clipping.\n==========\n'
        result = json.loads(request('/api/import?name=My%20Clippings.txt', clipping.encode()))
        assert result['added'] == 1, result
        with zipfile.ZipFile(io.BytesIO(request('/api/export', b''))) as archive:
            assert len(archive.namelist()) == 1
            assert b'A test clipping.' in archive.read(archive.namelist()[0])
        epub = io.BytesIO()
        with zipfile.ZipFile(epub, 'w') as archive:
            archive.writestr('mimetype', 'application/epub+zip')
            archive.writestr('META-INF/container.xml', '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
            archive.writestr('content.opf', '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="uid"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Container test</dc:title><dc:language>zh-CN</dc:language><dc:identifier id="uid">kindle-container-test</dc:identifier></metadata><manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="chapter"/></spine></package>')
            archive.writestr('chapter.xhtml', '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>测试</title></head><body><h1>镜像转换测试</h1><p>仅写入临时模拟设备。</p></body></html>')
        sent = json.loads(request('/api/upload?name=container-test.epub&folder=Literature', epub.getvalue()))
        target = root / 'device/documents/Literature/container-test.azw3'
        assert sent['name'] == target.name and target.stat().st_size > 0, sent
        before = hashlib.sha256(target.read_bytes()).digest()
        request('/api/upload?name=container-test.azw3&folder=Literature', b'replacement', status=400)
        assert hashlib.sha256(target.read_bytes()).digest() == before
        sidecar = target.with_suffix('.sdr')
        sidecar.mkdir()
        (sidecar / 'notes.bin').write_bytes(b'preserved metadata')
        state = json.loads(request('/api/state'))
        book = next(b for b in state['books'] if b['name'] == target.name)
        request('/api/move', json.dumps({'id': book['id'], 'folder': ''}).encode())
        moved = root / 'device/documents' / target.name
        assert not target.exists() and hashlib.sha256(moved.read_bytes()).digest() == before
        assert (moved.with_suffix('.sdr') / 'notes.bin').read_bytes() == b'preserved metadata'
        assert not list((root / 'data/moves').glob('*.json'))
        request('/api/eject', b'', status=400)
        print(compose('up', '-d', '--no-build', '--force-recreate', '--wait', '--wait-timeout', '60'), flush=True)
        base = 'http://' + compose('port', 'kindle', '8765')
        state = json.loads(request('/api/state'))
        assert state['connected'] and len(state['notes']) == 1 and len(state['transfers']) == 1
        assert any(b['folder'] == '' and b['name'] == moved.name for b in state['books'])
        assert 'Literature' in state['folders']
        assert hashlib.sha256(moved.read_bytes()).digest() == before
        container_id = compose('ps', '-q', 'kindle')
        info = json.loads(run('docker', 'inspect', container_id))[0]
        assert info['State']['Health']['Status'] == 'healthy'
        assert info['HostConfig']['ReadonlyRootfs'] and info['Config']['User'] == f'{os.getuid()}:{os.getgid()}'
        assert not info['HostConfig']['Privileged']
        print('PASS: Compose health, loopback HTTP, Host checks, import/export, EPUB conversion, folder upload/move with sidecars, no overwrite, data after recreate, disabled eject.', flush=True)
        print('Converted test bytes:', moved.stat().st_size, flush=True)
    finally:
        print(compose('down'), flush=True)
