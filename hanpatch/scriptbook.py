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
    src = config.load_object(config.src_path(), 'the extracted source')
    man = config.load_object(config.out('manifest.json'), 'the sealed manifest')
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
def family_sections(src, man):
    """Sections straight from the title's own families, in source order.

    The scene grammar below (chapter/field/tutorial keys, `region` stage names) is the
    reference title's structure, not a portable one. A title that does not carry it still
    has a script, and the container already groups it: one section per family, every
    shippable row in the order the container stores it. Without this the renderer raised
    `KeyError: 'dialogue'` on any other title, so the book simply did not exist for them.
    """
    sections = OrderedDict()
    for fam in src:
        rows = [(it['key'], it['en'], man[f'{fam}/{it["key"]}'])
                for it in src[fam]
                if f'{fam}/{it["key"]}' in man
                and it['en'].strip() and not tm.is_skip(it['en'], it['key'])]
        if rows:
            sections[fam] = {'title_en': fam, 'title_ko': fam,
                             'keys': [r[0] for r in rows], 'rows': rows,
                             'kind': 'story'}
    return sections


def dialogue_sections(src, man):
    """-> OrderedDict[section_id] = {title_en, title_ko, rows:[(key, en, ko)]}"""
    if 'dialogue' not in src:
        return family_sections(src, man)
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
.btn.donate{background:var(--accent);color:#fff;border-color:var(--accent);
text-decoration:none;display:inline-block;line-height:1.5}
.btn.donate:hover{text-decoration:none;opacity:.9}
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
/* reader feedback + global search (inert without fb.js) */
.fbbar{display:flex;gap:6px;align-items:center;margin-top:9px;
border-top:1px dashed var(--line);padding-top:7px}
.fbb,.fbv{border:1px solid var(--line);background:#f5eeda;color:var(--dim);
font:12px/1.4 inherit;padding:3px 9px;border-radius:99px;cursor:pointer}
.fbb:hover,.fbv:hover{border-color:var(--accent);color:var(--accent)}
.fbb.has{color:var(--accent);border-color:var(--accent);background:#f8e6cf}
.fbv.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.fbpanel{margin-top:10px;border-top:1px solid var(--line);padding-top:10px}
.fbnote{background:#f5eeda;border:1px solid var(--line);border-radius:5px;
padding:8px 10px;margin:0 0 8px;font-size:13px}
.fbnote .who{font-size:11px;color:var(--dim);margin-bottom:3px}
.fbnote .sug{border-left:3px solid var(--accent);padding-left:7px;margin-top:5px;
color:#5c4326}
.fbnote .done{color:#2f6b3a;font-size:11px}
.fbform{display:flex;flex-direction:column;gap:6px}
.fbform select,.fbform input,.fbform textarea{border:1px solid var(--line);
background:#fff;color:var(--ink);font:inherit;font-size:13px;padding:6px 8px;
border-radius:4px;width:100%}
.fbform textarea{min-height:64px;resize:vertical}
.fbrow{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.fbrow .grow{flex:1}
.fbhp{position:absolute;left:-9999px;width:1px;height:1px}
.fbmsg{font-size:12px;color:var(--accent)}
.fbsend{border:1px solid var(--accent);background:var(--accent);color:#fff;
font:inherit;font-size:13px;padding:6px 14px;border-radius:4px;cursor:pointer}
.fbmini{font-size:11px;color:var(--dim)}
#fbsearch{background:var(--panel);border:1px solid var(--line);border-radius:6px;
padding:10px 13px;margin:0 0 16px}
#fbsearch h4{margin:0 0 8px;font-size:13px;font-weight:700}
#fbsearch .hit{border-top:1px solid var(--line);padding:8px 0}
#fbsearch .hit:first-of-type{border-top:0}
#fbsearch .hit .sec{font-size:11px;color:var(--dim)}
#fbsearch .hit .ko{font-size:14px}
#fbsearch .hit .en{font-size:12px;border:0;margin:2px 0 0;padding:0}
#fbsearch .more{margin-top:8px}
.row.flash{outline:2px solid var(--accent);outline-offset:3px}
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
 // With the search API present, `fb.js` owns this box and searches the whole
 // book. Filtering the current page as well would hide every row behind the
 // result list for any query whose line lives on another page - which, at 371
 // pages, is nearly every query.
 if(q&&!window.HPFB_API){var t;
   q.addEventListener('input',function(){clearTimeout(t);t=setTimeout(run,140)});}
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


def feedback_intro(total):
    return """
<h2>번역이 이상한 대사를 신고하는 방법</h2>
<div class="row"><div class="ko">
<p>대사 블록마다 아래에 <b>의견</b> · <b>공감</b> 버튼이 있습니다. <b>의견</b>을 누르면
그 대사에 달린 다른 독자의 지적이 보이고, 어떤 부분이 어떻게 이상한지 적어 보낼 수
있습니다. 문제 종류(오역 · 어색함 · 오타 · 이름 표기 · 줄바꿈 깨짐 · 말투)를 고르고,
고칠 문장을 직접 제안할 수도 있습니다. 같은 지적에 <b>공감</b>을 누르면 우선순위가
올라갑니다.</p>
<p>이름은 적지 않아도 됩니다. 보낸 의견은 수정 작업에 묶이기 전까지 <b>내 의견 지우기</b>로 지울 수
있습니다.</p>
<p>검색창은 <b>전체 대본 {total}개 대사</b>를 한 번에 찾습니다. 단어 일부만 넣어도 되고,
원문·번역 어느 쪽이든 걸립니다. 결과를 누르면 그 대사가 있는 쪽으로 바로 이동합니다.</p>
<p>모인 의견은 사람이 직접 검토해서 수정 작업지시서로 묶습니다. 지적이 바로 패치에
반영되지는 않습니다.</p>
</div></div>
""".format(total=total)


def donate_intro():
    return """
<h2>후원</h2>
<div class="row"><div class="ko">
<p>이 대본집과 한글패치는 혼자 만들고 혼자 고칩니다. 보내주신 의견을 하나하나
확인해서 반영하는 일도 같은 사람이 합니다. 도움이 되었다면
<a href="{url}"><b>커피 한잔 후원 &rarr;</b></a>으로 응원해 주세요.</p>
<p>후원은 전부 자율이고, 후원하지 않아도 대본집과 패치는 그대로 전부 공개됩니다.
의견을 보내는 것도 후원과 무관하게 언제나 환영합니다.</p>
</div></div>
""".format(url=esc_attr(donate_url()))


# Reader-facing feedback and whole-book search. Loaded only when an API base is
# configured, and every DOM insert goes through textContent: the panel prints
# text other readers wrote, so it must never be able to print markup.
FB_JS = r"""
(function(){
 if(!window.HPFB_API){return}
 var API=HPFB_API, KINDS=HPFB_KINDS;
 function el(t,c,txt){var e=document.createElement(t);if(c)e.className=c;
  if(txt!=null)e.textContent=txt;return e}
 function get(u){return fetch(API+u,{credentials:'omit'}).then(function(r){return r.json()})}
 function post(u,b){return fetch(API+u,{method:'POST',credentials:'omit',
  headers:{'Content-Type':'application/json'},body:JSON.stringify(b)})
  .then(function(r){return r.json()})}
 function ls(k,v){try{if(v===undefined)return localStorage.getItem(k);
  localStorage.setItem(k,v)}catch(e){return null}}

 // ---------------------------------------------------------------- rows
 var rows=[].slice.call(document.querySelectorAll('.row[data-key]'));
 var byKey={};
 rows.forEach(function(r){byKey[r.dataset.fam+'/'+r.dataset.key]=r;bar(r)});

 function bar(r){
  var b=el('div','fbbar noprint');
  var c=el('button','fbb');c.appendChild(el('span',null,'의견 '));
  var cn=el('b',null,'0');c.appendChild(cn);
  var v=el('button','fbv');v.appendChild(el('span',null,'공감 '));
  var vn=el('b',null,'0');v.appendChild(vn);
  if(ls('fbv:'+r.id)==='1')v.classList.add('on');
  b.appendChild(c);b.appendChild(v);
  b.appendChild(el('span','fbmini','번역이 이상하면 알려 주세요'));
  r.appendChild(b);
  r._cn=cn;r._vn=vn;
  c.addEventListener('click',function(){toggle(r)});
  v.addEventListener('click',function(){
   post('/vote',{sec:r.dataset.fam,key:r.dataset.key}).then(function(d){
    if(d.error)return;vn.textContent=d.votes;
    var on=v.classList.toggle('on');ls('fbv:'+r.id,on?'1':'0')})});
 }

 function counts(){
  var fams={};rows.forEach(function(r){fams[r.dataset.fam]=1});
  Object.keys(fams).slice(0,12).forEach(function(f){
   get('/section?sec='+encodeURIComponent(f)).then(function(d){
    Object.keys(d.counts||{}).forEach(function(k){
     var r=byKey[f+'/'+k];if(!r)return;
     var n=d.counts[k].n||0;r._cn.textContent=n;r._vn.textContent=d.counts[k].v||0;
     if(n)r.querySelector('.fbb').classList.add('has')})}).catch(function(){})})
 }
 counts();

 function toggle(r){
  if(r._panel){r._panel.remove();r._panel=null;return}
  var p=el('div','fbpanel');r._panel=p;r.appendChild(p);
  p.appendChild(el('div','fbmini','불러오는 중…'));
  get('/line?sec='+encodeURIComponent(r.dataset.fam)+'&key='+
      encodeURIComponent(r.dataset.key)).then(function(d){
   p.innerHTML='';
   (d.items||[]).forEach(function(it){p.appendChild(note(it))});
   if(!(d.items||[]).length)
    p.appendChild(el('div','fbmini','아직 의견이 없습니다. 이 대사의 번역에서 어떤 부분이 이상한지 적어 주세요.'));
   p.appendChild(form(r,p));
  }).catch(function(){p.innerHTML='';
   p.appendChild(el('div','fbmsg','불러오지 못했습니다.'))});
 }

 function note(it){
  var n=el('div','fbnote');
  var who=el('div','who',(it.nick||'이름 없음')+' · '+
   (KINDS[it.kind]||it.kind)+' · '+(it.created||'').replace('T',' ').replace('Z',''));
  n.appendChild(who);
  n.appendChild(el('div',null,it.body));
  if(it.suggest)n.appendChild(el('div','sug','이렇게 바꾸면 어떨까요: '+it.suggest));
  if(it.fixed)n.appendChild(el('div','done','✓ 반영됨'));
  else if(it.taken)n.appendChild(el('div','done','✓ 확인함 · 수정 대기'));
  var tok=ls('fbt:'+it.id);
  if(tok&&!it.fixed){
   var del=el('button','fbb','내 의견 지우기');
   del.addEventListener('click',function(){
    post('/retract',{id:it.id,token:tok}).then(function(d){
     if(d.error)return alert(d.message||d.error);n.remove()})});
   var w=el('div','fbrow');w.appendChild(del);n.appendChild(w)}
  return n;
 }

 function form(r,p){
  var f=el('div','fbform');
  var kind=el('select');
  var o0=el('option',null,'어떤 종류의 문제인가요?');o0.value='';kind.appendChild(o0);
  Object.keys(KINDS).forEach(function(k){
   var o=el('option',null,KINDS[k]);o.value=k;kind.appendChild(o)});
  var body=el('textarea');
  body.placeholder='이 대사의 번역에서 어떤 부분이 이상한지 구체적으로 적어 주세요.';
  body.maxLength=1000;
  var sug=el('input');sug.placeholder='(선택) 이렇게 바꾸면 어떨까요';sug.maxLength=600;
  var nick=el('input');nick.placeholder='(선택) 이름';nick.maxLength=24;
  nick.value=ls('fbnick')||'';
  var hp=el('input','fbhp');hp.tabIndex=-1;hp.setAttribute('aria-hidden','true');
  var send=el('button','fbsend','보내기');
  var msg=el('div','fbmsg');
  var row=el('div','fbrow');row.appendChild(nick);
  var g=el('span','grow');row.appendChild(g);row.appendChild(send);
  f.appendChild(kind);f.appendChild(body);f.appendChild(sug);f.appendChild(hp);
  f.appendChild(row);f.appendChild(msg);
  send.addEventListener('click',function(){
   msg.textContent='';
   if(!kind.value){msg.textContent='문제 종류를 골라 주세요.';return}
   if(body.value.trim().length<2){msg.textContent='내용을 적어 주세요.';return}
   send.disabled=true;
   post('/feedback',{sec:r.dataset.fam,key:r.dataset.key,kind:kind.value,
    body:body.value,suggest:sug.value,nick:nick.value,hp:hp.value})
   .then(function(d){
    send.disabled=false;
    if(d.error){msg.textContent=d.message||d.error;return}
    if(d.token)ls('fbt:'+d.id,d.token);
    if(nick.value)ls('fbnick',nick.value);
    r._panel.remove();r._panel=null;
    var n=+r._cn.textContent||0;r._cn.textContent=n+1;
    r.querySelector('.fbb').classList.add('has');
    toggle(r);
   }).catch(function(){send.disabled=false;
    msg.textContent='보내지 못했습니다. 잠시 뒤에 다시 시도해 주세요.'});
  });
  return f;
 }

 // ------------------------------------------------------- whole-book search
 var q=document.getElementById('q');
 var main=document.querySelector('main');
 var box=null,t=null,state={q:'',off:0};
 function panel(){
  if(box)return box;
  box=el('div','noprint');box.id='fbsearch';
  main.insertBefore(box,main.firstChild.nextSibling);
  return box;
 }
 function render(d,append){
  var b=panel();
  if(!append){b.innerHTML='';
   b.appendChild(el('h4',null,'전체 대본 검색 · “'+d.q+'” '+d.total+'건'));
   if(d.match==='terms')
    b.appendChild(el('div','fbmini','문장이 그대로 있는 줄은 없어서, 단어를 많이 '
     +'포함한 줄부터 보여 드립니다. 사람 이름이나 아이템 이름처럼 게임이 실행 중에 '
     +'끼워 넣는 자리는 대본에 {HERO} 같은 태그로 들어 있어서, 화면에서 본 문장과 '
     +'글자까지 똑같지는 않습니다. 검색한 단어: '+(d.terms||[]).join(', ')));}
  else{var m=b.querySelector('.more');if(m)m.remove()}
  d.hits.forEach(function(h){
   var w=el('div','hit');
   w.appendChild(el('div','sec',h.section+' · '+h.fam+'/'+h.key+
    (h.score?' · 일치한 단어 '+h.matched.join(' '):'')+
    (h.n?' · 의견 '+h.n:'')));
   var a=el('a');a.href=h.page.indexOf('#')>=0?h.page:h.page+'#'+h.anchor;
   a.appendChild(el('div','ko',h.ko.replace(/\s+/g,' ').slice(0,180)));
   w.appendChild(a);
   w.appendChild(el('div','en',h.src.replace(/\s+/g,' ').slice(0,180)));
   b.appendChild(w)});
  if(d.offset+d.hits.length<d.total){
   var more=el('div','more');
   var mb=el('button','btn','다음 '+Math.min(40,d.total-d.offset-d.hits.length)+'건 더 보기');
   mb.addEventListener('click',function(){run(state.q,d.offset+d.hits.length,true)});
   more.appendChild(mb);b.appendChild(more)}
  if(!d.total&&!append)
   b.appendChild(el('div','fbmini','검색 결과가 없습니다.'));
 }
 function run(s,off,append){
  state.q=s;
  get('/search?q='+encodeURIComponent(s)+'&offset='+(off||0)+'&limit=40')
   .then(function(d){render(d,append)}).catch(function(){});
 }
 if(q){
  q.addEventListener('input',function(){
   clearTimeout(t);
   var s=q.value.trim();
   if(!s){if(box){box.remove();box=null}return}
   t=setTimeout(function(){run(s,0,false)},260);
  });
  var pre=new URLSearchParams(location.search).get('q');
  if(pre){q.value=pre;run(pre,0,false)}
 }

 // deep link from a search hit: make the landing row obvious
 function flash(){
  var id=location.hash.slice(1);if(!id)return;
  var r=document.getElementById(id);if(!r||!r.classList)return;
  r.classList.add('flash');
  setTimeout(function(){r.classList.remove('flash')},2600);
 }
 window.addEventListener('hashchange',flash);flash();
})();
"""


def fb_api():
    """Base URL of the feedback/search API, or '' for a plain static book.

    Deployment fact, not a title fact, so it lives in `hanpatch.json` (or the
    env) rather than in the profile the gates validate.
    """
    return (config.cfg().get('feedback_api')
            or os.environ.get('HANPATCH_FEEDBACK_API') or '').rstrip('/')


def donate_url():
    """Where the header's support link points, or '' for no link at all.

    A deployment fact like `feedback_api`: the book is generated for whoever hosts
    it, and a donation target hard-coded here would follow the book to hosts that
    never agreed to collect money. Empty means the link is not rendered.
    """
    return (config.cfg().get('donate_url')
            or os.environ.get('HANPATCH_DONATE_URL') or '').strip()


def esc(s):
    return html.escape(s, quote=False)


def ruby(s):
    """Escaped HTML with the source's reading annotations drawn as real furigana.

    The profile declares those annotations `source_only`: they exist to gloss the
    source script and are expected to vanish in translation. The book was dumping
    them verbatim, so a reader saw `漁{1りょう}` - and on a device with no Japanese
    font, the reading degraded to boxes and the row read as `{1@x}`. That is the
    markup leaking into the product, not a font problem to wave away.

    The annotation counts the BASE characters it applies to: `{1りょう}` glosses the
    single character in front of it, `城下町{3じょうかまち}` the three. Anything that
    does not carry that count, or that has no base text to attach to, is dropped
    rather than guessed at, because inventing a base would misattribute a reading.
    """
    pat = config.source_only_re()
    if pat is None:
        return esc(s)
    out = []
    pos = 0
    for m in pat.finditer(s):
        body = m.group(0)[1:-1]
        digits = re.match(r'(\d+)', body)
        reading = body[digits.end():] if digits else ''
        n = int(digits.group(1)) if digits else 0
        head = s[pos:m.start()]
        if not n or not reading or len(head) < n:
            out.append(esc(head))
            pos = m.end()
            continue
        out.append(esc(head[:-n]))
        out.append(f'<ruby>{esc(head[-n:])}<rt>{esc(reading)}</rt></ruby>')
        pos = m.end()
    out.append(esc(s[pos:]))
    return ''.join(out)

def esc_attr(s):
    """`esc` keeps quotes readable in prose, which is wrong inside an attribute."""
    return html.escape(s, quote=True)


def paras(s, cls):
    render = ruby if cls == 'en' else esc
    return f'<div class="{cls}">' + ''.join(
        f'<p>{render(p)}</p>' for p in flow(s)) + '</div>'


def page(title, toc, body, subtitle='', extra_head=''):
    # The config script loads FIRST so `app.js` can see that the search box
    # belongs to the API, and `fb.js` reads the same globals.
    if fb_api():
        extra_head += '\n<script src="fbcfg.js"></script>'
    fb = ('<script src="fb.js"></script>' if fb_api() else '')
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<meta name="robots" content="noindex,nofollow">
<link rel="stylesheet" href="style.css">{extra_head}
</head><body>
<header><div class="hwrap">
<div class="brand">{esc(book_name())} 대본집<small>{esc(book_name_en())} · KOREAN SCRIPT BOOK</small></div>
<div class="grow"></div>
<input type="search" id="q" placeholder="{'전체 대본에서 검색 (단어 일부도 됩니다)' if fb_api() else '원문·번역 검색'}">
<span id="cnt" style="font-size:12px;color:var(--dim)"></span>
<button class="btn" data-mode="both">대역</button>
<button class="btn" data-mode="ko">한국어</button>
<button class="btn" data-mode="en">English</button>
{f'<a class="btn donate" href="{esc_attr(donate_url())}">☕ 후원하기</a>' if donate_url() else ''}
</div></header>
<div class="layout">
<nav class="toc">{toc}</nav>
<main><h1>{esc(title)}</h1>{f'<p class="sub">{subtitle}</p>' if subtitle else ''}
{body}
<footer>ROM 원문 + 한글패치 봉인 매니페스트에서 자동 생성. 게임 데이터 자체는 포함하지 않습니다.</footer>
</main></div>
<script src="app.js"></script>{fb}</body></html>"""


def book_name():
    """The Korean display name of the title, declared - never guessed.

    A book that prints another game's name is worse than one with a plain name, and
    transliterating an English title in code would invent a rendering the project never
    decided. Falls back to the declared latin title.
    """
    return config.prof('book_title_ko') or config.cfg().get('title') or '한글화'


def book_name_en():
    return (config.cfg().get('title') or '').upper()


# One page per section once the book is too large to open. Measured: the reference title is
# 660 story rows in a 460KB page, while a full JRPG script is 65836 rows and renders a 22MB
# page that a browser cannot usefully display. The threshold is a size decision, not a title
# fact, so it lives here rather than in a profile.
PAGE_ROW_LIMIT = 4000


def row_anchor(fam, key):
    """Deep-link target for a single row, shared with the search index.

    `hanpatch.feedback.row_anchor` calls this, so a search hit and the rendered
    row can never disagree about the fragment.
    """
    return 'r-' + slug(fam) + '--' + slug(key)


def slug(sid):
    """A section id that is safe in a file name and a URL fragment.

    Family-shaped ids like `#100000` carry a character that ends a URL at the fragment, so
    an unslugged link silently points at the wrong page.
    """
    s = re.sub(r'[^A-Za-z0-9_-]+', '-', sid).strip('-')
    return s or 'sec'


def build():
    if OUT is None:
        _out()
    src, man, digest = load()
    os.makedirs(OUT, exist_ok=True)
    sections = dialogue_sections(src, man)

    paged = sum(len(s['rows']) for s in sections.values()) > PAGE_ROW_LIMIT

    def href(sid):
        return f'p-{slug(sid)}.html#{slug(sid)}' if paged else f'story.html#{sid}'

    nav_story, nav_field, nav_rules = [], [], []
    sn = 0
    for sid, sec in sections.items():
        kind = sec.get('kind', 'story')
        if kind == 'story':
            sn += 1
            nav_story.append(f'<a href="{href(sid)}">'
                             f'<i>{sn}</i>{esc(sec["title_ko"])}</a>')
        elif kind == 'field':
            nav_field.append(f'<a href="{href(sid)}">{esc(sec["title_ko"])}</a>')
        else:
            nav_rules.append(f'<a href="{href(sid)}">{esc(sec["title_ko"])}</a>')
    nav_app = [f'<a href="appendix.html#{a[0]}">{esc(a[1])}</a>' for a in APPENDICES]

    def toc(active=''):
        return (f'<h3>스토리 순서 ({sn})</h3>{"".join(nav_story)}'
                f'<h3>필드 메시지</h3>{"".join(nav_field)}'
                f'<h3>규칙 해설</h3>{"".join(nav_rules)}'
                f'<h3>부록</h3>{"".join(nav_app)}'
                f'<h3>기타</h3><a href="index.html">표지 · 통계</a>'
                f'<a href="script.md">Markdown 원본</a>')

    # ---- story page(s)
    ids = list(sections)
    story_ids = [i for i in ids if sections[i].get('kind') == 'story']

    def section_html(n, sid, sec, anchor, prev_href, next_href):
        kind = sec.get('kind', 'story')
        num = ''
        if kind == 'story':
            num = f'<span class="ord">{story_ids.index(sid) + 1}/{len(story_ids)}</span> '
        nav = '<div class="secnav noprint">'
        nav += (f'<a href="{prev_href[0]}">◀ {esc(prev_href[1])}</a>'
                if prev_href else '<span></span>')
        nav += (f'<a href="{next_href[0]}">{esc(next_href[1])} ▶</a>'
                if next_href else '<span></span>')
        nav += '</div>'
        out = [f'<section class="sec" data-kind="{kind}" data-sid="{sid}">',
               f'<h2 id="{anchor}">{num}{esc(sec["title_ko"])} '
               f'<span style="font-size:13px;color:var(--dim)">'
               f'{esc(sec["title_en"])}</span></h2>']
        for key, en, ko in sec['rows']:
            out.append(f'<div class="row" id="{row_anchor(sid, key)}" '
                       f'data-fam="{esc(sid)}" data-key="{esc(key)}">'
                       f'<span class="k">{esc(key)}</span>'
                       f'{paras(ko, "ko")}{paras(en, "en")}</div>')
        out.append(nav)
        out.append('</section>')
        return ''.join(out)

    total_rows = sum(len(s['rows']) for s in sections.values())
    lead = (f'<div class="readbar noprint">'
            f'<span>읽기 범위</span>'
            f'<button class="btn" data-scope="story">본편만</button>'
            f'<button class="btn" data-scope="story+field">본편+필드</button>'
            f'<button class="btn" data-scope="all">전체</button>'
            f'<span class="grow"></span>'
            f'<a class="btn" id="resume" href="'
            f'{href(story_ids[0]) if paged else "#" + story_ids[0]}">이어서 읽기</a>'
            f'</div>')
    if paged:
        # One section per page, plus a contents page that keeps `story.html` a valid entry
        # point. Every link in the shared table of contents already points at these files.
        for n, (sid, sec) in enumerate(sections.items()):
            prev_id = ids[n - 1] if n else None
            next_id = ids[n + 1] if n + 1 < len(ids) else None
            html = section_html(
                n, sid, sec, slug(sid),
                (href(prev_id), sections[prev_id]['title_ko']) if prev_id else None,
                (href(next_id), sections[next_id]['title_ko']) if next_id else None)
            open(f'{OUT}/p-{slug(sid)}.html', 'w').write(page(
                sec['title_ko'], toc(), html,
                f'{len(sec["rows"])}개 대사 블록 · {n + 1}/{len(ids)} 섹션'))
        contents = ['<div class="rows">']
        for n, (sid, sec) in enumerate(sections.items(), 1):
            contents.append(
                f'<div class="row"><span class="k">{n}/{len(ids)}</span>'
                f'<p><a href="{href(sid)}">{esc(sec["title_ko"])}</a> '
                f'<span style="color:var(--dim)">{len(sec["rows"])}개 블록</span></p>'
                f'</div>')
        contents.append('</div>')
        open(f'{OUT}/story.html', 'w').write(page(
            '본편 대본 · 차례', toc(), lead + ''.join(contents),
            f'전체 {len(sections)}섹션 · {total_rows}개 대사 블록 · '
            f'섹션마다 한 쪽으로 나뉘어 있습니다'))
    else:
        body = [section_html(
            n, sid, sec, sid,
            (f'#{ids[n - 1]}', sections[ids[n - 1]]['title_ko']) if n else None,
            (f'#{ids[n + 1]}', sections[ids[n + 1]]['title_ko'])
            if n + 1 < len(ids) else None)
            for n, (sid, sec) in enumerate(sections.items())]
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
            ab.append(f'<tr id="{row_anchor(family, key)}" '
                      f'data-fam="{esc(family)}" data-key="{esc(key)}">'
                      f'<td class="key">{esc(key)}</td>'
                      f'<td class="ko">{esc(clean(ko, False))}</td>'
                      f'<td class="en">{ruby(clean(en, False))}</td></tr>')
        ab.append('</tbody></table>')
    open(f'{OUT}/appendix.html', 'w').write(page(
        '부록 · 설정집', toc(), ''.join(ab),
        ' · '.join(f'{k} {v}' for k, v in app_counts.items() if v)))

    # ---- index
    n_story = sum(len(s['rows']) for k, s in sections.items() if not k.startswith('fm'))
    n_field = sum(len(s['rows']) for k, s in sections.items() if k.startswith('fm'))
    n_app = sum(app_counts.values())
    scene_grammar = 'dialogue' in src
    stat = f"""
<div class="stat">
<span><b>{n_story}</b> 본편 대사 블록</span>
<span><b>{n_field}</b> 필드 메시지</span>
<span><b>{n_app}</b> 부록 항목</span>
<span><b>{len(man)}</b> 전체 번역 문자열</span>
<span>매니페스트 <b>{esc(digest[:16])}</b></span>
</div>"""
    if not scene_grammar:
        # The prose below describes the reference title's chapters, field-message layout and
        # source-language policy. None of that is derivable for another title, and printing
        # it anyway makes the book state facts about a game it is not. State only what was
        # measured, and let the contents page carry the structure.
        idx = stat + f"""
<div class="row"><div class="ko">
<p><b>{esc(book_name())}</b> 한글화의 전체 텍스트를 원문과 한국어 번역으로 나란히
정리한 대본집입니다.</p>
<p>본문은 게임이 텍스트를 담고 있는 순서대로, 컨테이너의 묶음 단위별로 배열했습니다.
전체 {len(sections)}개 묶음, {n_story}개 대사 블록입니다.</p>
<p>모든 문자열은 ROM에서 직접 추출한 원문과, 한글패치가 실제로 탑재하는
<span style="font-family:var(--mono);font-size:13px">manifest.json</span>의 봉인된
번역문에서 자동 생성됩니다. 화면에 나오는 문장과 이 문서의 문장은 같습니다.</p>
<p>상단 버튼으로 <b>대역 / 한국어 / English</b> 표시를 바꿀 수 있고, 검색창은 원문과
번역문을 동시에 찾습니다.</p>
<p><a href="story.html"><b>본문 차례로 이동 →</b></a></p>
</div></div>
{feedback_intro(n_story + n_field) if fb_api() else ''}
{donate_intro() if donate_url() else ''}
<h2>구성</h2>
<table><thead><tr><th>구분</th><th class="ko">내용</th><th>분량</th></tr></thead><tbody>
<tr><td class="key">story.html</td><td class="ko">묶음별 차례 (묶음마다 한 쪽)</td><td>{len(sections)}</td></tr>
<tr><td class="key">script.md</td><td class="ko">전체 대본 Markdown 원본 (보관·인용용)</td><td>1</td></tr>
</tbody></table>
"""
    else:
        idx = stat + f"""
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
        f'{book_name()} 대본집', toc(), idx,
        '영문 원문 · 한국어 번역 대역본'))

    # ---- markdown
    md = [f'# {book_name()} 대본집',
          '',
          f'- 매니페스트 digest: `{digest}`',
          f'- 본편 대사 블록 {n_story} · 필드 메시지 {n_field} · 부록 항목 {n_app}']
    if scene_grammar:
        md.append('- 산문 기준 원문은 영어판, 고유명은 일본어 원판 표기를 따릅니다.')
    md.append('')
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
    if fb_api():
        from hanpatch import feedback as fbmod
        with open(f'{OUT}/fbcfg.js', 'w', encoding='utf-8') as fh:
            fh.write('var HPFB_API=%s;\nvar HPFB_KINDS=%s;\n' % (
                json.dumps(fb_api()), json.dumps(fbmod.KINDS, ensure_ascii=False)))
        with open(f'{OUT}/fb.js', 'w', encoding='utf-8') as fh:
            fh.write(FB_JS)
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
