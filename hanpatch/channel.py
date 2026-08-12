"""Update channel — how a published .hpk reaches the people running it.

A release bundle is a file, and a file that is copied by hand rots: the person
who downloaded it in March has no way of learning that the March text had a
mistranslated boss name. The channel is the missing half of `hanpatch release`.
It is deliberately a *static* directory, because the alternative — a service
that decides what a client may download — is a thing that must be operated,
authenticated and kept from being an upload endpoint.

Server side::

    hanpatch publish "dist/Dragon Quest VII (ko).hpk" \\
        --root /mnt/ssd256/krpatch-hpk \\
        --url-base https://krpatch.duckdns.org/hpk/

which lays out

    <root>/index.json                       every patch, newest first
    <root>/index.html                       a page for people, not clients
    <root>/hpk-update.py                    the client, with no dependencies
    <root>/<id>/<id>-<version>.hpk          the bundle, under an immutable name
    <root>/<id>/<id>-<version>.json         its metadata, one file per release

`index.json` is *derived*: it is a pure function of the sidecar files, so the
channel has no database to lose and can be rebuilt from the directory it
serves. Bundles are published under a version-stamped name and never rewritten,
so the web server may mark them immutable and a mirror can be a dumb copy.

Client side::

    hanpatch update --dir ~/patches            # fetch what changed
    hanpatch update --check                    # exit 10 if something changed
    python3 hpk-update.py --list               # same, without installing hanpatch

The client trusts the channel for *what exists* and nothing else: every
download is checked against the size and SHA-256 that the index announced, and
a bundle that fails is discarded rather than installed. Verification is why the
updater can be a script one pastes into a terminal.

Everything below the `updater` markers is stdlib-only and free of hanpatch
imports, because `write_updater` cuts exactly that region out of this file to
build the standalone client. One implementation, two shipping shapes.
"""
import datetime
import json
import os
import re
import shutil

from hanpatch import config, release

FORMAT = 1


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        '%Y-%m-%dT%H:%M:%SZ')


def slug(text):
    """A directory name that survives a URL, a shell and a FAT32 stick."""
    s = re.sub(r'[^a-z0-9]+', '-', (text or '').lower()).strip('-')
    return s or 'patch'


def patch_id(info):
    return f"{slug(info.get('title'))}-{slug(info.get('target') or 'ko')}"


def _sidecars(root):
    for entry in sorted(os.listdir(root)):
        d = os.path.join(root, entry)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if name.endswith('.json'):
                yield os.path.join(d, name)


def _released_key(rec):
    return (rec.get('released', ''), rec.get('version', ''))


def build_index(root, url_base=None):
    """Rewrite `<root>/index.json` from the sidecars on disk. Returns the doc."""
    by_id = {}
    for path in _sidecars(root):
        with open(path, encoding='utf-8') as f:
            rec = json.load(f)
        if not rec.get('sha256') or not rec.get('file'):
            continue
        by_id.setdefault(rec['id'], []).append(rec)

    patches = []
    for pid, recs in by_id.items():
        recs.sort(key=_released_key, reverse=True)
        latest = dict(recs[0])
        latest['history'] = [{k: r[k] for k in
                              ('version', 'file', 'sha256', 'size', 'released')
                              if k in r} for r in recs[1:]]
        patches.append(latest)
    patches.sort(key=_released_key, reverse=True)

    doc = {'format': FORMAT, 'generated': _now(), 'patches': patches}
    if url_base:
        doc['url_base'] = url_base if url_base.endswith('/') else url_base + '/'
    tmp = os.path.join(root, 'index.json.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(doc, f, indent=1, ensure_ascii=False)
        f.write('\n')
    os.replace(tmp, os.path.join(root, 'index.json'))
    return doc


def _pick_version(dirname, pid, want, sha):
    """A version nobody has seen before, unless this exact bundle was published."""
    if want:
        return want
    for name in sorted(os.listdir(dirname)) if os.path.isdir(dirname) else []:
        if not name.endswith('.json'):
            continue
        with open(os.path.join(dirname, name), encoding='utf-8') as f:
            rec = json.load(f)
        if rec.get('sha256') == sha:
            return rec['version']
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y.%m.%d')
    used = {n[len(pid) + 1:-4] for n in
            (os.listdir(dirname) if os.path.isdir(dirname) else [])
            if n.endswith('.hpk')}
    if stamp not in used:
        return stamp
    n = 2
    while f'{stamp}-{n}' in used:
        n += 1
    return f'{stamp}-{n}'


def publish(bundle, root, url_base=None, version=None, notes=None):
    """Copy `bundle` into the channel under an immutable name and reindex."""
    info = release.inspect(bundle)
    if info.get('format') != release.FORMAT:
        raise SystemExit(f'unsupported bundle format {info.get("format")}')
    pid = patch_id(info)
    sha = release._sha(bundle)
    d = os.path.join(root, pid)
    os.makedirs(d, exist_ok=True)
    version = _pick_version(d, pid, version, sha)

    name = f'{pid}-{version}.hpk'
    dest = os.path.join(d, name)
    side = os.path.join(d, f'{pid}-{version}.json')
    existing = os.path.exists(dest) and release._sha(dest) == sha
    if os.path.exists(dest) and not existing:
        raise SystemExit(f'refusing to overwrite a published bundle: {dest}\n'
                         'published versions are immutable; pass a new --version.')
    if not existing:
        tmp = dest + '.tmp'
        shutil.copyfile(bundle, tmp)
        os.replace(tmp, dest)

    released = _now()
    if existing and os.path.exists(side):
        with open(side, encoding='utf-8') as f:
            released = json.load(f).get('released', released)
    rec = {
        'id': pid,
        'title': info.get('title'),
        'platform': info.get('platform'),
        'adapter': info.get('adapter'),
        'target': info.get('target'),
        'version': version,
        'released': released,
        'file': f'{pid}/{name}',
        'size': os.path.getsize(dest),
        'sha256': sha,
        'digest': info.get('digest'),
        'entries': info.get('entries'),
        'source_sha256': info.get('source_sha256'),
        'output_sha256': info.get('output_sha256'),
        'notes': notes if notes is not None else info.get('notes', ''),
    }
    with open(side, 'w', encoding='utf-8') as f:
        json.dump(rec, f, indent=1, ensure_ascii=False)
        f.write('\n')

    write_updater(os.path.join(root, 'hpk-update.py'))
    doc = build_index(root, url_base)
    write_page(root, doc)
    rec['republished'] = existing
    return rec


PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>한글패치 업데이트 채널</title>
<style>
body{{max-width:52rem;margin:2rem auto;padding:0 1rem;
 font:16px/1.6 system-ui,"Noto Sans KR",sans-serif;color:#1c1c1c}}
code,pre{{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.88em}}
pre{{background:#f5f5f5;padding:.8rem 1rem;overflow-x:auto;border-radius:4px}}
table{{border-collapse:collapse;width:100%;margin:.6rem 0 1.4rem}}
th,td{{text-align:left;padding:.35rem .6rem;border-bottom:1px solid #e2e2e2;
 vertical-align:top}}
td.sha{{word-break:break-all;color:#555}}
h2{{margin-top:2rem;font-size:1.15rem}}
.muted{{color:#666;font-size:.9em}}
</style></head><body>
<h1>한글패치 업데이트 채널</h1>
<p>이 페이지는 배포된 <code>.hpk</code> 번들의 목록입니다. 번들에는 번역문과
글꼴만 들어 있고 게임은 들어 있지 않습니다. 적용은 각자 소유한 ROM에 대해
이루어지며, 그 결과는 제작자의 빌드와 바이트 단위로 같습니다.</p>

<h2>업데이트 도구</h2>
<pre>curl -O {base}hpk-update.py
python3 hpk-update.py --dir ~/patches</pre>
<p class="muted">파이썬 3.9 이상이면 그 밖의 준비물은 없습니다. 내려받은
파일은 발표된 크기와 SHA-256 으로 검증되며, 어긋나면 설치하지 않고 버립니다.
<code>--check</code> 는 받지 않고 갱신 여부만 알려 줍니다(갱신이 있으면 종료
코드 10). hanpatch 를 설치했다면 <code>hanpatch update</code> 가 같은 일을
합니다.</p>

<h2>적용</h2>
<pre>pip install hanpatch
hanpatch apply "<i>내려받은.hpk</i>" --rom /경로/내ROM.3ds</pre>

<h2>번들</h2>
{rows}
<p class="muted">기계가 읽는 목록: <a href="index.json">index.json</a> ·
생성 {generated}</p>
<p class="muted">패치할 게임을 정당하게 소유할 책임과 거주지 법을 지킬 책임은
이용자에게 있습니다.</p>
</body></html>
"""

ROW = """<h3>{title} <span class="muted">({target})</span></h3>
<table>
<tr><th>버전</th><td>{version} <span class="muted">({released})</span></td></tr>
<tr><th>파일</th><td><a href="{file}">{name}</a> <span class="muted">{mb:.1f} MB</span></td></tr>
<tr><th>SHA-256</th><td class="sha">{sha256}</td></tr>
<tr><th>대상 ROM</th><td class="sha">{source_sha256}</td></tr>
<tr><th>결과 ROM</th><td class="sha">{output_sha256}</td></tr>
<tr><th>문자열</th><td>{entries}개 · 매니페스트 {digest}</td></tr>
{notes}</table>
"""


def write_page(root, doc):
    rows = []
    for p in doc['patches']:
        notes = (f'<tr><th>비고</th><td>{p["notes"]}</td></tr>\n'
                 if p.get('notes') else '')
        rows.append(ROW.format(
            title=p.get('title') or p['id'], target=p.get('target') or '',
            version=p.get('version'), released=p.get('released', ''),
            file=p['file'], name=os.path.basename(p['file']),
            mb=p.get('size', 0) / 1e6, sha256=p.get('sha256', ''),
            source_sha256=p.get('source_sha256') or '(알 수 없음)',
            output_sha256=p.get('output_sha256') or '(알 수 없음)',
            entries=p.get('entries', 0), digest=(p.get('digest') or '')[:16],
            notes=notes))
    if not rows:
        rows = ['<p class="muted">아직 배포된 번들이 없습니다.</p>']
    html = PAGE.format(base=doc.get('url_base', ''), rows='\n'.join(rows),
                       generated=doc['generated'])
    tmp = os.path.join(root, 'index.html.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(html)
    os.replace(tmp, os.path.join(root, 'index.html'))
    return os.path.join(root, 'index.html')


# --- updater:begin ---  (stdlib only below; write_updater cuts this region out)
import argparse  # noqa: E402
import hashlib  # noqa: E402
import http.client  # noqa: E402
import sys  # noqa: E402
import urllib.error  # noqa: E402
import urllib.parse  # noqa: E402
import urllib.request  # noqa: E402

CHANNEL = os.environ.get('HANPATCH_CHANNEL',
                         'https://krpatch.duckdns.org/hpk/')
STATE_FILE = 'hpk-state.json'
UA = 'hpk-update/1 (+https://krpatch.duckdns.org/hpk/)'
CHUNK = 1 << 20


def _open(url, offset=0):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    if offset:
        req.add_header('Range', f'bytes={offset}-')
    return urllib.request.urlopen(req, timeout=60)


def fetch_index(channel=CHANNEL):
    """Read the channel index. `channel` may be a URL or a local directory."""
    base = channel if channel.endswith('/') else channel + '/'
    url = base + 'index.json'
    try:
        if '://' not in base or base.startswith('file://'):
            path = base[7:] if base.startswith('file://') else base
            with open(os.path.join(path, 'index.json'), encoding='utf-8') as f:
                doc = json.load(f)
        else:
            with _open(url) as r:
                doc = json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        raise SystemExit(f'{url}: HTTP {e.code} {e.reason}')
    except urllib.error.URLError as e:
        raise SystemExit(f'{url}: {e.reason}')
    except (OSError, ValueError, http.client.HTTPException) as e:
        raise SystemExit(f'{url}: unreadable channel index ({e})')
    if not isinstance(doc, dict):
        raise SystemExit(f'{url}: not a channel index')
    if doc.get('format') != 1:
        raise SystemExit(f'unsupported channel format {doc.get("format")}; '
                         'update this tool')
    doc['url_base'] = base
    return doc


def load_state(dest):
    try:
        with open(os.path.join(dest, STATE_FILE), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(dest, state):
    tmp = os.path.join(dest, STATE_FILE + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=1, ensure_ascii=False, sort_keys=True)
        f.write('\n')
    os.replace(tmp, os.path.join(dest, STATE_FILE))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def status(doc, dest, ids=None):
    """What each selected patch is: 'current', 'update' or 'new'."""
    state = load_state(dest)
    rows = []
    for p in doc.get('patches', []):
        if ids and p['id'] not in ids:
            continue
        local = state.get(p['id']) or {}
        path = os.path.join(dest, os.path.basename(p['file']))
        name = local.get('name') or ''
        have = None
        if name and os.path.exists(os.path.join(dest, name)):
            have = local.get('sha256')
        if have is None and os.path.exists(path):
            have = sha256_file(path)
        if have == p['sha256']:
            what = 'current'
        elif local or have:
            what = 'update'
        else:
            what = 'new'
        rows.append({'patch': p, 'state': what, 'local': local, 'path': path})
    if ids:
        known = {p['id'] for p in doc.get('patches', [])}
        for missing in sorted(set(ids) - known):
            raise SystemExit(f'no such patch in the channel: {missing}')
    return rows


def download(patch, dest, url_base, progress=None):
    """Fetch one bundle into `dest`, verified. Returns its path."""
    url = urllib.parse.urljoin(url_base, patch['file'])
    name = os.path.basename(patch['file'])
    out = os.path.join(dest, name)
    part = out + '.part'
    have = os.path.getsize(part) if os.path.exists(part) else 0
    if have and have >= patch.get('size', 0):
        have = 0
    total = patch.get('size') or 0
    mode = 'ab' if have else 'wb'
    if '://' not in url or url.startswith('file://'):
        src = url[7:] if url.startswith('file://') else url
        shutil.copyfile(src, part)
        have = os.path.getsize(part)
        if progress:
            progress(have, total)
    else:
        r = None
        if have:
            try:
                r = _open(url, have)
                if r.status != 206:  # the server ignored the range
                    r.close()
                    r = None
            except urllib.error.HTTPError as e:
                if e.code not in (416, 501):
                    raise SystemExit(f'{url}: HTTP {e.code} {e.reason}')
            except urllib.error.URLError as e:
                raise SystemExit(f'{url}: {e.reason}')
            if r is None:
                have, mode = 0, 'wb'
        if r is None:
            try:
                r = _open(url)
            except urllib.error.HTTPError as e:
                raise SystemExit(f'{url}: HTTP {e.code} {e.reason}')
            except urllib.error.URLError as e:
                raise SystemExit(f'{url}: {e.reason}')
        with r, open(part, mode) as f:
            done = have
            while True:
                cut = False
                try:
                    b = r.read(CHUNK)
                except http.client.IncompleteRead as e:
                    b, cut = e.partial, True  # a cut body is a short file below
                if b:
                    f.write(b)
                    done += len(b)
                    if progress:
                        progress(done, total)
                if cut or not b:
                    break

    size = os.path.getsize(part)
    if total and size != total:
        os.remove(part)
        raise SystemExit(f'{name}: got {size} bytes, the channel announced '
                         f'{total}. Discarded.')
    got = sha256_file(part)
    if got != patch['sha256']:
        os.remove(part)
        raise SystemExit(f'{name}: sha256 mismatch\n  channel {patch["sha256"]}'
                         f'\n  download {got}\nDiscarded; nothing was installed.')
    os.replace(part, out)
    return out


def update(channel=CHANNEL, dest='.', ids=None, check_only=False, quiet=False,
           progress=None):
    """Bring `dest` in line with the channel. Returns a report."""
    doc = fetch_index(channel)
    os.makedirs(dest, exist_ok=True)
    rows = status(doc, dest, ids)
    stale = [r for r in rows if r['state'] != 'current']
    report = {'channel': doc['url_base'], 'dest': dest, 'checked': len(rows),
              'pending': [r['patch']['id'] for r in stale], 'installed': []}
    if check_only or not stale:
        if not quiet:
            for r in rows:
                p = r['patch']
                mark = {'current': '=', 'update': '>', 'new': '+'}[r['state']]
                print(f"{mark} {p['id']}  {p['version']}  "
                      f"{p.get('size', 0) / 1e6:.1f} MB"
                      + ('' if r['state'] != 'update' else
                         f"  (have {r['local'].get('version', '?')})"))
            if not rows:
                print('nothing in this channel yet')
            elif not stale:
                print('up to date')
        return report

    state = load_state(dest)
    for r in stale:
        p = r['patch']
        if not quiet:
            print(f"{p['id']} {p['version']} "
                  f"({p.get('size', 0) / 1e6:.1f} MB) …", flush=True)
        path = download(p, dest, doc['url_base'], progress)
        state[p['id']] = {'version': p['version'], 'sha256': p['sha256'],
                          'name': os.path.basename(path),
                          'released': p.get('released'),
                          'source_sha256': p.get('source_sha256'),
                          'output_sha256': p.get('output_sha256')}
        save_state(dest, state)
        report['installed'].append({'id': p['id'], 'version': p['version'],
                                    'path': path})
        if not quiet:
            print(f'  {path}\n  sha256 {p["sha256"]}')
            if p.get('source_sha256'):
                print(f'  apply to the ROM whose sha256 is '
                      f'{p["source_sha256"]}')
    return report


def cli(argv=None):
    ap = argparse.ArgumentParser(
        prog='hpk-update', allow_abbrev=False,
        description='Fetch and verify .hpk translation bundles.')
    ap.add_argument('id', nargs='*', help='patch ids (default: every patch)')
    ap.add_argument('--channel', default=CHANNEL)
    ap.add_argument('--dir', default='.', help='where bundles live')
    ap.add_argument('--check', action='store_true',
                    help='report only; exit 10 when an update is waiting')
    ap.add_argument('--list', action='store_true', help='alias of --check')
    ap.add_argument('--quiet', action='store_true')
    a = ap.parse_args(argv)
    bar = None
    if not a.quiet and sys.stderr.isatty():
        def bar(done, total):
            pct = f'{done * 100 // total:3d}%' if total else '    '
            print(f'\r  {pct} {done / 1e6:7.1f} MB', end='', file=sys.stderr,
                  flush=True)
            if total and done >= total:
                print('', file=sys.stderr)
    rep = update(a.channel, a.dir, ids=a.id or None,
                 check_only=a.check or a.list, quiet=a.quiet, progress=bar)
    if (a.check or a.list) and rep['pending']:
        return 10
    return 0
# --- updater:end ---

DEFAULT_CHANNEL = CHANNEL


UPDATER_HEAD = '''#!/usr/bin/env python3
"""hpk-update — fetch and verify .hpk translation bundles.

    python3 hpk-update.py --dir ~/patches      # install or update everything
    python3 hpk-update.py --check              # exit 10 if something changed
    python3 hpk-update.py dragon-quest-vii-ko  # one patch only

Downloads are checked against the size and SHA-256 published in the channel
index; a bundle that does not match is deleted instead of installed. Applying a
bundle to your ROM is a separate step: `pip install hanpatch` then
`hanpatch apply <bundle> --rom <your rom>`.

Generated by `hanpatch publish` from hanpatch/channel.py — edit that, not this.
"""
import json
import os
import shutil

'''

UPDATER_TAIL = '''

if __name__ == '__main__':
    sys.exit(cli())
'''


def _updater_body():
    src = open(os.path.abspath(__file__).replace('.pyc', '.py'),
               encoding='utf-8').read()
    a = src.index('# --- updater:begin ---')
    a = src.index('\n', a) + 1
    b = src.index('# --- updater:end ---')
    body = src[a:b].rstrip() + '\n'
    for bad in ('from hanpatch', 'import hanpatch', 'config.', 'release.'):
        if bad in body:
            raise SystemExit('the updater region must stay stdlib-only; '
                             f'it refers to {bad!r}')
    return body


def write_updater(path):
    """Write the standalone client, cut from the region below the markers."""
    text = UPDATER_HEAD + _updater_body() + UPDATER_TAIL
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, path)
    os.chmod(path, 0o755)
    return path


def default_dest():
    """Where a project keeps the bundles it tracks."""
    try:
        return config.dist()
    except SystemExit:
        return os.getcwd()
