#!/usr/bin/env python3
"""브라우저 패처를 배포 디렉터리에 놓는다.

같은 파이프라인을 두 곳에서 돌리므로(명령줄, 브라우저), 배포판에 들어가는 휠은
저장소의 지금 소스에서 만든 것이어야 한다. 그래서 이 스크립트가 휠을 빌드하고,
정적 파일을 복사하고, 무엇이 어떤 해시로 올라갔는지 적는다.

    python3 tools/deploy_web.py --root /mnt/ssd256/krpatch-web

`vendor/pyodide` 는 크기가 커서 기본적으로 건드리지 않는다. 없으면 알려 주고,
`--fetch-pyodide` 를 주면 받아 온다.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(HERE, 'web', 'apply')
PYODIDE = 'v0.26.4'
PYODIDE_FILES = (
    'pyodide.js', 'pyodide.mjs', 'pyodide.asm.js', 'pyodide.asm.wasm',
    'python_stdlib.zip', 'pyodide-lock.json',
    'pycryptodome-3.20.0-cp35-abi3-pyodide_2024_0_wasm32.whl',
    'pillow-10.2.0-cp312-cp312-pyodide_2024_0_wasm32.whl',
    'micropip-0.6.0-py3-none-any.whl',
    'packaging-23.2-py3-none-any.whl',
)
APP_FILES = ('index.html', 'app.js', 'style.css', 'worker.js',
             'opfs-bridge.js', 'opfs-proxy.js')


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            b = f.read(1 << 22)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def build_wheel(out_dir):
    """저장소의 현재 소스로 휠을 만든다. 브라우저가 옛 코드를 돌리면 안 된다."""
    tmp = os.path.join(out_dir, '.build')
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    subprocess.run([sys.executable, '-m', 'pip', 'wheel', HERE, '--no-deps',
                    '--no-build-isolation', '-w', tmp],
                   check=True, capture_output=True)
    wheels = [f for f in os.listdir(tmp) if f.endswith('.whl')]
    if len(wheels) != 1:
        raise SystemExit(f'휠이 하나가 아닙니다: {wheels}')
    dest = os.path.join(out_dir, wheels[0])
    shutil.move(os.path.join(tmp, wheels[0]), dest)
    shutil.rmtree(tmp, ignore_errors=True)
    return dest


def fetch_pyodide(dest):
    os.makedirs(dest, exist_ok=True)
    base = f'https://cdn.jsdelivr.net/pyodide/{PYODIDE}/full/'
    for name in PYODIDE_FILES:
        out = os.path.join(dest, name)
        if os.path.exists(out):
            continue
        print(f'  받는 중 {name}', flush=True)
        req = urllib.request.Request(base + name, headers={
            'User-Agent': 'hanpatch-deploy/1'})
        with urllib.request.urlopen(req, timeout=300) as r, \
                open(out + '.tmp', 'wb') as f:
            shutil.copyfileobj(r, f)
        os.replace(out + '.tmp', out)


def main():
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument('--root', required=True, help='서빙되는 디렉터리')
    ap.add_argument('--fetch-pyodide', action='store_true')
    ap.add_argument('--selftest', action='store_true',
                    help='자체 검사 페이지도 함께 배포한다')
    a = ap.parse_args()

    os.makedirs(a.root, exist_ok=True)
    wheels_dir = os.path.join(a.root, 'wheels')
    os.makedirs(wheels_dir, exist_ok=True)
    for old in os.listdir(wheels_dir):
        if old.endswith('.whl'):
            os.remove(os.path.join(wheels_dir, old))
    wheel = build_wheel(wheels_dir)
    print(f'휠: {os.path.basename(wheel)} {os.path.getsize(wheel)} bytes')

    vendor = os.path.join(a.root, 'vendor', 'pyodide')
    if a.fetch_pyodide:
        fetch_pyodide(vendor)
    missing = [f for f in PYODIDE_FILES
               if not os.path.exists(os.path.join(vendor, f))]
    if missing:
        raise SystemExit(f'pyodide 파일이 없습니다({vendor}): {missing[:3]} … '
                         '--fetch-pyodide 를 주세요')

    copied = []
    for name in APP_FILES:
        src = os.path.join(APP, name)
        dst = os.path.join(a.root, name)
        shutil.copyfile(src, dst)
        copied.append(name)
    # 워커는 vendor/wheels 를 자기 위치 기준으로 찾는다. 배포 트리에서는
    # 페이지와 같은 디렉터리에 있으므로 경로가 그대로 맞는다.

    if a.selftest:
        st = os.path.join(a.root, 'selftest')
        os.makedirs(st, exist_ok=True)
        for name in os.listdir(os.path.join(APP, 'selftest')):
            shutil.copyfile(os.path.join(APP, 'selftest', name),
                            os.path.join(st, name))
            copied.append('selftest/' + name)
    else:
        shutil.rmtree(os.path.join(a.root, 'selftest'), ignore_errors=True)

    entries = []
    for rel in sorted(copied + [f'wheels/{os.path.basename(wheel)}']
                      + [f'vendor/pyodide/{f}' for f in PYODIDE_FILES]):
        p = os.path.join(a.root, rel)
        entries.append({'path': rel, 'size': os.path.getsize(p),
                        'sha256': sha256(p)})
    manifest = {
        'tool': 'hanpatch browser patcher',
        'pyodide': PYODIDE,
        'wheel': os.path.basename(wheel),
        'source_commit': subprocess.run(
            ['git', '-C', HERE, 'rev-parse', 'HEAD'], capture_output=True,
            text=True).stdout.strip() or None,
        'files': entries,
    }
    with open(os.path.join(a.root, 'build-manifest.json'), 'w',
              encoding='utf-8') as f:
        json.dump(manifest, f, indent=1, ensure_ascii=False)
        f.write('\n')
    total = sum(e['size'] for e in entries)
    print(f'{len(entries)}개 파일, {total / 1e6:.1f} MB → {a.root}')
    print('build-manifest.json 갱신됨')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
