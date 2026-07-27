"""Build the bilingual Crimson Shroud script book (대본집) as a static site.

Sources of truth:
  work/text_src.json      English + Japanese, straight from the ROM
  work/ko/manifest.json   the sealed Korean text that ships in the patch

Output: build/scriptbook/  (index.html, chapter pages, appendices, script.md)
"""
import html
import json
import os
import re
import shutil
import sys
from collections import OrderedDict, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mtl'))

from hanpatch import tm

from hanpatch import config

def _out(path=None):
    global OUT
    OUT = path or config.p('build', 'scriptbook')
    os.makedirs(OUT, exist_ok=True)
    return OUT


OUT = None
TAG = re.compile(r'<[^>\n]*>')
PAGE = re.compile(r'<page>')
KEYP = re.compile(r'<key>')

CHAPTER_TITLES = {
    0: ('Prologue', '프롤로그'),
    1: ('Chapter 1', '제1장'),
    2: ('Chapter 2', '제2장'),
    3: ('Chapter 3', '제3장'),
    4: ('Chapter 4', '제4장'),
    5: ('Chapter 5', '제5장'),
}


def load():
    src = json.load(open(config.src_path()))
    man = json.load(open(config.out('manifest.json')))
    return src, man['entries'], man['digest']


def clean(s, keep_breaks=True):
    """Readable prose: drop markup, keep paragraph structure."""
    s = s.replace('<br>', '\n').replace('<page>', '\n\n')
    s = re.sub(r'<key>', ' ', s)
    s = TAG.sub('', s)
    s = re.sub(r'[ \t]+', ' ', s)
    s = re.sub(r' *\n *', '\n', s)
    s = re.sub(r'\n{3,}', '\n\n', s)
    if not keep_breaks:
        s = s.replace('\n', ' ')
    return s.strip()


def flow(s):
    """Korean/English reading flow: soft line breaks inside a paragraph join."""
    parts = [p.strip() for p in clean(s).split('\n\n')]
    out = []
    for p in parts:
        out.append(re.sub(r'\n', ' ', p).strip())
    return [p for p in out if p]


# ---------------------------------------------------------------- structure
def dialogue_sections(src, man):
    """-> OrderedDict[section_id] = {title_en, title_ko, rows:[(key, en, ko)]}"""
    rows = {it['key']: it for it in src['dialogue']}
    ko = {k.split('/', 1)[1]: v for k, v in man.items() if k.startswith('dialogue/')}

    def ordered(pred, sortkey):
        ks = [k for k in rows if pred(k)]
        return sorted(ks, key=sortkey)

    sections = OrderedDict()

    # story first: mes_chN[_M]_KKK
    scenes = defaultdict(list)
    for k in rows:
        m = re.match(r'mes_ch(\d+)(?:_(\d+))?_(\d+)$', k)
        if m:
            scenes[(int(m.group(1)), int(m.group(2) or 0))].append(
                (int(m.group(3)), k))
    for (chn, sc) in sorted(scenes):
        ks = [k for _, k in sorted(scenes[(chn, sc)])]
        ten, tko = CHAPTER_TITLES.get(chn, (f'Chapter {chn}', f'제{chn}장'))
        if sc:
            ten, tko = f'{ten} · Scene {sc}', f'{tko} · {sc}절'
        sections[f'ch{chn}_{sc}'] = {'title_en': ten, 'title_ko': tko, 'keys': ks,
                                     'kind': 'story'}

    # field messages: mesNN_FM_KKK, grouped by stage
    stage_en = {}
    stage_ko = {}
    for it in src['region']:
        m = re.match(r'stage_ID(\d+)$', it['key'])
        if m:
            stage_en[int(m.group(1))] = it['en']
            stage_ko[int(m.group(1))] = man.get(f'region/{it["key"]}', it['en'])
    fields = defaultdict(list)
    for k in rows:
        m = re.match(r'mes(\d+)_FM_(\d+)$', k)
        if m:
            fields[int(m.group(1))].append((int(m.group(2)), k))
    for st in sorted(fields):
        ks = [k for _, k in sorted(fields[st])]
        nen = stage_en.get(st, f'Area {st:02d}')
        nko = stage_ko.get(st, f'{st:02d}구역')
        sections[f'fm{st:02d}'] = {
            'title_en': f'Field · {st:02d} {nen}',
            'title_ko': f'필드 · {st:02d} {nko}',
            'keys': ks,
            'kind': 'field',
        }

    # rules narration last: it is how-to-play text, not story
    tut = ordered(lambda k: k.startswith('mes_tutorial_') or '_tuto' in k,
                  lambda k: (0 if k.startswith('mes_tutorial') else 1, k))
    if tut:
        sections['tutorial'] = {
            'title_en': 'Tutorials & rules narration',
            'title_ko': '튜토리얼 · 규칙 해설',
            'keys': tut,
            'kind': 'rules',
        }

    for sid, sec in sections.items():
        sec['rows'] = [(k, rows[k]['en'], ko.get(k, '')) for k in sec['keys']
                       if not tm.is_skip(rows[k]['en'], k) and rows[k]['en'].strip()]
    return OrderedDict((k, v) for k, v in sections.items() if v['rows'])


APPENDICES = [
    ('characters', '등장인물', 'Characters', 'unit', r'unit_info_\d+'),
    ('regions', '지역 · 스테이지', 'Regions & stages', 'region', r'(region|stage)_\w+'),
    ('enemies', '적', 'Enemies', 'common', r'enemy_[A-Za-z]+_00'),
    ('arms', '무기 · 장비', 'Arms & equipment', 'arms', r'arms_\d+'),
    ('arms_help', '장비 설명', 'Equipment descriptions', 'arms_help', r'arms_\d+_help'),
    ('items', '아이템', 'Items', 'item', r'item_\d+'),
    ('item_help', '아이템 설명', 'Item descriptions', 'item', r'item_\d+_help'),
    ('magic', '마법 · 기술', 'Spells & skills', 'magic', r'effect_\d+'),
    ('magic_info', '마법 효과', 'Spell effects', 'magic', r'effect_info_\d+'),
    ('battle', '전투 로그 · 상태', 'Battle log & statuses', 'battle', r'btl_\w+'),
    ('system', '시스템 · 도움말', 'System & help', 'system', r'\w+'),
]


def appendix_rows(src, man, family, pattern):
    rx = re.compile(pattern)
    out = []
    for it in src.get(family, []):
        if not rx.fullmatch(it['key']):
            continue
        if tm.is_skip(it['en'], it['key']) or not it['en'].strip():
            continue
        ko = man.get(f'{family}/{it["key"]}')
        if not ko:
            continue
        out.append((it['key'], it['en'], ko))
    return out


# ---------------------------------------------------------------- rendering
CSS = """
:root{--bg:#efe7d4;--panel:#fbf6ea;--ink:#2f2418;--dim:#7b6a52;--line:#d8c9a8;
--accent:#8c2b23;--mono:'DejaVu Sans Mono',ui-monospace,monospace}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.75 'Noto Serif KR','Nanum Myeongjo',serif}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
header{position:sticky;top:0;z-index:20;background:rgba(239,231,212,.96);
border-bottom:1px solid var(--line);backdrop-filter:blur(6px)}
.hwrap{max-width:1180px;margin:0 auto;padding:10px 20px;display:flex;
gap:14px;align-items:center;flex-wrap:wrap}
.brand{font-weight:700;letter-spacing:.02em}
.brand small{display:block;font-weight:400;font-size:11px;color:var(--dim);letter-spacing:.06em}
.grow{flex:1}
.btn{border:1px solid var(--line);background:var(--panel);color:var(--ink);
padding:5px 11px;border-radius:4px;font:inherit;font-size:13px;cursor:pointer}
.btn.on{background:var(--accent);color:#fff;border-color:var(--accent)}
input[type=search]{border:1px solid var(--line);background:var(--panel);
padding:6px 10px;border-radius:4px;font:inherit;font-size:13px;min-width:210px}
.layout{max-width:1180px;margin:0 auto;padding:0 20px;display:flex;gap:28px;align-items:flex-start}
nav.toc{width:250px;flex:none;position:sticky;top:60px;max-height:calc(100vh - 80px);
overflow:auto;padding:18px 0;font-size:13px;line-height:1.5}
nav.toc h3{margin:14px 0 6px;font-size:11px;letter-spacing:.12em;color:var(--dim);
text-transform:uppercase}
nav.toc a{display:block;padding:3px 8px;border-radius:3px;color:var(--ink)}
nav.toc a:hover{background:var(--panel)}
nav.toc a.cur{background:var(--accent);color:#fff}
main{flex:1;min-width:0;padding:22px 0 90px}
h1{font-size:26px;margin:0 0 4px}
h2{font-size:20px;margin:34px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--line)}
.sub{color:var(--dim);font-size:13px;margin:0 0 22px}
.row{background:var(--panel);border:1px solid var(--line);border-radius:6px;
padding:12px 15px;margin:0 0 11px}
.row .k{font:11px/1 var(--mono);color:var(--dim);letter-spacing:.04em;
display:block;margin-bottom:7px}
.ko p,.en p{margin:0 0 7px}
.ko p:last-child,.en p:last-child{margin-bottom:0}
.en{color:#6c5b45;font-size:14px;border-top:1px dashed var(--line);
margin-top:9px;padding-top:8px;font-family:Georgia,serif}
body.only-ko .en{display:none}
body.only-en .ko{display:none}
body.only-en .en{border-top:0;margin-top:0;padding-top:0;color:var(--ink);font-size:16px}
table{width:100%;border-collapse:collapse;font-size:14px;background:var(--panel);
border:1px solid var(--line);border-radius:6px;overflow:hidden}
th,td{text-align:left;padding:7px 11px;border-bottom:1px solid var(--line);vertical-align:top}
th{background:#e6dcc3;font-size:12px;letter-spacing:.06em;color:var(--dim)}
tr:last-child td{border-bottom:0}
td.key{font:11px/1.4 var(--mono);color:var(--dim);white-space:nowrap}
td.en{color:#6c5b45;font-family:Georgia,serif}
body.only-ko td.en,body.only-ko th.en{display:none}
body.only-en td.ko,body.only-en th.ko{display:none}
.hide{display:none!important}
mark{background:#f4d98b;color:inherit;padding:0 1px}
footer{border-top:1px solid var(--line);margin-top:40px;padding:18px 0;
color:var(--dim);font-size:12px}
.stat{display:flex;gap:18px;flex-wrap:wrap;font-size:13px;color:var(--dim);margin:0 0 24px}
.stat b{color:var(--ink)}
.readbar{display:flex;gap:8px;align-items:center;background:var(--panel);
border:1px solid var(--line);border-radius:6px;padding:8px 12px;margin:0 0 20px;
font-size:13px;position:sticky;top:56px;z-index:10}
.readbar>span:first-child{color:var(--dim);letter-spacing:.04em}
.ord{display:inline-block;min-width:44px;font:11px/1 var(--mono);color:#fff;
background:var(--accent);border-radius:3px;padding:4px 6px;text-align:center;
vertical-align:2px;margin-right:6px}
.secnav{display:flex;justify-content:space-between;gap:12px;margin:6px 0 26px;
font-size:12px;color:var(--dim)}
.secnav a{background:var(--panel);border:1px solid var(--line);border-radius:4px;
padding:5px 10px;color:var(--ink)}
nav.toc a i{display:inline-block;min-width:26px;font:10px/1 var(--mono);
color:var(--dim);font-style:normal}
nav.toc a.cur i{color:#fff}
body.scope-story .sec:not([data-kind=story]),
body.scope-storyfield .sec[data-kind=rules]{display:none}
@media(max-width:900px){nav.toc{display:none}.layout{padding:0 14px}
.readbar{position:static;flex-wrap:wrap}}
@media print{header,nav.toc,.noprint{display:none}.layout{display:block;max-width:none}
body{background:#fff;font-size:11pt}.row{break-inside:avoid;border:0;background:none;
border-bottom:1px solid #ccc;border-radius:0}}
"""

JS = r"""
(function(){
 var b=document.body;
 function mode(m){b.className=m==='both'?'':'only-'+m;
   document.querySelectorAll('[data-mode]').forEach(function(x){
     x.classList.toggle('on',x.dataset.mode===m)});
   try{localStorage.setItem('csb-mode',m)}catch(e){}}
 document.querySelectorAll('[data-mode]').forEach(function(x){
   x.addEventListener('click',function(){mode(x.dataset.mode)})});
 var saved='both';try{saved=localStorage.getItem('csb-mode')||'both'}catch(e){}
 mode(saved);
 var q=document.getElementById('q');
 if(q){var t;q.addEventListener('input',function(){clearTimeout(t);t=setTimeout(run,140)});}
 function run(){
   var s=q.value.trim().toLowerCase();
   var units=document.querySelectorAll('.row,tbody tr');
   var n=0;
   units.forEach(function(u){
     if(!s){u.classList.remove('hide');unmark(u);n++;return}
     var hit=u.textContent.toLowerCase().indexOf(s)>=0;
     u.classList.toggle('hide',!hit);
     unmark(u); if(hit){mark(u,s);n++}
   });
   document.querySelectorAll('h2').forEach(function(h){
     var el=h.nextElementSibling,vis=false;
     while(el&&el.tagName!=='H2'){
       if(el.classList&&el.classList.contains('row')&&!el.classList.contains('hide'))vis=true;
       if(el.querySelector&&el.querySelector('tbody tr:not(.hide)'))vis=true;
       el=el.nextElementSibling}
     h.classList.toggle('hide',!!s&&!vis);
     var w=h.nextElementSibling;
     if(w&&w.tagName==='TABLE')w.classList.toggle('hide',!!s&&!vis);
   });
   var c=document.getElementById('cnt');
   if(c)c.textContent=s?(n+' hit'):'';
 }
 function unmark(u){u.querySelectorAll('mark').forEach(function(m){
   m.replaceWith(document.createTextNode(m.textContent))});u.normalize()}
 function mark(u,s){
   var w=document.createTreeWalker(u,NodeFilter.SHOW_TEXT),ns=[],x;
   while(x=w.nextNode())ns.push(x);
   ns.forEach(function(node){
     var i=node.nodeValue.toLowerCase().indexOf(s);
     if(i<0)return;
     var m=document.createElement('mark');
     var mid=node.splitText(i);mid.splitText(s.length);
     m.appendChild(document.createTextNode(mid.nodeValue));
     mid.parentNode.replaceChild(m,mid);
   });
 }
 function scope(v){
   b.classList.remove('scope-story','scope-storyfield');
   if(v==='story')b.classList.add('scope-story');
   if(v==='story+field')b.classList.add('scope-storyfield');
   document.querySelectorAll('[data-scope]').forEach(function(x){
     x.classList.toggle('on',x.dataset.scope===v)});
   try{localStorage.setItem('csb-scope',v)}catch(e){}}
 document.querySelectorAll('[data-scope]').forEach(function(x){
   x.addEventListener('click',function(e){e.preventDefault();scope(x.dataset.scope)})});
 if(document.querySelector('[data-scope]')){
   var sv='story+field';try{sv=localStorage.getItem('csb-scope')||'story+field'}catch(e){}
   scope(sv);}
 var secs=[].slice.call(document.querySelectorAll('section.sec'));
 var resume=document.getElementById('resume');
 if(resume){
   var last=null;try{last=localStorage.getItem('csb-last')}catch(e){}
   if(last&&document.getElementById(last)){
     resume.href='#'+last;
     var h=document.querySelector('#'+last);
     resume.textContent='이어서 읽기 · '+h.textContent.replace(/\s+/g,' ').trim().slice(0,22);
   }
   if(secs.length){
     window.addEventListener('scroll',function(){
       var y=window.scrollY+120,cur=null;
       secs.forEach(function(s){if(s.offsetParent!==null&&s.offsetTop<=y)cur=s.dataset.sid});
       if(cur){try{localStorage.setItem('csb-last',cur)}catch(e){}}
     },{passive:true});
   }
 }
 var links=[].slice.call(document.querySelectorAll('nav.toc a[href*="#"]'));
 var hs=links.map(function(a){
   var i=a.href.indexOf('#');return i<0?null:document.getElementById(a.href.slice(i+1))});
 window.addEventListener('scroll',function(){
   var y=window.scrollY+90,cur=0;
   hs.forEach(function(h,i){if(h&&h.offsetTop<=y)cur=i});
   links.forEach(function(a,i){a.classList.toggle('cur',i===cur)});
 },{passive:true});
})();
"""


def esc(s):
    return html.escape(s, quote=False)


def paras(s, cls):
    return f'<div class="{cls}">' + ''.join(
        f'<p>{esc(p)}</p>' for p in flow(s)) + '</div>'


def page(title, toc, body, subtitle='', extra_head=''):
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<meta name="robots" content="noindex,nofollow">
<link rel="stylesheet" href="style.css">{extra_head}
</head><body>
<header><div class="hwrap">
<div class="brand">크림슨 슈라우드 대본집<small>CRIMSON SHROUD · KOREAN SCRIPT BOOK</small></div>
<div class="grow"></div>
<input type="search" id="q" placeholder="원문·번역 검색">
<span id="cnt" style="font-size:12px;color:var(--dim)"></span>
<button class="btn" data-mode="both">대역</button>
<button class="btn" data-mode="ko">한국어</button>
<button class="btn" data-mode="en">English</button>
</div></header>
<div class="layout">
<nav class="toc">{toc}</nav>
<main><h1>{esc(title)}</h1>{f'<p class="sub">{subtitle}</p>' if subtitle else ''}
{body}
<footer>ROM 원문 + 한글패치 봉인 매니페스트에서 자동 생성. 게임 데이터 자체는 포함하지 않습니다.</footer>
</main></div>
<script src="app.js"></script></body></html>"""


def build():
    if OUT is None:
        _out()
    src, man, digest = load()
    os.makedirs(OUT, exist_ok=True)
    sections = dialogue_sections(src, man)

    nav_story, nav_field, nav_rules = [], [], []
    sn = 0
    for sid, sec in sections.items():
        kind = sec.get('kind', 'story')
        if kind == 'story':
            sn += 1
            nav_story.append(f'<a href="story.html#{sid}">'
                             f'<i>{sn}</i>{esc(sec["title_ko"])}</a>')
        elif kind == 'field':
            nav_field.append(f'<a href="story.html#{sid}">{esc(sec["title_ko"])}</a>')
        else:
            nav_rules.append(f'<a href="story.html#{sid}">{esc(sec["title_ko"])}</a>')
    nav_app = [f'<a href="appendix.html#{a[0]}">{esc(a[1])}</a>' for a in APPENDICES]

    def toc(active=''):
        return (f'<h3>스토리 순서 ({sn})</h3>{"".join(nav_story)}'
                f'<h3>필드 메시지</h3>{"".join(nav_field)}'
                f'<h3>규칙 해설</h3>{"".join(nav_rules)}'
                f'<h3>부록</h3>{"".join(nav_app)}'
                f'<h3>기타</h3><a href="index.html">표지 · 통계</a>'
                f'<a href="script.md">Markdown 원본</a>')

    # ---- story page
    ids = list(sections)
    story_ids = [i for i in ids if sections[i].get('kind') == 'story']
    body = []
    total_rows = 0
    for n, (sid, sec) in enumerate(sections.items()):
        kind = sec.get('kind', 'story')
        num = ''
        if kind == 'story':
            num = f'<span class="ord">{story_ids.index(sid) + 1}/{len(story_ids)}</span> '
        prev_id = ids[n - 1] if n else None
        next_id = ids[n + 1] if n + 1 < len(ids) else None
        nav = '<div class="secnav noprint">'
        nav += (f'<a href="#{prev_id}">◀ {esc(sections[prev_id]["title_ko"])}</a>'
                if prev_id else '<span></span>')
        nav += (f'<a href="#{next_id}">{esc(sections[next_id]["title_ko"])} ▶</a>'
                if next_id else '<span></span>')
        nav += '</div>'
        body.append(f'<section class="sec" data-kind="{kind}" data-sid="{sid}">')
        body.append(f'<h2 id="{sid}">{num}{esc(sec["title_ko"])} '
                    f'<span style="font-size:13px;color:var(--dim)">'
                    f'{esc(sec["title_en"])}</span></h2>')
        for key, en, ko in sec['rows']:
            total_rows += 1
            body.append(f'<div class="row"><span class="k">{esc(key)}</span>'
                        f'{paras(ko, "ko")}{paras(en, "en")}</div>')
        body.append(nav)
        body.append('</section>')
    lead = (f'<div class="readbar noprint">'
            f'<span>읽기 범위</span>'
            f'<button class="btn" data-scope="story">본편만</button>'
            f'<button class="btn" data-scope="story+field">본편+필드</button>'
            f'<button class="btn" data-scope="all">전체</button>'
            f'<span class="grow"></span>'
            f'<a class="btn" id="resume" href="#{story_ids[0]}">이어서 읽기</a>'
            f'</div>')
    open(f'{OUT}/story.html', 'w').write(page(
        '본편 대본', toc(), lead + ''.join(body),
        f'스토리 {len(story_ids)}장면 · 전체 {len(sections)}섹션 · '
        f'{total_rows}개 대사 블록 · 위에서 아래로 읽으면 스토리 순서입니다'))

    # ---- appendix page
    ab = []
    app_counts = {}
    for aid, ako, aen, family, pattern in APPENDICES:
        rows = appendix_rows(src, man, family, pattern)
        app_counts[ako] = len(rows)
        if not rows:
            continue
        ab.append(f'<h2 id="{aid}">{esc(ako)} '
                  f'<span style="font-size:13px;color:var(--dim)">{esc(aen)}</span></h2>')
        ab.append('<table><thead><tr><th>KEY</th><th class="ko">한국어</th>'
                  '<th class="en">English</th></tr></thead><tbody>')
        for key, en, ko in rows:
            ab.append(f'<tr><td class="key">{esc(key)}</td>'
                      f'<td class="ko">{esc(clean(ko, False))}</td>'
                      f'<td class="en">{esc(clean(en, False))}</td></tr>')
        ab.append('</tbody></table>')
    open(f'{OUT}/appendix.html', 'w').write(page(
        '부록 · 설정집', toc(), ''.join(ab),
        ' · '.join(f'{k} {v}' for k, v in app_counts.items() if v)))

    # ---- index
    n_story = sum(len(s['rows']) for k, s in sections.items() if not k.startswith('fm'))
    n_field = sum(len(s['rows']) for k, s in sections.items() if k.startswith('fm'))
    n_app = sum(app_counts.values())
    idx = f"""
<div class="stat">
<span><b>{n_story}</b> 본편 대사 블록</span>
<span><b>{n_field}</b> 필드 메시지</span>
<span><b>{n_app}</b> 부록 항목</span>
<span><b>{len(man)}</b> 전체 번역 문자열</span>
<span>매니페스트 <b>{esc(digest[:16])}</b></span>
</div>
<div class="row"><div class="ko">
<p><b>크림슨 슈라우드</b>(Crimson Shroud, 닌텐도 3DS eShop, 2012)의 전체 텍스트를
영문 원문과 한국어 번역으로 나란히 정리한 대본집입니다.</p>
<p>본편 대본은 장·절 순서대로, 필드 메시지는 스테이지 순서대로 배열했습니다.
부록에는 등장인물·지역·적·무기·아이템·마법·전투 로그·시스템 도움말 전체가 들어 있습니다.</p>
<p>모든 문자열은 ROM에서 직접 추출한 원문과, 한글패치가 실제로 탑재하는
<span style="font-family:var(--mono);font-size:13px">manifest.json</span>의 봉인된
번역문에서 자동 생성됩니다. 화면에 나오는 문장과 이 문서의 문장은 같습니다.</p>
<p>상단 버튼으로 <b>대역 / 한국어 / English</b> 표시를 바꿀 수 있고, 검색창은 원문과
번역문을 동시에 찾습니다. 인쇄하면 대본 형식으로 정리됩니다.</p>
</div></div>
<h2>스토리 순으로 읽는 법</h2>
<div class="row"><div class="ko">
<p><a href="story.html#ch0_0"><b>본편 대본 처음부터 읽기 →</b></a></p>
<p><b>story.html</b>은 위에서 아래로 그대로 읽으면 스토리 순서입니다. 배열은
프롤로그 → 제1장 1절 … → 제5장 2절 → 스테이지별 필드 메시지 → 규칙 해설 순입니다.
각 절 제목 앞의 <span class="ord">n/39</span> 표시가 스토리 진행 번호입니다.</p>
<p>페이지 상단 <b>읽기 범위</b>에서 <b>본편만</b>을 고르면 필드 메시지와 규칙 해설이
숨겨져 39개 절만 순서대로 이어집니다. 절 끝의 <b>◀ ▶</b> 링크로 앞뒤 절로 이동할 수
있고, 읽던 위치는 자동 저장되어 <b>이어서 읽기</b> 버튼으로 돌아갑니다.</p>
<p>왼쪽 목차의 <b>스토리 순서</b> 항목은 번호가 붙어 있어 어디까지 읽었는지 바로
확인됩니다. 필드 메시지는 실제 플레이에서는 본편 사이사이에 나오지만, 특정 지역을
방문할 때만 나오는 텍스트라 스테이지별로 따로 모아 두었습니다.</p>
</div></div>
<h2>구성</h2>
<table><thead><tr><th>구분</th><th class="ko">내용</th><th>분량</th></tr></thead><tbody>
<tr><td class="key">story.html</td><td class="ko">프롤로그~제5장 본편(스토리 순) → 스테이지별 필드 메시지 → 규칙 해설</td><td>{n_story + n_field}</td></tr>
<tr><td class="key">appendix.html</td><td class="ko">등장인물, 지역, 적, 무기·장비, 아이템, 마법·기술, 전투 로그, 시스템 도움말</td><td>{n_app}</td></tr>
<tr><td class="key">script.md</td><td class="ko">전체 대본 Markdown 원본 (보관·인용용)</td><td>1</td></tr>
</tbody></table>
<h2>번역 방침</h2>
<div class="row"><div class="ko">
<p>산문(내레이션·대사)의 기준 원문은 <b>영어판</b>입니다. 영어판이 일본어 원판과
내용이나 화자가 다를 때는 영어판을 따릅니다.</p>
<p>무기·방어구·아이템·마법·기술의 <b>고유명은 일본어 원판</b> 표기를 음역합니다.
(예: Vigor Leaf / キュアリーフ → 큐어 리프)</p>
<p>내레이션은 평서형(~다), 대사는 구어체, 플레이어에게 직접 지시하는 시스템 문장만
존댓말로 통일했습니다.</p>
</div></div>
"""
    open(f'{OUT}/index.html', 'w').write(page(
        '크림슨 슈라우드 대본집', toc(), idx,
        '영문 원문 · 한국어 번역 대역본'))

    # ---- markdown
    md = ['# 크림슨 슈라우드 대본집',
          '',
          f'- 매니페스트 digest: `{digest}`',
          f'- 본편 대사 블록 {n_story} · 필드 메시지 {n_field} · 부록 항목 {n_app}',
          '- 산문 기준 원문은 영어판, 고유명은 일본어 원판 표기를 따릅니다.',
          '']
    for sid, sec in sections.items():
        md.append(f'## {sec["title_ko"]} — {sec["title_en"]}')
        md.append('')
        for key, en, ko in sec['rows']:
            md.append(f'### `{key}`')
            md.append('')
            for p in flow(ko):
                md.append(p)
            md.append('')
            for p in flow(en):
                md.append(f'> {p}')
            md.append('')
    md.append('## 부록')
    md.append('')
    for aid, ako, aen, family, pattern in APPENDICES:
        rows = appendix_rows(src, man, family, pattern)
        if not rows:
            continue
        md.append(f'### {ako} — {aen}')
        md.append('')
        md.append('| KEY | 한국어 | English |')
        md.append('| --- | --- | --- |')
        for key, en, ko in rows:
            k1 = clean(ko, False).replace('|', '\\|')
            e1 = clean(en, False).replace('|', '\\|')
            md.append(f'| `{key}` | {k1} | {e1} |')
        md.append('')
    open(f'{OUT}/script.md', 'w').write('\n'.join(md))

    open(f'{OUT}/style.css', 'w').write(CSS)
    open(f'{OUT}/app.js', 'w').write(JS)
    open(f'{OUT}/robots.txt', 'w').write('User-agent: *\nDisallow: /\n')
    return {'sections': len(sections), 'story': n_story, 'field': n_field,
            'appendix': n_app, 'digest': digest}


def main(out=None):
    _out(out)
    info = build()
    print('scriptbook ->', OUT)
    for k, v in info.items():
        print(f'  {k:9} {v}')
    for f in sorted(os.listdir(OUT)):
        print(f'  {os.path.getsize(os.path.join(OUT, f)):>9} {f}')
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
