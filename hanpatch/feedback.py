"""Reader feedback on single script lines, and the operator-gated work orders
that turn that feedback into translation fixes.

Two surfaces, one database:

  public  (proxied on the book's own origin)  read counts, read a line's thread,
          post feedback, vote, retract your own post. Nothing else.
  admin   (loopback only)  triage, and assemble a WORK ORDER from selected
          feedback.

The split is the whole point. Readers never see triage state, and the pipeline
never reads raw feedback: `apply_order` refuses any order that a human has not
approved through the admin surface, and refuses any key the approved order does
not name. That is a procedure with a code gate, not a security boundary - a
process running as root can always write the override file directly. The gate
exists so that no automated step can turn a stranger's comment into shipped
text without a person putting their name on it.

The search index is built here too, because a reader who cannot find a line
cannot report it: the book is 371 pages, so an in-page filter finds nothing.
"""
import hashlib
import html as _html
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# The reader picks one of these. Free text alone buries the actionable part of a
# report under prose; the kind is what the work order groups by.
KINDS = {
    'mistranslation': '오역 (뜻이 다름)',
    'awkward': '어색한 문장',
    'typo': '오타 · 맞춤법',
    'name': '이름 · 고유명 표기',
    'layout': '줄바꿈 · 글자 깨짐',
    'register': '높임말 · 말투',
    'other': '기타',
}
STATUSES = ('open', 'accepted', 'rejected', 'dup', 'hold', 'done')
ORDER_STATUSES = ('draft', 'approved', 'applied', 'void')

BODY_MAX = 1000
SUGGEST_MAX = 600
NICK_MAX = 24
SEARCH_MAX = 200
# joins the two searchable renderings of one row inside a single indexed column:
# the text as the book prints it, and the text as the screen shows it. A reader's
# query never contains this character, so no match can straddle the two.
VARIANT_SEP = '\u2016'
# What a reader typed as one word may be two fields in the container. The item
# menu stores '안초버;모래' and the screen shows them adjacent, so the separator
# has to be gone from at least one indexed rendering.
JOIN_CHARS = ';'
# Korean particles cling to the word a reader is actually looking for. Term
# search retries each term without them; it never rewrites the reader's phrase.
JOSA = ('에게서', '으로서', '이라고', '에서', '에게', '으로', '하고', '보다',
        '부터', '까지', '처럼', '만큼', '은', '는', '이', '가', '을', '를', '의', '에',
        '도', '와', '과', '로', '만', '랑', '이랑')

# per-IP posting budget: (window seconds, allowed posts). The old hourly cap of
# 15 cut off readers who were doing exactly what we asked - one reader hit it
# after ~20 reports in a single sitting. A person proofreading a chapter can
# easily file a hundred; the burst cap is what actually stops a script, so the
# minute stays tight while the hour and the day are set well above a human
# session.
BUDGETS = ((60, 12), (3600, 120), (86400, 400))
# the same reader re-posting on the same line within this window is a double
# submit, not a second opinion
DEDUPE_SECONDS = 600

SCHEMA = """
pragma journal_mode=wal;
create table if not exists meta(k text primary key, v text not null);
create table if not exists lines(
  id integer primary key,
  fam text not null,
  key text not null,
  page text not null,
  anchor text not null,
  section text not null,
  src text not null,
  ko text not null,
  src_flat text not null,
  ko_flat text not null);
create unique index if not exists lines_fk on lines(fam, key);
create virtual table if not exists lines_fts using fts5(
  ko_flat, src_flat, content='lines', content_rowid='id', tokenize='trigram');
create table if not exists feedback(
  id integer primary key,
  line_id integer not null,
  created text not null,
  kind text not null,
  body text not null,
  suggest text not null default '',
  nick text not null default '',
  ip_hash text not null default '',
  ua text not null default '',
  status text not null default 'open',
  visible integer not null default 1,
  admin_note text not null default '',
  order_id text not null default '',
  token text not null default '');
create index if not exists feedback_line on feedback(line_id);
create index if not exists feedback_status on feedback(status);
create table if not exists votes(
  line_id integer not null,
  ip_hash text not null,
  created text not null,
  primary key(line_id, ip_hash));
create table if not exists posts(ip_hash text not null, ts real not null);
create index if not exists posts_ts on posts(ts);
create table if not exists orders(
  id text primary key,
  created text not null,
  title text not null,
  note text not null default '',
  status text not null default 'draft',
  approved_at text not null default '',
  approved_by text not null default '',
  applied_at text not null default '',
  payload text not null);
"""


def now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def flatten(s):
    """One searchable line: markup out, whitespace collapsed, casefolded.

    Readers search for what they SAW on screen, so engine tokens must not sit
    A digit-prefixed brace is furigana - a reading
    printed ABOVE the kanji - so dropping it is what makes `旅立ち` findable in
    `旅立{2たびだ}ち`. A brace without the digit is a substitution token, which the
    book prints verbatim and `screen()` renders separately, so it is kept as
    written: searching `{HERO}` must find the rows that carry it.
    """
    s = re.sub(r'<[^>\n]*>', ' ', s or '')
    s = re.sub(r'\{[0-9][^}]*\}', '', s)
    s = s.replace('\u3000', ' ')
    s = re.sub(r'\s+', ' ', s)
    return s.strip().casefold()


def screen(s, subs=None):
    """The row as the SCREEN shows it, for readers who search what they played.

    A container row is a template: `{HERO}는 {I_NAME}을 받았다!` is never on
    screen, so a reader who types the sentence they saw matches nothing. Declared
    substitutions become their real text; the rest are dropped, because an
    undeclared placeholder has no rendering this code is entitled to invent.
    """
    s = re.sub(r'<[^>\n]*>', ' ', s or '')
    s = re.sub(r'\{[0-9][^}]*\}', '', s)
    for tag, val in (subs or {}).items():
        s = s.replace(tag, val)
    s = re.sub(r'\{[^}]*\}', '', s)
    for ch in JOIN_CHARS:
        s = s.replace(ch, '')
    s = s.replace('\u3000', ' ')
    return re.sub(r'\s+', ' ', s).strip().casefold()


def indexed_text(s, subs=None):
    """Both renderings in one column, so one query reaches either."""
    a = flatten(s)
    b = screen(s, subs)
    return a if b == a else f'{a} {VARIANT_SEP} {b}'


def strip_josa(term):
    for j in JOSA:
        if term.endswith(j) and len(term) > len(j) + 1:
            return term[:-len(j)]
    return term


class Store:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        self._local = threading.local()
        self._write = threading.Lock()
        with self.conn() as c:
            c.executescript(SCHEMA)
        self.salt = self._salt()

    def conn(self):
        c = getattr(self._local, 'c', None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=15)
            c.row_factory = sqlite3.Row
            c.execute('pragma busy_timeout=15000')
            self._local.c = c
        return c

    def _salt(self):
        with self._write, self.conn() as c:
            row = c.execute("select v from meta where k='ip_salt'").fetchone()
            if row:
                return row['v']
            v = secrets.token_hex(16)
            c.execute("insert into meta(k,v) values('ip_salt',?)", (v,))
            return v

    # IPs are evidence of abuse, not of identity, and this box has no reason to
    # keep them. The salted hash still groups a flood; it does not name a reader.
    def iphash(self, ip):
        return hashlib.sha256((self.salt + '|' + (ip or '')).encode()).hexdigest()[:32]

    # ------------------------------------------------------------------ index
    def index(self, rows, subs=None):
        """rows: (fam, key, page, anchor, section, src, ko), in book order."""
        with self._write, self.conn() as c:
            keep = {(r['fam'], r['key']): r['id']
                    for r in c.execute('select id, fam, key from lines')}
            c.execute('delete from lines_fts')
            c.execute('delete from lines')
            n = 0
            for fam, key, page, anchor, section, src, ko in rows:
                n += 1
                rid = keep.get((fam, key), None)
                kf, sf = indexed_text(ko, subs), indexed_text(src, subs)
                cur = c.execute(
                    'insert into lines(id,fam,key,page,anchor,section,src,ko,'
                    'src_flat,ko_flat) values(?,?,?,?,?,?,?,?,?,?)',
                    (rid, fam, key, page, anchor, section, src, ko, sf, kf))
                rid = rid if rid is not None else cur.lastrowid
                c.execute('insert into lines_fts(rowid,ko_flat,src_flat) '
                          'values(?,?,?)', (rid, kf, sf))
            c.execute("insert into meta(k,v) values('indexed',?) "
                      "on conflict(k) do update set v=excluded.v", (now(),))
            c.execute("insert into meta(k,v) values('lines',?) "
                      "on conflict(k) do update set v=excluded.v", (str(n),))
        return n

    def stat(self):
        c = self.conn()
        return {
            'lines': c.execute('select count(*) n from lines').fetchone()['n'],
            'feedback': c.execute(
                'select count(*) n from feedback where visible=1').fetchone()['n'],
            'indexed': (c.execute("select v from meta where k='indexed'")
                        .fetchone() or {'v': ''})['v'],
        }

    def line(self, fam, key):
        return self.conn().execute(
            'select * from lines where fam=? and key=?', (fam, key)).fetchone()

    # ----------------------------------------------------------------- search
    def search(self, q, mode='all', limit=40, offset=0):
        q = (q or '').strip().casefold()
        q = re.sub(r'\s+', ' ', q)
        if not q:
            return {'total': 0, 'hits': [], 'q': ''}
        limit = max(1, min(int(limit or 40), SEARCH_MAX))
        offset = max(0, int(offset or 0))
        cols = {'ko': ['ko_flat'], 'src': ['src_flat']}.get(
            mode, ['ko_flat', 'src_flat'])
        c = self.conn()
        if len(q) >= 3:
            # trigram FTS needs three characters. Quoting makes the whole query a
            # phrase, so a substring matches and a stray quote cannot become
            # query syntax.
            phrase = '"%s"' % q.replace('"', '""')
            expr = ' OR '.join(f'{col}:{phrase}' for col in cols)
            total = c.execute('select count(*) n from lines_fts '
                              'where lines_fts match ?', (expr,)).fetchone()['n']
            rows = c.execute(
                'select l.* from lines_fts f join lines l on l.id=f.rowid '
                'where f.lines_fts match ? order by l.id limit ? offset ?',
                (expr, limit, offset)).fetchall()
        else:
            # One or two characters cannot be indexed by trigram, and refusing
            # them would break searching for a single-syllable name. A scan of
            # this table is milliseconds; it is the honest fallback.
            like = '%' + q.replace('\\', '\\\\').replace(
                '%', '\\%').replace('_', '\\_') + '%'
            where = ' or '.join(f"{col} like ? escape '\\'" for col in cols)
            args = [like] * len(cols)
            total = c.execute(f'select count(*) n from lines where {where}',
                              args).fetchone()['n']
            rows = c.execute(
                f'select * from lines where {where} order by id limit ? offset ?',
                args + [limit, offset]).fetchall()
        if not total and len(q.split()) > 1:
            # A sentence copied off the screen crosses rows: the item name in
            # `{HERO}는 {I_NAME}을 받았다!` lives in the item menu, not in the line.
            # No phrase can match that, so fall back to the words themselves
            # instead of telling the reader the line does not exist.
            return self.search_terms(q, mode, limit, offset)
        ids = [r['id'] for r in rows]
        counts = self._counts_for(ids)
        return {
            'q': q, 'total': total, 'offset': offset, 'mode': mode, 'match': 'phrase',
            'hits': [{
                'fam': r['fam'], 'key': r['key'], 'page': r['page'],
                'anchor': r['anchor'], 'section': r['section'],
                'ko': r['ko'], 'src': r['src'],
                'n': counts.get(r['id'], (0, 0))[0],
                'v': counts.get(r['id'], (0, 0))[1],
            } for r in rows],
        }

    TERM_CAP = 4000

    def search_terms(self, q, mode='all', limit=40, offset=0):
        """Rank rows by how many of the reader's words they carry.

        Ordering is by matched LENGTH, then by book order. An AND returns nothing
        for the very query that needs this path, an unscored OR buries the line
        under every row containing `받았다`, and a plain count ranks two common
        words above one long distinctive one - so `안초뱄모래` has to weigh more
        than `받았다`.
        """
        terms = [t for t in re.split(r'[\s\u3000]+', q) if len(t) >= 2][:8]
        if not terms:
            return {'q': q, 'total': 0, 'hits': [], 'offset': 0, 'mode': mode,
                    'match': 'terms', 'terms': []}
        score = {}
        weight = {}
        hit_terms = {}
        for term in terms:
            found = set()
            cand_len = len(term)
            for cand in (term, strip_josa(term)):
                found.update(self._rowids(cand, mode))
                if found:
                    cand_len = len(cand)
                    break
            for rid in found:
                score[rid] = score.get(rid, 0) + 1
                weight[rid] = weight.get(rid, 0) + cand_len
                hit_terms.setdefault(rid, []).append(term)
        if not score:
            return {'q': q, 'total': 0, 'hits': [], 'offset': 0, 'mode': mode,
                    'match': 'terms', 'terms': terms}
        ranked = sorted(score, key=lambda r: (-weight[r], -score[r], r))
        total = len(ranked)
        page = ranked[offset:offset + limit]
        rows = {r['id']: r for r in self.conn().execute(
            'select * from lines where id in (%s)' % ','.join('?' * len(page)),
            page)} if page else {}
        counts = self._counts_for(page)
        return {
            'q': q, 'total': total, 'offset': offset, 'mode': mode,
            'match': 'terms', 'terms': terms,
            'hits': [{
                'fam': rows[i]['fam'], 'key': rows[i]['key'],
                'page': rows[i]['page'], 'anchor': rows[i]['anchor'],
                'section': rows[i]['section'], 'ko': rows[i]['ko'],
                'src': rows[i]['src'], 'score': score[i],
                'matched': hit_terms[i],
                'n': counts.get(i, (0, 0))[0], 'v': counts.get(i, (0, 0))[1],
            } for i in page if i in rows],
        }

    def _rowids(self, term, mode):
        cols = {'ko': ['ko_flat'], 'src': ['src_flat']}.get(
            mode, ['ko_flat', 'src_flat'])
        c = self.conn()
        if len(term) >= 3:
            phrase = '"%s"' % term.replace('"', '""')
            expr = ' OR '.join(f'{col}:{phrase}' for col in cols)
            return [r[0] for r in c.execute(
                'select rowid from lines_fts where lines_fts match ? limit ?',
                (expr, self.TERM_CAP))]
        like = '%' + term.replace('\\', '\\\\').replace(
            '%', '\\%').replace('_', '\\_') + '%'
        where = ' or '.join(f"{col} like ? escape '\\'" for col in cols)
        return [r[0] for r in c.execute(
            f'select id from lines where {where} limit ?',
            [like] * len(cols) + [self.TERM_CAP])]

    def _counts_for(self, ids):
        if not ids:
            return {}
        marks = ','.join('?' * len(ids))
        out = {}
        for r in self.conn().execute(
                f'select line_id, count(*) n from feedback where visible=1 '
                f'and line_id in ({marks}) group by line_id', ids):
            out[r['line_id']] = (r['n'], 0)
        for r in self.conn().execute(
                f'select line_id, count(*) n from votes '
                f'where line_id in ({marks}) group by line_id', ids):
            n, _ = out.get(r['line_id'], (0, 0))
            out[r['line_id']] = (n, r['n'])
        return out

    def section_counts(self, fam):
        c = self.conn()
        out = {}
        for r in c.execute(
                'select l.key k, count(f.id) n from lines l '
                'join feedback f on f.line_id=l.id and f.visible=1 '
                'where l.fam=? group by l.key', (fam,)):
            out[r['k']] = {'n': r['n'], 'v': 0}
        for r in c.execute(
                'select l.key k, count(v.ip_hash) n from lines l '
                'join votes v on v.line_id=l.id where l.fam=? group by l.key',
                (fam,)):
            out.setdefault(r['k'], {'n': 0, 'v': 0})['v'] = r['n']
        return out

    def thread(self, fam, key):
        ln = self.line(fam, key)
        if ln is None:
            return None
        c = self.conn()
        items = [{
            'id': r['id'], 'created': r['created'], 'kind': r['kind'],
            'nick': r['nick'], 'body': r['body'], 'suggest': r['suggest'],
            # readers see only whether the operator has taken the line on, never
            # the triage vocabulary
            'taken': r['status'] in ('accepted', 'done'),
            'fixed': r['status'] == 'done',
        } for r in c.execute(
            'select * from feedback where line_id=? and visible=1 '
            'order by id', (ln['id'],))]
        votes = c.execute('select count(*) n from votes where line_id=?',
                          (ln['id'],)).fetchone()['n']
        return {'fam': fam, 'key': key, 'items': items, 'votes': votes}

    # ------------------------------------------------------------------ write
    def budget_left(self, ip_hash):
        c = self.conn()
        t = time.time()
        for window, allowed in BUDGETS:
            n = c.execute('select count(*) n from posts where ip_hash=? and ts>?',
                          (ip_hash, t - window)).fetchone()['n']
            if n >= allowed:
                return window
        return 0

    def submit(self, fam, key, kind, body, suggest='', nick='', ip='', ua=''):
        body = (body or '').strip()
        suggest = (suggest or '').strip()
        nick = re.sub(r'\s+', ' ', (nick or '').strip())[:NICK_MAX]
        if kind not in KINDS:
            return {'error': 'kind', 'message': '피드백 유형을 골라 주세요.'}
        if len(body) < 2:
            return {'error': 'body', 'message': '어떤 점이 문제인지 적어 주세요.'}
        if len(body) > BODY_MAX or len(suggest) > SUGGEST_MAX:
            return {'error': 'length',
                    'message': f'내용은 {BODY_MAX}자, 제안은 {SUGGEST_MAX}자까지 쓸 수 있습니다.'}
        ln = self.line(fam, key)
        if ln is None:
            return {'error': 'line', 'message': '그 대사를 찾을 수 없습니다.'}
        iph = self.iphash(ip)
        window = self.budget_left(iph)
        if window:
            wait = ('1분' if window <= 60 else
                    '한 시간' if window <= 3600 else '하루')
            return {'error': 'rate',
                    'message': f'짧은 시간에 너무 많이 보냈습니다. {wait} 뒤에 다시 시도해 주세요.'}
        c = self.conn()
        dup = c.execute(
            'select id from feedback where line_id=? and ip_hash=? and created>? '
            'order by id desc limit 1',
            (ln['id'], iph,
             time.strftime('%Y-%m-%dT%H:%M:%SZ',
                           time.gmtime(time.time() - DEDUPE_SECONDS)))).fetchone()
        if dup:
            return {'error': 'dup', 'message': '이미 이 대사에 방금 의견을 남겼습니다.'}
        token = secrets.token_urlsafe(12)
        with self._write, c:
            cur = c.execute(
                'insert into feedback(line_id,created,kind,body,suggest,nick,'
                'ip_hash,ua,token) values(?,?,?,?,?,?,?,?,?)',
                (ln['id'], now(), kind, body, suggest, nick, iph,
                 (ua or '')[:120], token))
            c.execute('insert into posts(ip_hash,ts) values(?,?)', (iph, time.time()))
            c.execute('delete from posts where ts<?', (time.time() - 86400 * 2,))
        return {'id': cur.lastrowid, 'token': token}

    def vote(self, fam, key, ip=''):
        ln = self.line(fam, key)
        if ln is None:
            return {'error': 'line', 'message': '그 대사를 찾을 수 없습니다.'}
        iph = self.iphash(ip)
        c = self.conn()
        with self._write, c:
            try:
                c.execute('insert into votes(line_id,ip_hash,created) values(?,?,?)',
                          (ln['id'], iph, now()))
            except sqlite3.IntegrityError:
                c.execute('delete from votes where line_id=? and ip_hash=?',
                          (ln['id'], iph))
        n = c.execute('select count(*) n from votes where line_id=?',
                      (ln['id'],)).fetchone()['n']
        return {'votes': n}

    def retract(self, fid, token):
        c = self.conn()
        row = c.execute('select * from feedback where id=?', (fid,)).fetchone()
        if row is None or not token or row['token'] != token:
            return {'error': 'token', 'message': '이 의견을 지울 권한이 없습니다.'}
        if row['order_id']:
            return {'error': 'ordered',
                    'message': '이미 수정 작업에 반영된 의견은 지울 수 없습니다.'}
        with self._write, c:
            c.execute('update feedback set visible=0 where id=?', (fid,))
        return {'ok': True}

    # ------------------------------------------------------------------ admin
    def queue(self, status='open', kind='', limit=200, offset=0, q=''):
        where = ['1=1']
        args = []
        if status and status != 'all':
            where.append('f.status=?')
            args.append(status)
        if kind:
            where.append('f.kind=?')
            args.append(kind)
        if q:
            where.append('(f.body like ? or f.suggest like ? or l.ko like ? '
                         'or l.key like ?)')
            args += ['%' + q + '%'] * 4
        sql = ('select f.*, l.fam, l.key, l.section, l.page, l.anchor, l.src, l.ko, '
               '(select count(*) from votes v where v.line_id=l.id) votes '
               'from feedback f join lines l on l.id=f.line_id '
               'where ' + ' and '.join(where) +
               ' order by votes desc, f.id desc limit ? offset ?')
        rows = self.conn().execute(sql, args + [int(limit), int(offset)]).fetchall()
        return [dict(r) for r in rows]

    def counts_by_status(self):
        return {r['status']: r['n'] for r in self.conn().execute(
            'select status, count(*) n from feedback where visible=1 '
            'group by status')}

    def triage(self, ids, status=None, note=None, visible=None):
        if status is not None and status not in STATUSES:
            raise ValueError('unknown status')
        ids = [int(i) for i in ids]
        if not ids:
            return 0
        marks = ','.join('?' * len(ids))
        sets, args = [], []
        if status is not None:
            sets.append('status=?')
            args.append(status)
        if note is not None:
            sets.append('admin_note=?')
            args.append(note[:2000])
        if visible is not None:
            sets.append('visible=?')
            args.append(1 if visible else 0)
        if not sets:
            return 0
        c = self.conn()
        with self._write, c:
            cur = c.execute(f'update feedback set {", ".join(sets)} '
                            f'where id in ({marks})', args + ids)
        return cur.rowcount

    # ------------------------------------------------------------------ order
    def make_order(self, ids, title='', note=''):
        """Freeze the selected feedback into a draft work order.

        The order carries the source row, the CURRENTLY SHIPPED Korean and the
        reader complaints - everything a translator needs and nothing that
        identifies a reader. It is a draft: `approve` is a separate, human act.
        """
        ids = [int(i) for i in ids]
        if not ids:
            raise ValueError('no feedback selected')
        marks = ','.join('?' * len(ids))
        rows = self.conn().execute(
            f'select f.*, l.fam, l.key, l.section, l.page, l.anchor, l.src, l.ko, '
            f'(select count(*) from votes v where v.line_id=l.id) votes '
            f'from feedback f join lines l on l.id=f.line_id '
            f'where f.id in ({marks}) and f.visible=1 order by l.id, f.id',
            ids).fetchall()
        if not rows:
            raise ValueError('selected feedback does not exist')
        by_line = {}
        for r in rows:
            fk = f'{r["fam"]}/{r["key"]}'
            item = by_line.setdefault(fk, {
                'fam': r['fam'], 'key': r['key'], 'section': r['section'],
                'page': r['page'], 'anchor': r['anchor'],
                'src': r['src'], 'ko_current': r['ko'], 'votes': r['votes'],
                'feedback': [],
            })
            item['feedback'].append({
                'id': r['id'], 'kind': r['kind'], 'kind_ko': KINDS.get(r['kind'], r['kind']),
                'body': r['body'], 'suggest': r['suggest'],
                'operator_note': r['admin_note'],
            })
        oid = time.strftime('ord-%Y%m%d-%H%M%S', time.gmtime())
        payload = {
            'id': oid,
            'created': now(),
            'title': title or f'{len(by_line)}개 대사 수정',
            'note': note,
            'rows': list(by_line.values()),
        }
        c = self.conn()
        with self._write, c:
            c.execute('insert into orders(id,created,title,note,status,payload) '
                      'values(?,?,?,?,?,?)',
                      (oid, payload['created'], payload['title'], note, 'draft',
                       json.dumps(payload, ensure_ascii=False)))
            c.execute(f'update feedback set order_id=?, status=case when '
                      f'status=\'open\' then \'accepted\' else status end '
                      f'where id in ({marks})', [oid] + ids)
        return payload

    def order(self, oid):
        row = self.conn().execute('select * from orders where id=?', (oid,)).fetchone()
        return dict(row) if row else None

    def orders(self, limit=100):
        return [dict(r) for r in self.conn().execute(
            'select id,created,title,status,approved_by,approved_at,applied_at '
            'from orders order by created desc limit ?', (int(limit),))]

    def approve_order(self, oid, by, outdir=None):
        """The human act. Nothing downstream runs without this row."""
        row = self.order(oid)
        if row is None:
            raise ValueError(f'no such order: {oid}')
        if row['status'] != 'draft':
            raise ValueError(f'order {oid} is {row["status"]}, not a draft')
        by = (by or '').strip()
        if not by:
            raise ValueError('approval needs a name')
        payload = json.loads(row['payload'])
        payload['approved_by'] = by
        payload['approved_at'] = now()
        payload['status'] = 'approved'
        c = self.conn()
        with self._write, c:
            c.execute('update orders set status=?, approved_by=?, approved_at=?, '
                      'payload=? where id=?',
                      ('approved', by, payload['approved_at'],
                       json.dumps(payload, ensure_ascii=False), oid))
        if outdir:
            os.makedirs(outdir, exist_ok=True)
            with open(os.path.join(outdir, f'{oid}.json'), 'w', encoding='utf-8') as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=1)
                fh.write('\n')
            with open(os.path.join(outdir, f'{oid}.md'), 'w', encoding='utf-8') as fh:
                fh.write(order_md(payload))
        return payload

    def void_order(self, oid):
        row = self.order(oid)
        if row is None:
            raise ValueError(f'no such order: {oid}')
        if row['status'] == 'applied':
            raise ValueError('an applied order cannot be voided')
        c = self.conn()
        with self._write, c:
            c.execute('update orders set status=? where id=?', ('void', oid))
            c.execute("update feedback set order_id='', status='open' "
                      "where order_id=? and status='accepted'", (oid,))
        return True


def order_md(payload):
    """The work order as the text a translator or an agent is handed.

    It states the authorisation on the first screen, because an order without a
    named approver must not be actioned and the reader of this file is the one
    who has to notice.
    """
    st = payload.get('status', 'draft')
    out = [f'# 번역 수정 작업지시서 · {payload["id"]}',
           '',
           f'- 제목: {payload.get("title", "")}',
           f'- 작성: {payload.get("created", "")}',
           f'- 상태: **{st}**',
           f'- 승인: {payload.get("approved_by") or "미승인"}'
           + (f' ({payload["approved_at"]})' if payload.get('approved_at') else ''),
           f'- 대상 대사: {len(payload.get("rows", []))}개',
           '']
    if st != 'approved':
        out += ['> **이 지시서는 승인되지 않았다. 아무것도 수정하지 말 것.**', '']
    if payload.get('note'):
        out += ['## 운영자 지시', '', payload['note'], '']
    out += ['## 작업 규칙', '',
            '1. 아래 열거된 키만 수정한다. 열거되지 않은 키는 건드리지 않는다.',
            '2. 수정문은 `fixes.json`에 `{"패밀리/키": "새 번역"}` 형태로 쓴다.',
            '3. 반영은 `hanpatch feedback apply <지시서 ID> --fixes fixes.json`으로만 한다.',
            '   이 명령은 승인된 지시서가 아니면 거부하고, 지시서에 없는 키도 거부한다.',
            '4. 용어집·용량·줄바꿈 규칙 검증은 반영 단계에서 자동으로 다시 돌린다.',
            '   검증에서 걸린 줄은 반영되지 않으므로 고쳐서 다시 제출한다.', '']
    for i, r in enumerate(payload.get('rows', []), 1):
        out += [f'### {i}. `{r["fam"]}/{r["key"]}`', '',
                f'- 위치: {r.get("section", "")} · `{r.get("page", "")}#{r.get("anchor", "")}`',
                f'- 공감: {r.get("votes", 0)}', '',
                '원문',
                '```', r['src'].rstrip('\n'), '```', '',
                '현재 번역',
                '```', r['ko_current'].rstrip('\n'), '```', '',
                '독자 지적']
        for f in r['feedback']:
            out.append(f'- ({f["kind_ko"]}) {f["body"]}')
            if f.get('suggest'):
                out.append(f'  - 제안: {f["suggest"]}')
            if f.get('operator_note'):
                out.append(f'  - 운영자 메모: {f["operator_note"]}')
        out.append('')
    return '\n'.join(out) + '\n'


def apply_order(store, oid, fixes, outdir=None):
    """Write the revised Korean into the manifest override, under the gate.

    Refuses unless a human approved this order, and refuses any key the order
    does not name. Every value goes through the SAME ruleset the manifest build
    uses, so a fix that breaks capacity, the glossary or the layout budget is
    rejected here instead of at the seal.
    """
    from hanpatch import capacity as capmod
    from hanpatch import config, glossary, translate

    row = store.order(oid)
    if row is None:
        return {'ok': False, 'error': f'no such order: {oid}'}
    if row['status'] != 'approved':
        return {'ok': False,
                'error': f'order {oid} is {row["status"]}. 승인된 지시서만 반영할 수 있다.'}
    payload = json.loads(row['payload'])
    allowed = {f'{r["fam"]}/{r["key"]}': r for r in payload['rows']}
    stray = [k for k in fixes if k not in allowed]
    if stray:
        return {'ok': False, 'error': 'order does not cover: ' + ', '.join(sorted(stray))}
    if not fixes:
        return {'ok': False, 'error': 'no fixes given'}

    src = config.load_object(config.src_path(), 'the extracted source')
    gl = glossary.load()
    srcrow = {(fam, it['key']): it for fam, items in src.items() for it in items}
    ovpath = config.out(f'text_{config.target()}.json')
    override = (config.load_object(ovpath, 'the manifest override')
                if os.path.exists(ovpath) else {})

    applied, problems = {}, []
    for fk, ko in sorted(fixes.items()):
        fam, key = fk.split('/', 1)
        it = srcrow.get((fam, key))
        if it is None:
            problems.append(f'{fk} :: not in the extracted source')
            continue
        ko2, probs = translate.check(it['en'], ko,
                                     glossary.relevant(gl, [it['en']], fam),
                                     fam, capmod.group(fam, key))
        if probs:
            problems.append(f'{fk} :: {probs}')
            continue
        applied[fk] = ko2
    if problems:
        return {'ok': False, 'error': 'ruleset rejected the fixes',
                'problems': problems, 'applied': 0}
    for fk, ko2 in applied.items():
        fam, key = fk.split('/', 1)
        override.setdefault(fam, {})[key] = ko2
    tmp = ovpath + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(override, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, ovpath)

    payload['status'] = 'applied'
    payload['applied_at'] = now()
    payload['applied'] = applied
    c = store.conn()
    with store._write, c:
        c.execute('update orders set status=?, applied_at=?, payload=? where id=?',
                  ('applied', payload['applied_at'],
                   json.dumps(payload, ensure_ascii=False), oid))
        c.execute("update feedback set status='done' where order_id=?", (oid,))
    if outdir:
        os.makedirs(outdir, exist_ok=True)
        with open(os.path.join(outdir, f'{oid}.json'), 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
            fh.write('\n')
    return {'ok': True, 'applied': len(applied), 'override': ovpath,
            'keys': sorted(applied)}


# ------------------------------------------------------------------- indexing
def index_from_book(store):
    """Index exactly what the book renders, so every hit has a page to open."""
    from hanpatch import config
    from hanpatch import scriptbook as sb
    subs = config.prof('placeholder_text') or {}
    src, man, _digest = sb.load()
    sections = sb.dialogue_sections(src, man)
    paged = sum(len(s['rows']) for s in sections.values()) > sb.PAGE_ROW_LIMIT
    rows = []
    for sid, sec in sections.items():
        page = f'p-{sb.slug(sid)}.html' if paged else 'story.html'
        for key, en, ko in sec['rows']:
            rows.append((sid, key, page, row_anchor(sid, key),
                         sec['title_ko'], en, ko))
    for aid, ako, _aen, family, pattern in sb.APPENDICES:
        for key, en, ko in sb.appendix_rows(src, man, family, pattern):
            rows.append((family, key, f'appendix.html#{aid}',
                         row_anchor(family, key), f'부록 · {ako}', en, ko))
    return store.index(rows, subs)


def row_anchor(fam, key):
    """Deep-link target for one row. Stored, never re-derived by the client."""
    from hanpatch import scriptbook as sb
    return sb.row_anchor(fam, key)


# ---------------------------------------------------------------------- HTTP
class _Handler(BaseHTTPRequestHandler):
    server_version = 'hanpatch-feedback'
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass  # nginx already logs; journald does not need a second copy

    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _payload(self):
        n = int(self.headers.get('Content-Length') or 0)
        if n <= 0 or n > 16384:
            return {}
        raw = self.rfile.read(n)
        ctype = (self.headers.get('Content-Type') or '')
        try:
            if 'json' in ctype:
                d = json.loads(raw.decode('utf-8'))
                return d if isinstance(d, dict) else {}
            return {k: v[0] for k, v in parse_qs(raw.decode('utf-8')).items()}
        except Exception:
            return {}

    def _ip(self):
        fwd = self.headers.get('X-Real-IP') or self.headers.get('X-Forwarded-For') or ''
        return fwd.split(',')[0].strip() or self.client_address[0]

    def do_GET(self):
        self._route('GET')

    def do_HEAD(self):
        self._route('GET')

    def do_POST(self):
        self._route('POST')

    def _route(self, method):
        u = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        fn = self.server.routes.get((method, u.path.rstrip('/') or '/'))
        if fn is None:
            for (m, prefix), f in self.server.routes.items():
                if m == method and prefix.endswith('*') and u.path.startswith(prefix[:-1]):
                    fn, qs['_rest'] = f, u.path[len(prefix) - 1:]
                    break
        if fn is None:
            return self._send(404, {'error': 'not found'})
        if not self.server.authorised(self):
            return self._send(403, {'error': 'forbidden'})
        try:
            code, body, ctype = fn(self, qs)
        except Exception as e:                                # noqa: BLE001
            return self._send(500, {'error': e.__class__.__name__, 'message': str(e)})
        return self._send(code, body, ctype) if ctype else self._send(code, body)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, store, routes, token=''):
        super().__init__(addr, _Handler)
        self.store = store
        self.routes = routes
        self.token = token

    def authorised(self, h):
        if not self.token:
            return True
        got = (h.headers.get('X-Admin-Token') or '').strip()
        if not got:
            cookie = h.headers.get('Cookie') or ''
            m = re.search(r'hpfb_token=([^;]+)', cookie)
            got = m.group(1).strip() if m else ''
        if not got:
            got = (parse_qs(urlparse(h.path).query).get('token') or [''])[0]
        return secrets.compare_digest(got, self.token)


# ---- public routes
def _r_stat(h, q):
    return 200, h.server.store.stat(), None


def _r_section(h, q):
    fam = q.get('sec', '')
    if not fam:
        return 400, {'error': 'sec'}, None
    return 200, {'sec': fam, 'counts': h.server.store.section_counts(fam)}, None


def _r_line(h, q):
    t = h.server.store.thread(q.get('sec', ''), q.get('key', ''))
    return (404, {'error': 'line'}, None) if t is None else (200, t, None)


def _r_search(h, q):
    return 200, h.server.store.search(q.get('q', ''), q.get('mode', 'all'),
                                      q.get('limit', 40), q.get('offset', 0)), None


def _r_submit(h, q):
    d = h._payload()
    if (d.get('hp') or '').strip():
        # honeypot: a field no human sees. Answer 200 so a bot does not learn.
        return 200, {'id': 0, 'token': ''}, None
    res = h.server.store.submit(
        d.get('sec', ''), d.get('key', ''), d.get('kind', ''), d.get('body', ''),
        d.get('suggest', ''), d.get('nick', ''), h._ip(),
        h.headers.get('User-Agent', ''))
    return (400 if res.get('error') else 200), res, None


def _r_vote(h, q):
    d = h._payload()
    res = h.server.store.vote(d.get('sec', ''), d.get('key', ''), h._ip())
    return (400 if res.get('error') else 200), res, None


def _r_retract(h, q):
    d = h._payload()
    res = h.server.store.retract(int(d.get('id') or 0), d.get('token', ''))
    return (400 if res.get('error') else 200), res, None


PUBLIC_ROUTES = {
    ('GET', '/api/stat'): _r_stat,
    ('GET', '/api/section'): _r_section,
    ('GET', '/api/line'): _r_line,
    ('GET', '/api/search'): _r_search,
    ('POST', '/api/feedback'): _r_submit,
    ('POST', '/api/vote'): _r_vote,
    ('POST', '/api/retract'): _r_retract,
}


# ---- admin routes
def _a_index(h, q):
    return 200, ADMIN_HTML, 'text/html; charset=utf-8'


def _a_queue(h, q):
    s = h.server.store
    return 200, {
        'items': s.queue(q.get('status', 'open'), q.get('kind', ''),
                         q.get('limit', 200), q.get('offset', 0), q.get('q', '')),
        'counts': s.counts_by_status(),
        'kinds': KINDS,
        'orders': s.orders(30),
        'stat': s.stat(),
    }, None


def _a_triage(h, q):
    d = h._payload()
    n = h.server.store.triage(d.get('ids') or [], d.get('status'), d.get('note'),
                              d.get('visible'))
    return 200, {'updated': n}, None


def _a_order(h, q):
    d = h._payload()
    try:
        p = h.server.store.make_order(d.get('ids') or [], d.get('title', ''),
                                      d.get('note', ''))
    except ValueError as e:
        return 400, {'error': str(e)}, None
    return 200, p, None


def _a_approve(h, q):
    d = h._payload()
    try:
        p = h.server.store.approve_order(d.get('id', ''), d.get('by', ''),
                                         h.server.orderdir)
    except ValueError as e:
        return 400, {'error': str(e)}, None
    return 200, p, None


def _a_void(h, q):
    d = h._payload()
    try:
        h.server.store.void_order(d.get('id', ''))
    except ValueError as e:
        return 400, {'error': str(e)}, None
    return 200, {'ok': True}, None


def _a_order_md(h, q):
    row = h.server.store.order((q.get('_rest') or '').removesuffix('.md'))
    if row is None:
        return 404, {'error': 'no such order'}, None
    return 200, order_md(json.loads(row['payload'])), 'text/plain; charset=utf-8'


ADMIN_ROUTES = {
    ('GET', '/'): _a_index,
    ('GET', '/queue.json'): _a_queue,
    ('GET', '/search'): _r_search,
    ('POST', '/triage'): _a_triage,
    ('POST', '/order'): _a_order,
    ('POST', '/order/approve'): _a_approve,
    ('POST', '/order/void'): _a_void,
    ('GET', '/order/*'): _a_order_md,
}

ADMIN_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<title>대본 피드백 · 내부 처리</title>
<meta name="robots" content="noindex,nofollow">
<style>
:root{--bg:#14120f;--panel:#1e1b16;--ink:#ece5d6;--dim:#9a8f7c;--line:#3a342a;--accent:#c2683f}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.6 system-ui,'Noto Sans KR',sans-serif}
header{position:sticky;top:0;background:#100e0b;border-bottom:1px solid var(--line);
padding:10px 16px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;z-index:5}
b.brand{letter-spacing:.02em}
.dim{color:var(--dim)}
main{padding:16px;max-width:1200px;margin:0 auto}
button,select,input,textarea{font:inherit;background:var(--panel);color:var(--ink);
border:1px solid var(--line);border-radius:4px;padding:5px 9px}
button{cursor:pointer}
button.go{background:var(--accent);border-color:var(--accent);color:#fff}
.item{background:var(--panel);border:1px solid var(--line);border-radius:6px;
padding:10px 12px;margin:0 0 10px}
.item.sel{border-color:var(--accent)}
.meta{display:flex;gap:10px;flex-wrap:wrap;font-size:12px;color:var(--dim);margin-bottom:6px}
pre{white-space:pre-wrap;word-break:break-word;margin:4px 0;font:12px/1.55 ui-monospace,monospace;
background:#171410;border:1px solid var(--line);border-radius:4px;padding:7px 9px}
.body{margin:6px 0}
.sug{border-left:3px solid var(--accent);padding-left:8px;color:#ffd9a6}
.row2{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:6px}
.tag{border:1px solid var(--line);border-radius:99px;padding:1px 8px;font-size:12px}
h2{font-size:15px;margin:22px 0 8px;border-bottom:1px solid var(--line);padding-bottom:5px}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{text-align:left;border-bottom:1px solid var(--line);padding:5px 7px}
.warn{background:#3a1f16;border:1px solid #7a3a22;border-radius:5px;padding:9px 11px;margin:0 0 14px}
</style></head><body>
<header><b class="brand">대본 피드백 · 내부 처리</b>
<span class="dim" id="stat"></span><span style="flex:1"></span>
<select id="fstatus"></select><input id="fq" placeholder="본문·키 검색" size="18">
<button onclick="load()">새로고침</button></header>
<main>
<div class="warn">지시서는 <b>승인</b>을 눌러 이름을 남긴 것만 반영할 수 있다.
에이전트가 자율로 번역을 고치는 경로는 없다 — 반영은
<code>hanpatch feedback apply &lt;ID&gt; --fixes fixes.json</code> 뿐이고, 미승인 지시서와
지시서에 없는 키는 거부된다.</div>
<div class="row2"><button class="go" onclick="mkorder()">선택 항목으로 지시서 초안</button>
<input id="otitle" placeholder="지시서 제목" size="26">
<input id="onote" placeholder="운영자 지시(작업 방향)" size="40">
<button onclick="pick(1)">전체 선택</button><button onclick="pick(0)">선택 해제</button>
<span class="dim" id="selcnt"></span></div>
<h2>피드백</h2><div id="items"></div>
<h2>작업지시서</h2><table id="orders"></table>
</main>
<script>
var sel=new Set(), D={};
function esc(s){var d=document.createElement('div');d.textContent=s==null?'':s;return d.innerHTML}
function j(u,b){return fetch(u,b?{method:'POST',headers:{'Content-Type':'application/json'},
 body:JSON.stringify(b)}:undefined).then(function(r){return r.json()})}
function load(){
 var u='queue.json?status='+encodeURIComponent(document.getElementById('fstatus').value||'open')
  +'&q='+encodeURIComponent(document.getElementById('fq').value||'');
 j(u).then(function(d){D=d;draw()})}
function draw(){
 var st=document.getElementById('fstatus');
 if(!st.options.length){['open','accepted','done','hold','rejected','dup','all'].forEach(function(s){
   var o=document.createElement('option');o.value=o.textContent=s;st.appendChild(o)});
   st.onchange=load}
 document.getElementById('stat').textContent=
  D.stat.lines+'행 색인 · 피드백 '+D.stat.feedback+' · '+JSON.stringify(D.counts);
 var box=document.getElementById('items');box.innerHTML='';
 D.items.forEach(function(it){
  var d=document.createElement('div');d.className='item'+(sel.has(it.id)?' sel':'');
  d.innerHTML='<div class="meta"><b>#'+it.id+'</b><span class="tag">'+esc(D.kinds[it.kind]||it.kind)
   +'</span><span>'+esc(it.fam)+'/'+esc(it.key)+'</span><span>'+esc(it.section)+'</span>'
   +'<span>공감 '+it.votes+'</span><span>'+esc(it.created)+'</span>'
   +'<span>'+esc(it.nick||'익명')+'</span><span class="tag">'+esc(it.status)+'</span>'
   +(it.order_id?'<span class="tag">'+esc(it.order_id)+'</span>':'')+'</div>'
   +'<pre>'+esc(it.src)+'</pre><pre>'+esc(it.ko)+'</pre>'
   +'<div class="body">'+esc(it.body)+'</div>'
   +(it.suggest?'<div class="body sug">제안: '+esc(it.suggest)+'</div>':'')
   +'<div class="row2"><label><input type="checkbox" '+(sel.has(it.id)?'checked':'')
   +' data-id="'+it.id+'"> 선택</label>'
   +'<input placeholder="운영자 메모" value="'+esc(it.admin_note)+'" data-note="'+it.id+'" size="34">'
   +'<button data-s="accepted" data-i="'+it.id+'">채택</button>'
   +'<button data-s="hold" data-i="'+it.id+'">보류</button>'
   +'<button data-s="rejected" data-i="'+it.id+'">반영 안 함</button>'
   +'<button data-s="dup" data-i="'+it.id+'">중복</button>'
   +'<button data-hide="'+it.id+'">숨기기</button></div>';
  box.appendChild(d)});
 box.querySelectorAll('input[type=checkbox]').forEach(function(c){
  c.onchange=function(){c.checked?sel.add(+c.dataset.id):sel.delete(+c.dataset.id);
   c.closest('.item').classList.toggle('sel',c.checked);cnt()}});
 box.querySelectorAll('button[data-s]').forEach(function(b){
  b.onclick=function(){var id=+b.dataset.i;
   var n=box.querySelector('input[data-note="'+id+'"]').value;
   j('triage',{ids:[id],status:b.dataset.s,note:n}).then(load)}});
 box.querySelectorAll('button[data-hide]').forEach(function(b){
  b.onclick=function(){j('triage',{ids:[+b.dataset.hide],visible:false}).then(load)}});
 var t=document.getElementById('orders');
 t.innerHTML='<tr><th>ID</th><th>제목</th><th>상태</th><th>승인</th><th></th></tr>'
  +D.orders.map(function(o){return '<tr><td><a href="order/'+o.id+'.md" target="_blank">'
   +esc(o.id)+'</a></td><td>'+esc(o.title)+'</td><td>'+esc(o.status)+'</td><td>'
   +esc(o.approved_by||'-')+'</td><td>'+(o.status==='draft'
   ?'<button data-ap="'+o.id+'">승인</button> <button data-vo="'+o.id+'">폐기</button>':'')
   +'</td></tr>'}).join('');
 t.querySelectorAll('button[data-ap]').forEach(function(b){
  b.onclick=function(){var who=prompt('승인자 이름 (이 이름이 지시서에 남는다)');
   if(!who)return;j('order/approve',{id:b.dataset.ap,by:who}).then(function(r){
    if(r.error)alert(r.error);load()})}});
 t.querySelectorAll('button[data-vo]').forEach(function(b){
  b.onclick=function(){if(confirm('폐기하면 채택 상태가 open 으로 돌아간다.'))
   j('order/void',{id:b.dataset.vo}).then(load)}});
 cnt()}
function cnt(){document.getElementById('selcnt').textContent=sel.size+'개 선택'}
function pick(on){document.querySelectorAll('#items input[type=checkbox]').forEach(function(c){
 c.checked=!!on;on?sel.add(+c.dataset.id):sel.delete(+c.dataset.id);
 c.closest('.item').classList.toggle('sel',!!on)});cnt()}
function mkorder(){if(!sel.size)return alert('선택된 피드백이 없다.');
 j('order',{ids:[].concat(Array.from(sel)),title:document.getElementById('otitle').value,
  note:document.getElementById('onote').value}).then(function(r){
   if(r.error)return alert(r.error);sel.clear();load();
   alert('초안 '+r.id+' 생성. 검토 후 승인을 눌러야 반영할 수 있다.')})}
load();
</script></body></html>
"""


def serve(store, port=8120, admin_port=None, host='127.0.0.1', token='',
          orderdir=None):
    srv = _Server((host, port), store, PUBLIC_ROUTES)
    srv.orderdir = orderdir
    threads = [threading.Thread(target=srv.serve_forever, daemon=True)]
    admin = None
    if admin_port is not None:
        admin = _Server((host, admin_port), store, ADMIN_ROUTES, token=token)
        admin.orderdir = orderdir
        threads.append(threading.Thread(target=admin.serve_forever, daemon=True))
    for t in threads:
        t.start()
    return srv, admin
