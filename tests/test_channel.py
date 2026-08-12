"""Adversarial tests for the .hpk update channel.

Run:  python3 tests/test_channel.py

The channel hands a stranger a file and tells them to run it against their own
copy of a game. Two things must therefore be true no matter what the server
says: a bundle whose bytes do not match the announced hash is never installed,
and a version that was published once is never rewritten under the same name.
Everything here tries to break one of those two.

The tests serve a real channel over HTTP on localhost, because the interesting
failures — a truncated body, a lying index, a server that ignores Range — are
transport failures and a fake fetcher would assume them away.
"""
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hanpatch import channel, release  # noqa: E402

PASS = []
FAIL = []


def case(name, ok):
    (PASS if ok else FAIL).append(name)
    print(('  ok   ' if ok else '  FAIL ') + name)


def sec(title):
    print()
    print(title)


def raises(fn, *a, **kw):
    """The message of the SystemExit, or None if the call succeeded."""
    try:
        fn(*a, **kw)
    except SystemExit as e:
        return str(e)
    return None


def make_bundle(path, title='Dragon Quest VII', target='ko', entries=3,
                digest='d' * 64, extra=b''):
    info = {'format': release.FORMAT, 'title': title, 'platform': 'threeds',
            'adapter': 'dq7', 'target': target, 'entries': entries,
            'digest': digest, 'source_sha256': 'a' * 64,
            'output_sha256': 'b' * 64, 'notes': ''}
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('bundle.json', json.dumps(info))
        z.writestr('manifest.json', json.dumps({'digest': digest,
                                                'entries': [1] * entries}))
        z.writestr('profile.json', '{}')
        if extra:
            z.writestr('pad.bin', extra)
    return path


class Handler(http.server.BaseHTTPRequestHandler):
    """A channel server that can be told to misbehave.

    Ranges are implemented here rather than inherited, because
    SimpleHTTPRequestHandler ignores Range — and a client that resumes against
    a server which silently answers 200 is exactly the bug worth testing.
    """
    truncate = False
    no_range = False

    def log_message(self, *a):
        pass

    def path_on_disk(self):
        rel = self.path.split('?', 1)[0].lstrip('/')
        return os.path.join(self.server.root, rel)

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head=False):
        try:
            body = open(self.path_on_disk(), 'rb').read()
        except OSError:
            self.send_error(404)
            return
        start = 0
        rng = self.headers.get('Range')
        if rng and not self.no_range:
            start = int(rng.split('=', 1)[1].split('-', 1)[0])
            if start >= len(body):
                self.send_error(416)
                return
        out = body[start:]
        if self.truncate and self.path.endswith('.hpk'):
            out = out[:len(out) // 2]
        self.send_response(206 if start else 200)
        if start:
            self.send_header('Content-Range',
                             f'bytes {start}-{len(body) - 1}/{len(body)}')
        if not self.no_range:
            self.send_header('Accept-Ranges', 'bytes')
        # the announced length is the honest one even when the body is cut, so
        # a client that trusts Content-Length is the one that gets caught out
        self.send_header('Content-Length', str(len(body) - start))
        self.send_header('Content-Type', 'application/octet-stream')
        self.end_headers()
        if not head:
            self.wfile.write(out)


def serve(root):
    class H(Handler):
        pass
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
    srv.root = root
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, H, f'http://127.0.0.1:{srv.server_port}/'


TMP = tempfile.mkdtemp(prefix='hanpatch-channel-test-')
CHAN = os.path.join(TMP, 'chan')
os.makedirs(CHAN)
BUNDLE = make_bundle(os.path.join(TMP, 'Dragon Quest VII (ko).hpk'))

sec('publishing')
rec = channel.publish(BUNDLE, CHAN, url_base='https://example.invalid/hpk/')
case('the id is derived from the title and the target',
     rec['id'] == 'dragon-quest-vii-ko')
case('the published name carries the version',
     rec['file'] == f"dragon-quest-vii-ko/dragon-quest-vii-ko-{rec['version']}.hpk")
case('the bundle landed where the index says it did',
     os.path.exists(os.path.join(CHAN, rec['file'])))
case('the recorded size is the size on disk',
     rec['size'] == os.path.getsize(os.path.join(CHAN, rec['file'])))
case('the recorded hash is the hash of the bytes served',
     rec['sha256'] == release._sha(os.path.join(CHAN, rec['file'])))
case('an index, a page and a client are published beside it',
     all(os.path.exists(os.path.join(CHAN, n)) for n in
         ('index.json', 'index.html', 'hpk-update.py')))
INDEX = json.load(open(os.path.join(CHAN, 'index.json'), encoding='utf-8'))
case('the index announces exactly one patch', len(INDEX['patches']) == 1)
case('the index carries the input hash the patch expects',
     INDEX['patches'][0]['source_sha256'] == 'a' * 64)
case('the index knows where it is served from',
     INDEX['url_base'] == 'https://example.invalid/hpk/')

again = channel.publish(BUNDLE, CHAN, url_base='https://example.invalid/hpk/')
case('republishing identical bytes is a no-op, not a second version',
     again['version'] == rec['version'] and again['republished'])
case('re-publishing did not multiply the entry',
     len(json.load(open(os.path.join(CHAN, 'index.json'),
                        encoding='utf-8'))['patches']) == 1)
case('the release date survived the republish',
     again['released'] == rec['released'])

CHANGED = make_bundle(os.path.join(TMP, 'changed.hpk'), digest='e' * 64,
                      extra=b'x' * 4096)
msg = raises(channel.publish, CHANGED, CHAN, version=rec['version'])
case('different bytes may not take a published version name',
     msg is not None and 'immutable' in msg)
case('the refused publish left the original bytes alone',
     release._sha(os.path.join(CHAN, rec['file'])) == rec['sha256'])

new = channel.publish(CHANGED, CHAN, url_base='https://example.invalid/hpk/',
                      version='2026.09.01', notes='보스 이름 수정')
INDEX = json.load(open(os.path.join(CHAN, 'index.json'), encoding='utf-8'))
case('a second version replaces the first as the latest',
     len(INDEX['patches']) == 1 and
     INDEX['patches'][0]['version'] == '2026.09.01')
case('the older version is kept in the history, not deleted',
     [h['version'] for h in INDEX['patches'][0]['history']] == [rec['version']]
     and os.path.exists(os.path.join(CHAN, rec['file'])))
case('a note given at publish time reaches the index',
     INDEX['patches'][0]['notes'] == '보스 이름 수정')
case('the page names the current version',
     '2026.09.01' in open(os.path.join(CHAN, 'index.html'),
                          encoding='utf-8').read())

built = channel.build_index(CHAN, 'https://example.invalid/hpk/')
case('the index is a pure function of the directory',
     [p['file'] for p in built['patches']] ==
     [p['file'] for p in INDEX['patches']])

OTHER = make_bundle(os.path.join(TMP, 'cs.hpk'), title='Crimson Shroud',
                    digest='f' * 64)
channel.publish(OTHER, CHAN, url_base='https://example.invalid/hpk/')
INDEX = json.load(open(os.path.join(CHAN, 'index.json'), encoding='utf-8'))
case('two titles are two entries, keyed by title and language',
     sorted(p['id'] for p in INDEX['patches']) ==
     ['crimson-shroud-ko', 'dragon-quest-vii-ko'])

sec('fetching over http')
SRV, H, URL = serve(CHAN)
DEST = os.path.join(TMP, 'dest')
rep = channel.update(URL, DEST, check_only=True, quiet=True)
case('a check reports both patches as pending', len(rep['pending']) == 2)
case('a check installs nothing',
     not os.path.exists(os.path.join(DEST, 'hpk-state.json')))

rep = channel.update(URL, DEST, ids=['dragon-quest-vii-ko'], quiet=True)
got = os.path.join(DEST, 'dragon-quest-vii-ko-2026.09.01.hpk')
case('a named patch is fetched under its published name', os.path.exists(got))
case('only the named patch was fetched', len(rep['installed']) == 1)
case('the downloaded bytes are the published bytes',
     release._sha(got) == release._sha(os.path.join(
         CHAN, 'dragon-quest-vii-ko/dragon-quest-vii-ko-2026.09.01.hpk')))
case('the fetched bundle is a bundle hanpatch can read',
     release.inspect(got)['title'] == 'Dragon Quest VII')
case('no .part file survives a completed download',
     not os.path.exists(got + '.part'))
state = json.load(open(os.path.join(DEST, 'hpk-state.json'), encoding='utf-8'))
case('the state records what was installed',
     state['dragon-quest-vii-ko']['version'] == '2026.09.01')

rep = channel.update(URL, DEST, ids=['dragon-quest-vii-ko'], quiet=True)
case('a second run has nothing to do', rep['installed'] == [])
rows = channel.status(channel.fetch_index(URL), DEST,
                      ['dragon-quest-vii-ko'])
case('an installed patch reads as current', rows[0]['state'] == 'current')

os.remove(os.path.join(DEST, 'hpk-state.json'))
rows = channel.status(channel.fetch_index(URL), DEST,
                      ['dragon-quest-vii-ko'])
case('a lost state file is recovered by hashing what is on disk',
     rows[0]['state'] == 'current')

open(got, 'ab').write(b'rot')
rows = channel.status(channel.fetch_index(URL), DEST,
                      ['dragon-quest-vii-ko'])
case('a corrupted local bundle does not read as current',
     rows[0]['state'] in ('update', 'new'))
channel.update(URL, DEST, ids=['dragon-quest-vii-ko'], quiet=True)
case('the corrupted bundle is replaced by the published bytes',
     release._sha(got) == release._sha(os.path.join(
         CHAN, 'dragon-quest-vii-ko/dragon-quest-vii-ko-2026.09.01.hpk')))

case('asking for a patch the channel does not have fails loudly',
     'no such patch' in (raises(channel.update, URL, DEST, ids=['nope'],
                                quiet=True) or ''))

sec('a channel that lies')
BAD = os.path.join(TMP, 'bad')
DEST2 = os.path.join(TMP, 'dest2')
shutil.copytree(CHAN, BAD)
doc = json.load(open(os.path.join(BAD, 'index.json'), encoding='utf-8'))
for p in doc['patches']:
    if p['id'] == 'dragon-quest-vii-ko':
        p['sha256'] = '0' * 64
json.dump(doc, open(os.path.join(BAD, 'index.json'), 'w'))
SRV2, H2, URL2 = serve(BAD)
msg = raises(channel.update, URL2, DEST2, ids=['dragon-quest-vii-ko'],
             quiet=True)
case('a hash that does not match the bytes is a refusal',
     msg is not None and 'sha256 mismatch' in msg)
case('the mismatched download is not left behind',
     not any(n.endswith(('.hpk', '.part')) for n in os.listdir(DEST2)))
case('nothing was recorded as installed',
     not os.path.exists(os.path.join(DEST2, 'hpk-state.json')))

H2.truncate = True
DEST3 = os.path.join(TMP, 'dest3')
msg = raises(channel.update, URL2, DEST3, ids=['crimson-shroud-ko'],
             quiet=True)
case('a truncated body is caught by the size or the hash',
     msg is not None and ('bytes' in msg or 'sha256' in msg))
case('the truncated download is discarded',
     not any(n.endswith(('.hpk', '.part')) for n in os.listdir(DEST3)))
H2.truncate = False

doc = json.load(open(os.path.join(BAD, 'index.json'), encoding='utf-8'))
doc['format'] = 99
json.dump(doc, open(os.path.join(BAD, 'index.json'), 'w'))
case('a channel from the future is refused, not guessed at',
     'unsupported channel format' in (raises(channel.fetch_index, URL2) or ''))

open(os.path.join(BAD, 'index.json'), 'w').write('{"format": 1, "patc')
case('half an index is a refusal with the URL in it, not a traceback',
     'index.json' in (raises(channel.fetch_index, URL2) or ''))
open(os.path.join(BAD, 'index.json'), 'w').write('["not", "an", "index"]')
case('an index that is not an object is refused',
     'not a channel index' in (raises(channel.fetch_index, URL2) or ''))

sec('resuming')
DEST4 = os.path.join(TMP, 'dest4')
os.makedirs(DEST4)
src = os.path.join(CHAN, 'dragon-quest-vii-ko/dragon-quest-vii-ko-2026.09.01.hpk')
whole = open(src, 'rb').read()
part = os.path.join(DEST4, 'dragon-quest-vii-ko-2026.09.01.hpk.part')
open(part, 'wb').write(whole[:len(whole) // 3])
channel.update(URL, DEST4, ids=['dragon-quest-vii-ko'], quiet=True)
case('a half download is resumed into the right file',
     open(os.path.join(DEST4,
                       'dragon-quest-vii-ko-2026.09.01.hpk'), 'rb').read()
     == whole)

DEST5 = os.path.join(TMP, 'dest5')
os.makedirs(DEST5)
part = os.path.join(DEST5, 'dragon-quest-vii-ko-2026.09.01.hpk.part')
open(part, 'wb').write(b'\0' * (len(whole) // 3))
H.no_range = True
channel.update(URL, DEST5, ids=['dragon-quest-vii-ko'], quiet=True)
case('a server that ignores Range makes the client start over, not corrupt',
     open(os.path.join(DEST5,
                       'dragon-quest-vii-ko-2026.09.01.hpk'), 'rb').read()
     == whole)
H.no_range = False

sec('the standalone client')
UPD = os.path.join(TMP, 'hpk-update.py')
channel.write_updater(UPD)
text = open(UPD, encoding='utf-8').read()
IMPORTS = [l.strip() for l in text.splitlines()
           if l.startswith(('import ', 'from '))]
case('the generated client imports nothing from hanpatch',
     not any('hanpatch' in l for l in IMPORTS))
case('every module the generated client imports is in the stdlib',
     all(l.split()[1].split('.')[0] in
         getattr(sys, 'stdlib_module_names', {l.split()[1].split('.')[0]})
         for l in IMPORTS))
case('the generated client is executable', os.access(UPD, os.X_OK))
case('the published client is byte-identical to a fresh cut',
     open(os.path.join(CHAN, 'hpk-update.py'), encoding='utf-8').read() == text)

DEST6 = os.path.join(TMP, 'dest6')
env = dict(os.environ, PYTHONPATH='')
r = subprocess.run([sys.executable, UPD, '--channel', URL, '--dir', DEST6,
                    '--check'], capture_output=True, text=True, env=env)
case('--check on a fresh directory exits 10', r.returncode == 10)
case('--check names what it would fetch', 'dragon-quest-vii-ko' in r.stdout)
case('--check downloaded nothing', not os.path.isdir(DEST6) or
     not [n for n in os.listdir(DEST6) if n.endswith('.hpk')])

r = subprocess.run([sys.executable, UPD, '--channel', URL, '--dir', DEST6,
                    'dragon-quest-vii-ko'], capture_output=True, text=True,
                   env=env)
case('the standalone client installs a bundle', r.returncode == 0 and
     os.path.exists(os.path.join(
         DEST6, 'dragon-quest-vii-ko-2026.09.01.hpk')))
case('the standalone client verified the bytes it wrote',
     release._sha(os.path.join(DEST6, 'dragon-quest-vii-ko-2026.09.01.hpk'))
     == release._sha(src))
r = subprocess.run([sys.executable, UPD, '--channel', URL, '--dir', DEST6,
                    'dragon-quest-vii-ko', '--check'], capture_output=True,
                   text=True, env=env)
case('--check on an up-to-date directory exits 0', r.returncode == 0)
case('--check says so out loud', 'up to date' in r.stdout)

sec('the cli')
r = subprocess.run([sys.executable, '-m', 'hanpatch.cli', 'update',
                    '--channel', URL, '--dir', os.path.join(TMP, 'dest7'),
                    '--check'], capture_output=True, text=True,
                   cwd=ROOT, env=dict(os.environ, PYTHONPATH=ROOT))
case('hanpatch update --check exits 10 when a patch is waiting',
     r.returncode == 10)
r = subprocess.run([sys.executable, '-m', 'hanpatch.cli', 'publish',
                    BUNDLE, '--root', os.path.join(TMP, 'chan2'),
                    '--url-base', 'https://example.invalid/hpk/'],
                   capture_output=True, text=True, cwd=ROOT,
                   env=dict(os.environ, PYTHONPATH=ROOT))
case('hanpatch publish needs no project to publish a bundle',
     r.returncode == 0 and 'dragon-quest-vii-ko' in r.stdout)
case('the fresh channel is servable',
     os.path.exists(os.path.join(TMP, 'chan2', 'index.json')))

SRV.shutdown()
SRV2.shutdown()
shutil.rmtree(TMP, ignore_errors=True)

print()
print(f'{len(PASS)} passed, {len(FAIL)} failed')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
sys.exit(1 if FAIL else 0)
