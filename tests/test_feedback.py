"""What the feedback subsystem must not get wrong.

The interesting cases are the refusals: a substring query that finds nothing is
a broken search, an unapproved order that applies is a broken gate, and a reader
who can post 500 times is a broken site.
"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hanpatch import feedback  # noqa: E402

ROWS = [
    ('100000', 'k1', 'p-100000.html', 'r-100000--k1', '100000',
     'エリー「まだ　旅立{2たびだ}ちの　準備{2じゅんび}が　できてないの？',
     'エ리「아직 여행 준비가 안 됐어?」'),
    ('100000', 'k2', 'p-100000.html', 'r-100000--k2', '100000',
     'Of course, all began with one, single gift.',
     '물론, 모든 것은 하나의 선물에서 시작되었다.'),
    ('MENULIST_item_menu', 'm1', 'p-MENULIST_item_menu.html',
     'r-MENULIST_item_menu--m1', 'MENULIST_item_menu',
     '<lineheight=8>Vigor Leaf', '큐어 리프'),
]


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='hpfb')
        self.s = feedback.Store(os.path.join(self.dir, 'fb.db'))
        self.s.index(ROWS)


class TestIndexAndSearch(Base):
    def test_index_counts_every_row(self):
        self.assertEqual(self.s.stat()['lines'], 3)

    def test_partial_word_inside_a_korean_word_matches(self):
        # the whole point of the trigram index: readers type a fragment
        hits = self.s.search('여행 준')['hits']
        self.assertEqual([h['key'] for h in hits], ['k1'])

    def test_partial_word_in_the_source_matches(self):
        self.assertEqual([h['key'] for h in self.s.search('single gif')['hits']],
                         ['k2'])

    def test_two_character_query_still_works(self):
        # below the trigram floor; the LIKE fallback must carry it
        self.assertEqual([h['key'] for h in self.s.search('리프')['hits']], ['m1'])

    def test_markup_does_not_sit_between_the_letters(self):
        self.assertEqual([h['key'] for h in self.s.search('Vigor Leaf')['hits']],
                         ['m1'])

    def test_furigana_braces_are_searchable_as_plain_text(self):
        self.assertTrue(self.s.search('旅立ち')['hits'])

    def test_mode_limits_the_side_that_is_searched(self):
        self.assertFalse(self.s.search('single gif', mode='ko')['hits'])
        self.assertTrue(self.s.search('single gif', mode='src')['hits'])

    def test_case_is_ignored(self):
        self.assertTrue(self.s.search('VIGOR')['hits'])

    def test_quote_in_the_query_is_not_query_syntax(self):
        # as one token it is a literal that matches nothing; as a sentence it
        # falls through to term search, which is still not FTS syntax
        self.assertEqual(self.s.search('gift"OR"리프')['total'], 0)
        r = self.s.search('gift" OR ko_flat:"리프')
        self.assertEqual(r['match'], 'terms')
        self.assertTrue(all(h['score'] <= len(r['terms']) for h in r['hits']))

    def test_percent_is_a_literal_in_the_short_query_path(self):
        self.assertEqual(self.s.search('%')['total'], 0)

    def test_paging_walks_the_whole_result(self):
        self.s.index(ROWS + [(f'x{i}', f'k{i}', 'p.html', f'r-x{i}--k{i}', 'x',
                              'gift', f'선물 {i}') for i in range(10)])
        first = self.s.search('gift', limit=4)
        self.assertEqual(first['total'], 11)
        self.assertEqual(len(first['hits']), 4)
        second = self.s.search('gift', limit=4, offset=4)
        self.assertNotEqual([h['key'] for h in first['hits']],
                            [h['key'] for h in second['hits']])

    def test_reindex_keeps_the_line_id_so_feedback_survives(self):
        self.s.submit('100000', 'k1', 'typo', '오타 있음', ip='1.2.3.4')
        self.s.index(ROWS)
        self.assertEqual(len(self.s.thread('100000', 'k1')['items']), 1)


class TestScreenRendering(Base):
    """A reader searches the sentence the GAME showed, not the template."""

    SUBS = {'{HERO}': '아루스'}
    TPL = [('#100000', 't1', 'p.html', 'r-100000--t1', '#100000',
            '{HERO}は{I_NAME}をてにいれた！', '{HERO}는 {I_NAME}을 받았다!  \n\n'),
           ('#MENULIST_item_menu', '#259', 'p2.html', 'r-MENULIST_item_menu--259',
            '#MENULIST_item_menu', 'アンチョビ;サンド', '안초뱄;모래')]

    def setUp(self):
        super().setUp()
        self.s.index(self.TPL, self.SUBS)

    def test_a_declared_placeholder_is_searchable_as_its_screen_text(self):
        self.assertEqual([h['key'] for h in self.s.search('아루스는')['hits']],
                         ['t1'])

    def test_the_template_token_is_still_searchable(self):
        self.assertEqual([h['key'] for h in self.s.search('{HERO}')['hits']], ['t1'])

    def test_the_shipped_text_keeps_the_token(self):
        self.assertIn('{HERO}', self.s.search('아루스는')['hits'][0]['ko'])

    def test_a_field_separator_does_not_split_a_word(self):
        self.assertEqual([h['key'] for h in self.s.search('안초뱄모래')['hits']],
                         ['#259'])

    def test_a_sentence_copied_off_the_screen_finds_its_template(self):
        r = self.s.search('아루스는 안초뱄모래을 받았다!')
        self.assertEqual(r['match'], 'terms')
        keys = [h['key'] for h in r['hits']]
        self.assertEqual(keys[0], 't1')
        self.assertIn('#259', keys)

    def test_a_long_distinctive_word_outranks_two_common_ones(self):
        # '안초뱄모래' is what identifies the row; '받았다' is on 181 lines
        self.s.index(self.TPL + [
            (f'#x{i}', f'k{i}', 'p.html', f'r-x{i}--k{i}', 'x',
             'src', f'아루스는 받았다 {i}') for i in range(5)], self.SUBS)
        keys = [h['key'] for h in
                self.s.search('안초뱄모래을 받았다')['hits']]
        self.assertEqual(keys[0], '#259')

    def test_a_particle_does_not_hide_the_word(self):
        # '안초비모래을' is the reader's word; the row holds only '안초비모래'
        r = self.s.search('안초뱄모래을 받았다')
        self.assertIn('#259', [h['key'] for h in r['hits']])

    def test_an_undeclared_placeholder_is_dropped_not_guessed(self):
        # the screen rendering of the template is '아루스는 을 받았다!' - the item
        # name is a runtime value this code has no rendering for
        self.assertEqual([h['key'] for h in self.s.search('아루스는 을 받았다')['hits']],
                         ['t1'])

    def test_a_phrase_that_exists_does_not_fall_back(self):
        self.assertEqual(self.s.search('아루스는 ')['match'], 'phrase')


class TestSubmit(Base):
    def test_a_report_appears_on_that_line_only(self):
        r = self.s.submit('100000', 'k1', 'mistranslation', '이름이 エ리로 남아 있다',
                          suggest='엘리', nick='독자', ip='1.1.1.1')
        self.assertIn('id', r)
        t = self.s.thread('100000', 'k1')
        self.assertEqual(t['items'][0]['suggest'], '엘리')
        self.assertEqual(self.s.thread('100000', 'k2')['items'], [])

    def test_unknown_kind_is_refused(self):
        r = self.s.submit('100000', 'k1', 'whatever', 'x' * 5, ip='1.1.1.1')
        self.assertEqual(r['error'], 'kind')

    def test_empty_body_is_refused(self):
        self.assertEqual(self.s.submit('100000', 'k1', 'typo', ' ',
                                       ip='1.1.1.1')['error'], 'body')

    def test_oversized_body_is_refused(self):
        r = self.s.submit('100000', 'k1', 'typo', 'ㅋ' * (feedback.BODY_MAX + 1),
                          ip='1.1.1.1')
        self.assertEqual(r['error'], 'length')

    def test_unknown_line_is_refused(self):
        self.assertEqual(self.s.submit('nope', 'nope', 'typo', 'x' * 5,
                                       ip='1.1.1.1')['error'], 'line')

    def test_the_same_reader_cannot_double_post_on_one_line(self):
        self.s.submit('100000', 'k1', 'typo', '첫 번째', ip='9.9.9.9')
        r = self.s.submit('100000', 'k1', 'typo', '두 번째', ip='9.9.9.9')
        self.assertEqual(r['error'], 'dup')

    def bulk(self, n):
        """Index n more lines so a budget test is not capped by the dedupe rule."""
        keys = [f'b{i}' for i in range(n)]
        self.s.index(ROWS + [
            ('BULK', k, 'p-BULK.html', f'r-BULK--{k}', 'BULK',
             f'line {k}', f'대사 {k}') for k in keys])
        return keys

    def test_the_minute_budget_stops_a_flood(self):
        burst = feedback.BUDGETS[0][1]
        keys = self.bulk(burst + 1)
        for i in range(burst):
            self.assertNotIn('error', self.s.submit(
                'BULK', keys[i], 'typo', f'의견 {i}', ip='8.8.8.8'))
        r = self.s.submit('BULK', keys[burst], 'typo', '한 건 더', ip='8.8.8.8')
        self.assertEqual(r['error'], 'rate')

    def test_a_reader_can_file_a_hundred_reports_in_one_sitting(self):
        # the reason the budgets exist is scripts, not diligent readers. Someone
        # proofreading a chapter files far more than the old cap of 15/hour.
        keys = self.bulk(100)
        burst = feedback.BUDGETS[0][1]
        for i, k in enumerate(keys):
            if i and i % burst == 0:
                # a human pauses between bursts; age the minute window instead
                # of sleeping through it
                with self.s._write, self.s.conn() as c:
                    c.execute('update posts set ts=ts-61 where ip_hash=?',
                              (self.s.iphash('8.8.8.8'),))
            self.assertNotIn('error', self.s.submit(
                'BULK', k, 'typo', f'의견 {i}', ip='8.8.8.8'), f'stopped at {i}')

    def test_another_reader_is_not_rate_limited_by_the_first(self):
        keys = self.bulk(feedback.BUDGETS[0][1])
        for i, k in enumerate(keys):
            self.s.submit('BULK', k, 'typo', f'의견 {i}', ip='8.8.8.8')
        self.assertNotIn('error', self.s.submit('100000', 'k1', 'typo', '나는 다른 사람',
                                                ip='7.7.7.7'))

    def test_the_raw_ip_is_never_stored(self):
        self.s.submit('100000', 'k1', 'typo', '오타', ip='203.0.113.9')
        blob = json.dumps([dict(r) for r in self.s.conn().execute(
            'select * from feedback')], ensure_ascii=False)
        self.assertNotIn('203.0.113.9', blob)

    def test_readers_never_see_triage_vocabulary(self):
        fid = self.s.submit('100000', 'k1', 'typo', '오타', ip='1.1.1.1')['id']
        self.s.triage([fid], status='rejected', note='내부 메모')
        item = self.s.thread('100000', 'k1')['items'][0]
        self.assertNotIn('status', item)
        self.assertNotIn('내부 메모', json.dumps(item, ensure_ascii=False))
        self.assertFalse(item['taken'])

    def test_hidden_feedback_leaves_the_public_thread(self):
        fid = self.s.submit('100000', 'k1', 'typo', '오타', ip='1.1.1.1')['id']
        self.s.triage([fid], visible=False)
        self.assertEqual(self.s.thread('100000', 'k1')['items'], [])

    def test_retract_needs_the_right_token(self):
        r = self.s.submit('100000', 'k1', 'typo', '오타', ip='1.1.1.1')
        self.assertEqual(self.s.retract(r['id'], 'wrong')['error'], 'token')
        self.assertTrue(self.s.retract(r['id'], r['token'])['ok'])
        self.assertEqual(self.s.thread('100000', 'k1')['items'], [])

    def test_a_vote_toggles_and_counts_once_per_reader(self):
        self.assertEqual(self.s.vote('100000', 'k1', '1.1.1.1')['votes'], 1)
        self.assertEqual(self.s.vote('100000', 'k1', '2.2.2.2')['votes'], 2)
        self.assertEqual(self.s.vote('100000', 'k1', '1.1.1.1')['votes'], 1)

    def test_counts_are_reported_per_section(self):
        self.s.submit('100000', 'k1', 'typo', '오타', ip='1.1.1.1')
        self.s.vote('100000', 'k1', '5.5.5.5')
        c = self.s.section_counts('100000')
        self.assertEqual(c['k1'], {'n': 1, 'v': 1})


class TestOrders(Base):
    def _one(self, ip='1.1.1.1'):
        return self.s.submit('100000', 'k1', 'mistranslation', 'エ리가 그대로다',
                             suggest='엘리', ip=ip)['id']

    def test_a_new_order_is_a_draft_and_carries_the_shipped_text(self):
        p = self.s.make_order([self._one()], title='이름 표기 수정')
        self.assertEqual(p['rows'][0]['ko_current'], ROWS[0][6])
        self.assertEqual(self.s.order(p['id'])['status'], 'draft')

    def test_an_order_does_not_carry_reader_identity(self):
        p = self.s.make_order([self._one()])
        blob = json.dumps(p, ensure_ascii=False)
        self.assertNotIn('ip_hash', blob)
        self.assertNotIn('1.1.1.1', blob)

    def test_making_an_order_takes_the_feedback_off_the_queue(self):
        fid = self._one()
        self.s.make_order([fid])
        self.assertEqual(self.s.queue('open'), [])
        self.assertEqual(self.s.queue('accepted')[0]['id'], fid)

    def test_an_ordered_report_cannot_be_retracted(self):
        r = self.s.submit('100000', 'k1', 'typo', '오타', ip='4.4.4.4')
        self.s.make_order([r['id']])
        self.assertEqual(self.s.retract(r['id'], r['token'])['error'], 'ordered')

    def test_approval_needs_a_name(self):
        p = self.s.make_order([self._one()])
        with self.assertRaises(ValueError):
            self.s.approve_order(p['id'], '  ')

    def test_approval_writes_the_order_to_disk(self):
        p = self.s.make_order([self._one()])
        out = os.path.join(self.dir, 'orders')
        self.s.approve_order(p['id'], '운영자', out)
        self.assertTrue(os.path.exists(os.path.join(out, p['id'] + '.json')))
        md = open(os.path.join(out, p['id'] + '.md'), encoding='utf-8').read()
        self.assertIn('운영자', md)
        self.assertIn('100000/k1', md)

    def test_an_unapproved_order_says_so_on_its_face(self):
        p = self.s.make_order([self._one()])
        self.assertIn('아무것도 수정하지 말 것', feedback.order_md(p))

    def test_double_approval_is_refused(self):
        p = self.s.make_order([self._one()])
        self.s.approve_order(p['id'], '운영자')
        with self.assertRaises(ValueError):
            self.s.approve_order(p['id'], '운영자')

    def test_voiding_returns_the_feedback_to_the_queue(self):
        fid = self._one()
        p = self.s.make_order([fid])
        self.s.void_order(p['id'])
        self.assertEqual(self.s.queue('open')[0]['id'], fid)


class TestApplyGate(Base):
    """`apply_order` is the only door from feedback to shipped text."""

    def _order(self, approve=False):
        fid = self.s.submit('100000', 'k1', 'typo', '오타', ip='1.1.1.1')['id']
        p = self.s.make_order([fid])
        if approve:
            self.s.approve_order(p['id'], '운영자')
        return p

    def test_an_unapproved_order_is_refused(self):
        p = self._order()
        res = feedback.apply_order(self.s, p['id'], {'100000/k1': '새 번역'})
        self.assertFalse(res['ok'])
        self.assertIn('승인된 지시서만', res['error'])

    def test_an_unknown_order_is_refused(self):
        res = feedback.apply_order(self.s, 'ord-nope', {'100000/k1': 'x'})
        self.assertFalse(res['ok'])

    def test_a_key_outside_the_order_is_refused(self):
        p = self._order(approve=True)
        res = feedback.apply_order(self.s, p['id'], {'100000/k2': '몰래 고치기'})
        self.assertFalse(res['ok'])
        self.assertIn('100000/k2', res['error'])

    def test_an_empty_fix_set_is_refused(self):
        p = self._order(approve=True)
        self.assertFalse(feedback.apply_order(self.s, p['id'], {})['ok'])

    def test_a_voided_order_cannot_be_applied(self):
        p = self._order()
        self.s.void_order(p['id'])
        self.assertFalse(feedback.apply_order(
            self.s, p['id'], {'100000/k1': 'x'})['ok'])


class TestHTTP(Base):
    def setUp(self):
        super().setUp()
        self.srv, self.admin = feedback.serve(
            self.s, port=0, admin_port=0, token='sekret',
            orderdir=os.path.join(self.dir, 'orders'))
        self.base = 'http://127.0.0.1:%d' % self.srv.server_address[1]
        self.abase = 'http://127.0.0.1:%d' % self.admin.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.admin.shutdown()

    def _get(self, url, headers=None):
        import urllib.request
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())

    def _post(self, url, body, headers=None):
        import urllib.error
        import urllib.request
        h = {'Content-Type': 'application/json'}
        h.update(headers or {})
        req = urllib.request.Request(url, json.dumps(body).encode(), h)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_public_search_over_http(self):
        from urllib.parse import quote
        code, d = self._get(self.base + '/api/search?q=' + quote('여행 준'))
        self.assertEqual(code, 200)
        self.assertEqual(d['hits'][0]['anchor'], 'r-100000--k1')

    def test_public_post_and_read_back(self):
        code, d = self._post(self.base + '/api/feedback',
                             {'sec': '100000', 'key': 'k1', 'kind': 'typo',
                              'body': '오타가 있습니다'})
        self.assertEqual(code, 200)
        _c, t = self._get(self.base + '/api/line?sec=100000&key=k1')
        self.assertEqual(t['items'][0]['body'], '오타가 있습니다')

    def test_the_honeypot_swallows_a_bot_without_storing_it(self):
        code, d = self._post(self.base + '/api/feedback',
                             {'sec': '100000', 'key': 'k1', 'kind': 'typo',
                              'body': 'buy pills', 'hp': 'http://spam'})
        self.assertEqual(code, 200)
        self.assertEqual(d['id'], 0)
        self.assertEqual(self.s.thread('100000', 'k1')['items'], [])

    def test_the_public_surface_has_no_triage_route(self):
        self.assertEqual(self._post(self.base + '/triage',
                                    {'ids': [1], 'status': 'done'})[0], 404)
        self.assertEqual(self._get_code(self.base + '/queue.json'), 404)

    def _get_code(self, url):
        import urllib.error
        import urllib.request
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_the_admin_surface_needs_the_token(self):
        self.assertEqual(self._get_code(self.abase + '/queue.json'), 403)
        code, d = self._get(self.abase + '/queue.json',
                            {'X-Admin-Token': 'sekret'})
        self.assertEqual(code, 200)
        self.assertIn('counts', d)

    def test_admin_order_flow_over_http(self):
        h = {'X-Admin-Token': 'sekret'}
        self._post(self.base + '/api/feedback',
                   {'sec': '100000', 'key': 'k1', 'kind': 'typo', 'body': '오타요'})
        _c, q = self._get(self.abase + '/queue.json', h)
        fid = q['items'][0]['id']
        _c, p = self._post(self.abase + '/order', {'ids': [fid], 'title': 't'}, h)
        self.assertEqual(p['rows'][0]['key'], 'k1')
        code, ap = self._post(self.abase + '/order/approve',
                              {'id': p['id'], 'by': '운영자'}, h)
        self.assertEqual((code, ap['status']), (200, 'approved'))
        self.assertTrue(os.path.exists(
            os.path.join(self.dir, 'orders', p['id'] + '.md')))


if __name__ == '__main__':
    unittest.main(verbosity=2)
