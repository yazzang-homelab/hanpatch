"""Durable, row-oriented repair loop around the release gates.

The loop deliberately does not try to make a gate less strict.  A gate run
produces a durable queue of the rows it named; ``next`` exposes a bounded part
of that queue and ``submit`` accepts only translations that pass the same
checks used by the normal manifest builder.
"""
import contextlib
import datetime
import hashlib
import io
import json
import os
import re
import tempfile

from hanpatch import config


# Imports that inspect the title profile are lazy.  Apart from making ``py_compile``
# useful in a checkout with no project selected, this gives tests a small seam for
# replacing a gate or validator without constructing the real corpus.
pipeline = None
qagate = None
capacity = None
wrap = None
audit = None
glossary = None
translate = None
register = None
manifest = None
tm = None


def _module(name):
    global pipeline, qagate, capacity, wrap, audit, glossary, translate, register
    value = globals()[name]
    if value is None:
        if name == 'pipeline':
            from hanpatch import pipeline as value
        elif name == 'qagate':
            from hanpatch import qagate as value
        elif name == 'capacity':
            from hanpatch import capacity as value
        elif name == 'wrap':
            from hanpatch import wrap as value
        elif name == 'audit':
            from hanpatch import audit as value
        elif name == 'glossary':
            from hanpatch import glossary as value
        elif name == 'translate':
            from hanpatch import translate as value
        elif name == 'register':
            from hanpatch import register as value
        elif name == 'manifest':
            from hanpatch import manifest as value
        elif name == 'tm':
            from hanpatch import tm as value
        else:
            # A name this function does not know would otherwise be reported as
            # a KeyError from the globals lookup above, which reads like state
            # corruption rather than what it is.
            raise RuntimeError(f'loop._module does not know the module {name!r}')
        globals()[name] = value
    return value


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _report_stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def _state_path():
    return config.p('loop', 'state.json')


def _loop_dir():
    path = config.p('loop')
    os.makedirs(path, exist_ok=True)
    return path


def _report_path():
    base = config.p('loop', f'report-{_report_stamp()}.json')
    path = base
    n = 1
    while os.path.exists(path):
        stem, ext = os.path.splitext(base)
        path = f'{stem}-{n}{ext}'
        n += 1
    return path


def _default_state():
    # ``batchKeys`` and ``batchSeq`` are operational fields.  The public state
    # fields remain the documented schema; these two fields make an in-flight
    # batch recoverable after a process or browser turn dies.
    return {
        'iteration': 0,
        'batchId': None,
        'pending': {},
        'lastGate': {},
        'history': [],
        'batchKeys': {},
        'batchSeq': 0,
    }


def _write_json(path, value):
    """Atomically replace a JSON document on the same filesystem."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.loop-', suffix='.tmp', dir=parent or None)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(value, fh, ensure_ascii=False, indent=1, sort_keys=True)
            fh.write('\n')
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _valid_state(value):
    if not isinstance(value, dict):
        return False
    if not isinstance(value.get('iteration'), int) or value['iteration'] < 0:
        return False
    if value.get('batchId') is not None and not isinstance(value['batchId'], str):
        return False
    pending = value.get('pending')
    if not isinstance(pending, dict):
        return False
    for cls, rows in pending.items():
        if not isinstance(cls, str) or not isinstance(rows, dict):
            return False
        for key, problems in rows.items():
            if not isinstance(key, str) or not isinstance(problems, list):
                return False
            if not all(isinstance(problem, str) for problem in problems):
                return False
    if not isinstance(value.get('lastGate'), dict):
        return False
    if not isinstance(value.get('history'), list):
        return False
    for rec in value['history']:
        if not isinstance(rec, dict):
            return False
    batch_keys = value.get('batchKeys', {})
    if not isinstance(batch_keys, dict):
        return False
    if not all(isinstance(k, str) and isinstance(v, str)
               for k, v in batch_keys.items()):
        return False
    if not isinstance(value.get('batchSeq', 0), int):
        return False
    return True


def _load_state():
    """Load state, preserving a malformed document before recovering it."""
    path = _state_path()
    _loop_dir()
    if not os.path.exists(path):
        state = _default_state()
        _write_json(path, state)
        return state
    try:
        with open(path, encoding='utf-8') as fh:
            value = json.load(fh)
    except (OSError, ValueError, TypeError):
        value = None
    if _valid_state(value):
        # Older state documents may omit only the operational fields.
        value.setdefault('batchKeys', {})
        value.setdefault('batchSeq', 0)
        return value
    stamp = _report_stamp()
    corrupt = f'{path}.corrupt-{stamp}'
    # A same-second collision should preserve both damaged files rather than
    # replacing the first forensic copy.
    n = 1
    while os.path.exists(corrupt):
        corrupt = f'{path}.corrupt-{stamp}-{n}'
        n += 1
    try:
        os.replace(path, corrupt)
    except OSError:
        # If a concurrent process moved it first, recovery is still preferable
        # to exposing a half-valid in-memory state.
        pass
    state = _default_state()
    _write_json(path, state)
    return state


def _save_state(state):
    _write_json(_state_path(), state)


def _record(state, op, accepted=0, rejected=0, stage=None):
    state.setdefault('history', []).append({
        'at': _utc_now(),
        'op': op,
        'accepted': int(accepted),
        'rejected': int(rejected),
        'stage': stage,
    })


def _pending_count(state, exclude_batch=True):
    keys = {key for rows in state.get('pending', {}).values() for key in rows}
    if not exclude_batch:
        return len(keys)
    return len(keys - set(state.get('batchKeys', {})))


def _pending_summary(state):
    return {cls: len(rows) for cls, rows in sorted(state.get('pending', {}).items())
            if rows}


def _last_gate_view(state):
    gate = state.get('lastGate') or {}
    if not gate:
        return {'ok': False, 'stage': None, 'detail': 'no gate run yet'}
    return {k: gate[k] for k in ('ok', 'stage', 'detail') if k in gate}


def _next_action(state):
    gate = state.get('lastGate') or {}
    if gate.get('ok'):
        return 'publish'
    if state.get('batchId') and state.get('batchKeys'):
        return ('hanpatch loop submit --batch %s --file <fixes.json>'
                % state['batchId'])
    if _pending_count(state):
        return 'hanpatch loop next --limit 40'
    stage = gate.get('stage')
    if stage in ('glossary', 'materialize', 'manifest'):
        return 'update glossary/profile inputs, then hanpatch loop gate'
    return 'hanpatch loop gate'


def _source_rows():
    path = config.src_path()
    if not os.path.exists(path):
        return {}, {}
    src = config.load_object(path, 'the extracted source')
    by_key = {}
    for family, items in src.items():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or 'key' not in item:
                continue
            key = f'{family}/{item["key"]}'
            by_key[key] = (family, item)
    return src, by_key


def _tm_db(_cache={}):
    """The merged translation memory, loaded once per process.

    `tm.load()` walks every per-family shard - about 700 files for Classic
    Dungeon X2 - so a per-row call would turn one batch into thousands of
    directory scans inside a turn that a single Pro message paid for.
    """
    if 'db' not in _cache:
        _cache['db'] = _module('tm').load()
    return _cache['db']


def _translations():
    path = config.out('text_ko.json')
    if not os.path.exists(path):
        return {}
    return config.load_object(path, 'the target translation')


def _translation_for(doc, family, key, en=None):
    """The translation a gate run would use for this row, or None.

    Resolution order mirrors `manifest.build` exactly - the row override wins,
    otherwise the translation memory answers:

        ko = ov if ov is not None else tm.lookup(tmdb, en)

    Reading only the override would have reported "no current translation" for
    every row of a title whose text lives in the TM rather than in per-row
    overrides. Classic Dungeon X2 is exactly that: 11,335 rows, all translated,
    no override file at all. A model told the current translation is null
    retranslates from scratch and loses the established terms and register that
    the glossary and audit gates then reject it for.
    """
    if isinstance(doc, dict):
        table = doc.get(family)
        if isinstance(table, dict) and key in table:
            return table[key]
        full = f'{family}/{key}'
        value = doc.get(full)
        if isinstance(value, str):
            return value
    if en:
        found = _module('tm').lookup(_tm_db(), en)
        if isinstance(found, str):
            return found
    return None


def _source_text(item):
    if not item:
        return None
    # ``en`` is the historical field name even for Japanese-source projects;
    # ``jp`` is the player-facing original retained by newer extractors.
    value = item.get('en')
    if isinstance(value, str) and value.strip():
        return value
    value = item.get('jp')
    return value if isinstance(value, str) and value.strip() else None


def _width_lines(text, budget=None):
    """Return the widest rendered line and line count without reading QA data."""
    if not isinstance(text, str):
        return None, None
    wm = _module('wrap')
    if budget is not None:
        try:
            text = wm.rewrap(text, budget, soft=True)
        except (Exception, SystemExit):
            pass
    chunks = text.replace('<page>', '\n').split('\n')
    for token in config.prof('hard_break') or ():
        chunks = [part for chunk in chunks for part in chunk.split(token)]
    widths = []
    try:
        widths = [wm.text_width(chunk) for chunk in chunks]
    except (Exception, SystemExit):
        return None, len(chunks) if chunks else None
    return max(widths, default=0), len(chunks)


def _budget_for(family, source=None, ko=None, key=None):
    try:
        wm = _module('wrap')
        max_px = wm.budget_for(family)
    except (Exception, SystemExit):
        return None
    row = _row_bound(source)
    if row:
        # The row governs itself, so the family number is the WRONG constraint to
        # publish: it is the widest row anywhere in the family. A `db__MONSTER.DAT`
        # row whose own source draws 122px lines would be handed a ~700px budget,
        # the rewrite would satisfy it, and the gate would refuse the row again for
        # the same reason it refused the last one.
        max_px, slots = row
        current = None
        if ko is not None:
            current, _lines = _width_lines(ko, max_px)
        return {'maxPx': max_px, 'lines': slots, 'currentPx': current,
                'boundedBy': 'this row\'s own source lines'}
    try:
        lines = None
        current = None
        if source is not None:
            _src_width, lines = _width_lines(source, max_px)
        if ko is not None:
            current, _ko_lines = _width_lines(ko, max_px)
        return {'maxPx': max_px, 'lines': lines, 'currentPx': current}
    except (Exception, SystemExit):
        return {'maxPx': max_px, 'lines': None, 'currentPx': None}


def _row_bound(source):
    """(px, lines) this row's OWN source proves, or None when it proves nothing.

    Only where `wrap` says the row draws its own lines - a container that stores
    one display line per record line, and a title whose budgets are lower bounds
    rather than measured boxes. Elsewhere the family budget is the measured box and
    this must not narrow it.
    """
    if not isinstance(source, str):
        return None
    try:
        wm = _module('wrap')
        if not wm.row_draws_its_own_lines(source):
            return None
        pages = source.split('<page>')
        px = max(wm.row_budget(page) for page in pages)
        slots = sum(len(wm.row_line_slots(page)) for page in pages)
    except (Exception, SystemExit):
        return None
    return (round(px), slots) if px and slots else None


def _row_payload(anchor, problems, by_key, translations):
    found = by_key.get(anchor)
    family, item = found if found else (None, None)
    key = anchor.split('/', 1)[1] if '/' in anchor else anchor
    source = _source_text(item)
    ko = (_translation_for(translations, family, key, (item or {}).get('en'))
          if found else None)
    row_problems = list(problems)
    if not found:
        row_problems.append('source row not found in text_src.json')
    elif source is None:
        row_problems.append('source text not found in text_src.json')
    if found and ko is None:
        row_problems.append('current translation not found in text_ko.json')
    budget = _budget_for(family, source, ko, key) if found else None
    terms = {}
    if source is not None:
        try:
            terms = _module('glossary').relevant(_module('glossary').load(),
                                                [source], family)
        except (Exception, SystemExit):
            terms = {}
    try:
        reg = _module('register')
        marker = reg.marker_of(source or '')
    except (Exception, SystemExit):
        marker = None
    if marker not in ('plain', 'polite'):
        marker = config.prof('register_default', 'plain')
        if marker not in ('plain', 'polite'):
            marker = 'plain'
    return {
        'key': anchor,
        'jp': source,
        'ko': ko,
        'problems': row_problems,
        'budget': budget,
        'hints': {'terms': dict(terms), 'register': marker},
    }


def _add_problem(pending, cls, key, problem):
    if not isinstance(problem, str):
        problem = str(problem)
    table = pending.setdefault(cls, {})
    old = table.setdefault(key, [])
    if problem not in old:
        old.append(problem)


def _parse_failure(values, pending, cls='qagate'):
    """Add ``mkey: problem`` values while retaining unparseable evidence."""
    for value in values:
        text = str(value)
        if ': ' in text:
            key, problem = text.split(': ', 1)
            if key:
                _add_problem(pending, cls, key, problem)
                continue
        # qagate's stale-waiver values are pair hashes, not row anchors.  Keep
        # them addressable rather than dropping evidence we cannot map.
        _add_problem(pending, cls, text, f'{cls} failure: {text}')


def _capacity_pending(pending):
    src, by_key = _source_rows()
    try:
        translations = _translations()
    except (Exception, SystemExit):
        translations = {}
    wm = _module('wrap')
    cap = _module('capacity')
    for family, items in src.items():
        for item in items:
            if not isinstance(item, dict) or 'key' not in item:
                continue
            anchor = f'{family}/{item["key"]}'
            source = _source_text(item)
            ko = _translation_for(translations, family, item['key'],
                                  item.get('en'))
            if source is None or ko is None:
                continue
            group = cap.group(family, item['key'])
            try:
                _fixed, problems = wm.fits(source, ko, family, group)
            except (Exception, SystemExit):
                continue
            for problem in problems or ():
                _add_problem(pending, 'capacity', anchor, problem)


def _audit_text(detail):
    # pipeline.GateFailed carries the captured audit output after the first
    # newline.  A test double may provide only a stage/detail pair, in which
    # case run audit.main once and capture its report here.
    if '\n' in detail:
        return detail.split('\n', 1)[1]
    match = re.match(r'^\s*[^\s:]+:([^\s]+)', detail)
    if match and (not match.group(1).isdigit() or ' :: ' in detail):
        return detail
    ad = _module('audit')
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = ad.main()
    if isinstance(result, tuple) and len(result) == 2:
        return str(result[1])
    return buf.getvalue()


def _audit_anchor(row):
    """`family:key` as audit writes it -> `family/key` as the loop addresses it."""
    family, sep, key = str(row).partition(':')
    return f'{family}/{key}' if sep else str(row)


def _audit_pending(detail, pending):
    # Prefer the complete lists the audit gate publishes. Its printed report
    # shows only the first four rows of each problem, so parsing the text gave
    # six rows for a run that actually had 353 - and a loop that repaired those
    # six would fail the same gate forever.
    ad = _module('audit')
    fails = getattr(ad, 'LAST_FAILS', None)
    if isinstance(fails, dict) and fails:
        advisory = set(getattr(ad, 'ADVISORY', ()))
        # Advisory problems are excluded from the gate's own hard-failure count,
        # so repairing them cannot turn it green. Feeding them to the loop would
        # spend turns - and, on Pro, message quota - on rows the gate never asked
        # for.
        blocking = {name: rows for name, rows in fails.items()
                    if name not in advisory}
        if blocking:
            for name, rows in blocking.items():
                for row in rows:
                    anchor, _sep, why = str(row).partition(' :: ')
                    _add_problem(pending, 'audit', _audit_anchor(anchor.strip()),
                                 why.strip() or name)
            return
        # Only advisory problems remain, yet the gate failed. Something outside
        # `fails` did it - an unresolved review or a rejected decision - and
        # falling through to the text parse keeps that visible instead of
        # reporting an empty repair set.
    text = _audit_text(detail)
    parsed = False
    unparsed = []
    for line in text.splitlines():
        m = re.match(r'^\s{2,}([^\s:]+):([^\s]+)(?:\s+::\s*(.*))?\s*$', line)
        if not m:
            if line.strip():
                unparsed.append(line)
            continue
        family, key, problem = m.groups()
        anchor = f'{family}/{key}'
        _add_problem(pending, 'audit', anchor, problem or 'audit failure')
        parsed = True
    if unparsed:
        _add_problem(pending, 'audit', '__unparsed__', '\n'.join(unparsed))
    elif not parsed and text.strip():
        _add_problem(pending, 'audit', '__unparsed__', text)


def _qagate_pending(pending):
    qa = _module('qagate')
    try:
        blocked, bad, stale = qa.validate(man=None, quiet=True)
    except TypeError:
        blocked, bad, stale = qa.validate()
    _parse_failure(blocked, pending)
    _parse_failure(bad, pending)
    _parse_failure(stale, pending)


def _failure_pending(stage, detail):
    pending = {}
    if stage == 'qagate':
        try:
            _qagate_pending(pending)
        except (Exception, SystemExit) as exc:
            _add_problem(pending, 'qagate', '__unparsed__',
                         f'could not read qagate findings: {exc}; {detail}')
    elif stage == 'capacity':
        _capacity_pending(pending)
    elif stage == 'audit':
        _audit_pending(detail, pending)
    return pending


SEALED_TEXT = os.path.join('loop', 'sealed-text.json')


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _seal_text():
    """Write the passing run's sealed strings to one file and hash it.

    A git worktree cannot re-derive the real proof. The sealed manifest is a
    gitignored multi-megabyte artefact built from files the worktree does not
    have - the ROM, the built fonts, the hundreds-of-megabytes verdict ledger -
    so a check suite running there has nothing to recompute it from.

    Nor can the worktree just hash the project's translation file, because there
    isn't one shape of it: dq7 and Crimson Shroud keep `work/<target>/text_ko.json`,
    while Classic Dungeon X2 keeps its translation in ~700 per-container shards.
    Hashing whichever file happened to exist would have silently sealed nothing
    for that title.

    What every title does share is the manifest the gates seal, and its `entries`
    are already exactly `{key: translation}`. Writing those to one file with
    sorted keys gives a single, title-independent, worktree-checkable artefact,
    and its sha256 is what a pull request is judged against.

    Returns `(sha256, count)`, or `(None, 0)` when no sealed manifest exists.
    """
    try:
        doc = _module('manifest').load()
    except SystemExit:
        # `manifest.load` raises SystemExit for the legitimate absences: no
        # manifest on disk, or one whose re-derived digest does not match. The
        # run simply has nothing to seal, and publish refuses on that.
        #
        # Only SystemExit is swallowed. A broader except here hid a real coding
        # error once already - a module this function could not import returned
        # "nothing to seal" and a passing gate silently produced no seal at all.
        return None, 0
    entries = doc.get('entries') if isinstance(doc, dict) else None
    if not isinstance(entries, dict):
        return None, 0
    path = config.p(SEALED_TEXT)
    _write_json(path, entries)
    return _sha256(path), len(entries)


def _gate_success_report(rep):
    digest = rep.get('manifest') if isinstance(rep, dict) else None
    if digest is not None:
        digest = str(digest)[:16]
    sealed_sha, sealed_n = _seal_text()
    return {'ok': True, 'stage': None, 'detail': 'all gates passed',
            'digest': digest, 'textSha256': sealed_sha,
            'sealedEntries': sealed_n}


def gate():
    """Run the strict pipeline once and persist its row-level outcome."""
    state = _load_state()
    state['iteration'] = int(state.get('iteration', 0)) + 1
    state['batchId'] = None
    state['batchKeys'] = {}
    stage = None
    detail = ''
    try:
        # ``quiet=True`` is the normal contract; redirect as a second guard so
        # a title adapter or a test double cannot corrupt the CLI's one-JSON
        # stdout stream.
        with contextlib.redirect_stdout(io.StringIO()):
            rep = _module('pipeline').gates(quiet=True)
        result = _gate_success_report(rep)
        pending = {}
    except BaseException as exc:  # a gate failure is data, not a CLI traceback
        stage = getattr(exc, 'stage', None) or 'unknown'
        detail = getattr(exc, 'detail', None) or str(exc)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                pending = _failure_pending(stage, str(detail))
        except BaseException as derive_exc:
            pending = {}
            _add_problem(pending, stage, '__unparsed__',
                         f'{detail}; could not derive rows: {derive_exc}')
        # Recorded on failure too: the next turn compares it to decide whether
        # the translation actually changed since the last attempt, so a loop
        # that resubmits identical text is visible instead of silent.
        result = {'ok': False, 'stage': stage, 'detail': str(detail),
                  'textSha256': None, 'sealedEntries': 0}
    state['pending'] = pending
    report_path = _report_path()
    result['iteration'] = state['iteration']
    result['pending'] = _pending_summary(state)
    result['reportPath'] = report_path
    report_doc = {'op': 'gate'}
    report_doc.update(result)
    report_doc['pendingCounts'] = result.get('pending', {})
    report_doc['pending'] = pending
    _write_json(report_path, report_doc)
    state['lastGate'] = result
    _record(state, 'gate', stage=stage)
    _save_state(state)
    if result['ok']:
        return {
            'op': 'gate', 'ok': True, 'stage': None,
            'digest': result.get('digest'),
            'textSha256': result.get('textSha256'),
            'iteration': state['iteration'],
            'nextAction': 'publish',
        }
    out = {
        'op': 'gate', 'ok': False, 'stage': stage, 'detail': str(detail),
        'pending': _pending_summary(state),
        'reportPath': report_path,
        'textSha256': result.get('textSha256'),
        'iteration': state['iteration'],
        'nextAction': _next_action(state),
    }
    return out


def status():
    """Read only the durable state; never invoke a gate."""
    state = _load_state()
    try:
        src, _by_key = _source_rows()
        total_rows = sum(len(items) for items in src.values())
    except (Exception, SystemExit):
        total_rows = 0
    return {
        'op': 'status',
        'title': config.title(),
        'target': config.target(),
        'iteration': state['iteration'],
        'gate': _last_gate_view(state),
        'pending': _pending_summary(state),
        'totalRows': total_rows,
        'nextAction': _next_action(state),
    }


def _is_stale_seal(problems):
    """True when every problem on a row is the SEAL being stale, not the text.

    `manifest.build` refuses wholesale: one row the rules cannot store keeps the
    whole manifest from resealing, so every row the rules would rewrite goes on
    reporting `sealed != normalised` until that one row is fixed. Measured on
    Classic Dungeon X2 after the per-row layout rule landed: 353 of 393 pending
    rows were that, all clearing themselves on the next successful seal, and 40
    were the rows actually blocking it.

    So these rows go LAST. Handing them out first spends a turn rewriting text
    that needs no rewriting, and the seal - the thing that clears them - stays
    blocked the whole time.
    """
    return bool(problems) and all('!= normalised' in p for p in problems)


def next_batch(limit=40):
    """Expose at most ``limit`` unassigned rows and remember their batch."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
        raise SystemExit('--limit must be an integer from 1 to 200')
    state = _load_state()
    _src, by_key = _source_rows()
    try:
        translations = _translations()
    except (Exception, SystemExit):
        translations = {}
    active = state.get('batchKeys', {})
    if state.get('batchId') and active:
        selected = [(key, cls) for key, cls in active.items()]
        batch_id = state['batchId']
    else:
        selected = []
        selected_keys = set(active)
        for cls in sorted(state.get('pending', {})):
            rows_of = state['pending'][cls]
            for key in sorted(rows_of, key=lambda k: (_is_stale_seal(rows_of[k]), k)):
                if key not in selected_keys:
                    selected.append((key, cls))
                    selected_keys.add(key)
                if len(selected) >= limit:
                    break
            if len(selected) >= limit:
                break
        seq = int(state.get('batchSeq', 0)) + 1
        batch_id = f'b-{seq:04d}'
        state['batchSeq'] = seq
        state['batchId'] = batch_id
        state['batchKeys'] = {key: cls for key, cls in selected}
    rows = []
    for key, cls in selected:
        problems = list(state['pending'].get(cls, {}).get(key, []))
        row = _row_payload(key, problems, by_key, translations)
        # Lookup failures are evidence too; retain them in the durable queue.
        state['pending'].setdefault(cls, {})[key] = list(row['problems'])
        rows.append(row)
    _record(state, 'next', stage=(state.get('lastGate') or {}).get('stage'))
    _save_state(state)
    return {
        'op': 'next',
        'batchId': batch_id,
        'rows': rows,
        'remaining': _pending_count(state),
        'nextAction': ('hanpatch loop submit --batch %s --file <fixes.json>' % batch_id
                       if rows else _next_action(state)),
    }


def _remove_pending(state, key):
    for cls, rows in list(state.get('pending', {}).items()):
        if key in rows:
            del rows[key]
            if not rows:
                # Keep class names in state only while they have work, which
                # keeps status summaries concise and deterministic.
                del state['pending'][cls]


def _validate_translation(anchor, value, by_key):
    found = by_key.get(anchor)
    if not found:
        return None, 'source row not found in text_src.json'
    family, item = found
    if not isinstance(value, str):
        return None, 'translation must be a string'
    source = _source_text(item)
    if source is None:
        return None, 'source text not found in text_src.json'
    try:
        glmod = _module('glossary')
        terms = glmod.relevant(glmod.load(), [source], family)
        fixed, problems = _module('translate').check(
            source, value, terms, family,
            _module('capacity').group(family, item['key']))
    except BaseException as exc:
        return None, f'validation could not run: {exc}'
    if problems:
        return None, '; '.join(str(problem) for problem in problems)
    if not isinstance(fixed, str):
        return None, 'validator returned a non-text translation'
    return fixed, None


def submit(batch_id, fixes):
    """Validate and atomically merge only rows from the active batch."""
    if not isinstance(batch_id, str) or not batch_id:
        raise SystemExit('--batch is required')
    if not isinstance(fixes, dict):
        raise SystemExit('--file must contain a JSON object of key/translation pairs')
    state = _load_state()
    by_key = _source_rows()[1]
    matching_batch = state.get('batchId') == batch_id
    active = dict(state.get('batchKeys', {})) if matching_batch else {}
    accepted = 0
    rejects = []
    merged = None
    if active:
        try:
            merged = _translations()
        except (Exception, SystemExit):
            merged = {}
    for key, value in fixes.items():
        if key not in active:
            rejects.append({'key': key, 'reason': f'not in batch {batch_id}'})
            continue
        with contextlib.redirect_stdout(io.StringIO()):
            fixed, reason = _validate_translation(key, value, by_key)
        cls = active[key]
        if reason:
            state['pending'].setdefault(cls, {})[key] = [reason]
            rejects.append({'key': key, 'reason': reason})
            continue
        family, item = by_key[key]
        if isinstance(merged.get(family), dict):
            merged[family][item['key']] = fixed
        elif key in merged:
            # Keep a flat document flat when a small tool or fixture supplied
            # one; normal manifests use the family/key object shape.
            merged[key] = fixed
        else:
            table = merged.setdefault(family, {})
            table[item['key']] = fixed
        _remove_pending(state, key)
        accepted += 1
    if not matching_batch:
        # A stale or mistyped batch id must not cancel the real in-flight batch.
        # Its keys are still pending and the caller can retry with that id.
        state['batchId'] = state.get('batchId')
        # `rejected` is computed further down for the accepting path; on this
        # one - a batch id that does not match the in-flight batch - it was read
        # before it existed and the whole submit died with an UnboundLocalError,
        # taking the caller's already-translated rows with it.
        _record(state, 'submit', accepted=0, rejected=len(rejects),
                stage=(state.get('lastGate') or {}).get('stage'))
        _save_state(state)
        return {
            'op': 'submit', 'batchId': batch_id, 'accepted': 0,
            'rejected': len(rejects), 'rejects': rejects,
            'remaining': _pending_count(state, exclude_batch=False),
            'nextAction': ('hanpatch loop submit --batch %s --file <fixes.json>'
                           % state['batchId'] if state.get('batchId')
                           else _next_action(state)),
        }
    # Omitted rows remain actionable and are explicitly reported, rather than
    # silently making a model wait for a batch that can never complete.
    for key, cls in active.items():
        if key in fixes:
            continue
        reason = f'no fix submitted for batch {batch_id}'
        state['pending'].setdefault(cls, {})[key] = [reason]
        rejects.append({'key': key, 'reason': reason})
    if accepted:
        _write_json(config.out('text_ko.json'), merged)
    rejected = len(rejects)
    state['batchId'] = None
    state['batchKeys'] = {}
    _record(state, 'submit', accepted=accepted, rejected=rejected,
            stage=(state.get('lastGate') or {}).get('stage'))
    _save_state(state)
    return {
        'op': 'submit',
        'batchId': batch_id,
        'accepted': accepted,
        'rejected': rejected,
        'rejects': rejects,
        'remaining': _pending_count(state, exclude_batch=False),
        'nextAction': ('hanpatch loop next --limit 40'
                       if state.get('pending') else 'hanpatch loop gate'),
    }


def main(op, limit=40, batch=None, fixes=None):
    """Small programmatic dispatch used by the CLI and tests."""
    if op == 'status':
        return status()
    if op == 'next':
        return next_batch(limit)
    if op == 'gate':
        return gate()
    if op == 'submit':
        return submit(batch, fixes)
    raise SystemExit(f'unknown loop operation: {op}')

