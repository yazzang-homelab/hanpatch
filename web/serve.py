#!/usr/bin/env python3
"""개발용 정적 서버 — 배포와 같은 헤더로 `web/apply` 를 띄운다.

SharedArrayBuffer 는 cross-origin isolation 없이는 존재하지 않고, OPFS 동기 접근
핸들은 보안 컨텍스트가 아니면 없다. 그래서 로컬 확인도 COOP/COEP 를 붙인 채
127.0.0.1(보안 컨텍스트 예외) 로 해야 프로덕션과 같은 조건이 된다.

    python3 web/serve.py --port 8123 --vendor /mnt/ssd256/krpatch-web

`--vendor` 는 배포 트리(pyodide 번들과 휠)를 얹는 곳이다. 저장소의 `web/apply`
파일이 우선하고, 없으면 vendor 트리에서 찾는다.
"""
import argparse
import http.server
import os
import posixpath
import socketserver
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


class Handler(http.server.SimpleHTTPRequestHandler):
    roots = ()

    def log_message(self, fmt, *a):
        if '"GET' in (fmt % a) and ' 200 ' in (fmt % a):
            return
        sys.stderr.write('%s\n' % (fmt % a))

    def translate_path(self, path):
        rel = posixpath.normpath(urlsplit_path(path)).lstrip('/')
        for root in self.roots:
            p = os.path.join(root, rel)
            if os.path.exists(p):
                return p
        return os.path.join(self.roots[0], rel)

    def end_headers(self):
        self.send_header('Cross-Origin-Opener-Policy', 'same-origin')
        self.send_header('Cross-Origin-Embedder-Policy', 'require-corp')
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()


def urlsplit_path(path):
    return path.split('?', 1)[0].split('#', 1)[0]


def main():
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument('--port', type=int, default=8123)
    ap.add_argument('--vendor', default='/mnt/ssd256/krpatch-web')
    ap.add_argument('--extra', action='append', default=[],
                    help='추가로 얹을 트리(픽스처 등)')
    a = ap.parse_args()
    Handler.roots = tuple([os.path.join(HERE, 'apply'), a.vendor] + a.extra)
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(('127.0.0.1', a.port), Handler) as s:
        print(f'http://127.0.0.1:{a.port}/  roots={Handler.roots}',
              flush=True)
        s.serve_forever()


if __name__ == '__main__':
    main()
