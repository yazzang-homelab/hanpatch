"""Project and title-profile resolution.

Every path the pipeline touches is derived from one project file so the core
modules contain no title-specific constants.  A project is a directory holding
`hanpatch.json`:

    {
      "title":    "Crimson Shroud",
      "platform": "threeds",
      "adapter":  "crimson_shroud",
      "profile":  "profiles/crimson_shroud.json",
      "target":   "ko",
      "rom":      "game.cia"
    }

Resolution order for the project root:
  1. `$HANPATCH_PROJECT`
  2. the nearest parent of `$PWD` containing `hanpatch.json`
  3. `$PWD`
"""
import json
import os
import re
import sys

PROJECT_FILE = 'hanpatch.json'
_root = None
_cfg = None
_profile = None
_SOURCE_LANGS = ('en', 'ja')
# Residual-script detection modes this build implements. AUTO (`None`) resolves
# to the first for a Latin source and to the second otherwise.
_RESIDUAL_MODES = ('off', 'kana+kanji')

# Profile keys whose value must be an object, and keys whose value must be a
# list. A wrongly shaped value is not a style question: `dict.update` no-ops on a
# list, so `"terms": []` silently empties the glossary the title declared.
_OBJECT_KEYS = (
    'font_ttf', 'font_sheet', 'font_shade','terms', 'register', 'budget', 'capacity',
    'gate_thresholds', 'substitution_values')
_LIST_KEYS = ('models', 'name_keys', 'ui_only_families', 'ui_only_terms', 'hard_families',
              'hard_terms', 'kanji_allowlist', 'hard_break', 'page_break',
              'movable_tags', 'control_tags', 'literal_delimiters', 'font_src', 'font_out')
# `skip_families`, `skip_key_patterns` and `skip_value_patterns` are deliberately
# NOT validated: nothing reads them yet (`tm.is_skip` still carries the policy in
# code), and schema-validating a key that does nothing promises the operator it
# is honoured. They join the list above when the skip policy actually moves into
# the profile.
_MODE_KEYS = {'copied_spans_tokenizer': ('latin',)}
_BOOL_KEYS = ('fullwidth_is_content', 'engine_wraps')
_STRING_KEYS = ('judge_policy', 'book_title_ko')


def load_object(path, what):
    """Load a JSON document that MUST be an object, or fail with a diagnostic.

    Every state file this pipeline reads is a mapping. `json.load` will happily
    return a list or a scalar, after which `dict.update` silently no-ops and
    `.items()` raises a bare AttributeError deep inside a gate. Naming the file
    and the type actually found turns a traceback into a repairable message.
    """
    try:
        with open(path) as fh:
            data = json.load(fh)
    except ValueError as exc:
        raise SystemExit(f'{what} is not valid JSON: {path}: {exc}')
    if not isinstance(data, dict):
        raise SystemExit(f'{what} must be a JSON object: {path} holds a '
                         f'{type(data).__name__}')
    return data


PKG = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(PKG)


def root():
    """Absolute path of the project directory."""
    global _root
    if _root is None:
        env = os.environ.get('HANPATCH_PROJECT')
        if env:
            _root = os.path.abspath(env)
        else:
            d = os.path.abspath(os.getcwd())
            while True:
                if os.path.exists(os.path.join(d, PROJECT_FILE)):
                    break
                parent = os.path.dirname(d)
                if parent == d:
                    d = os.path.abspath(os.getcwd())
                    break
                d = parent
            _root = d
    return _root


def set_root(path):
    """Point the whole pipeline at another project (used by tests).

    The switch is all-or-nothing. The new profile is resolved and validated
    BEFORE any module cache is rebuilt, and any failure restores the previous
    root and re-derives the caches under it. Committing the root first and
    rebuilding caches afterwards would leave a caller that catches the error
    running the new project's paths against the old project's term tables,
    prompt and layout budgets, which is worse than not switching at all.
    """
    global _root, _cfg, _profile
    previous = (_root, _cfg, _profile)
    _root = os.path.abspath(path)
    _cfg = None
    _profile = None
    try:
        # A repoint is not an init: the target must already be a project. `cfg()`
        # deliberately tolerates a missing project file so `hanpatch init` can
        # run in an empty directory, and that tolerance would otherwise turn a
        # mistyped path into a silently accepted default configuration.
        if not os.path.exists(os.path.join(_root, PROJECT_FILE)):
            raise SystemExit(
                f'not a project: {os.path.join(_root, PROJECT_FILE)} is missing')
        profile()
        reset_module_caches()
    except BaseException:
        _root, _cfg, _profile = previous
        # Re-derive under the restored profile. If this raises too, the previous
        # project is itself broken and the caller must see that, unmasked.
        reset_module_caches()
        raise


def reset_module_caches():
    """Re-derive profile-dependent constants in every already-imported module.

    Several modules capture profile facts at import time because they are read
    per row: the tag grammar, the layout budgets, the term tables and the
    source-language-dependent translator prompt. Clearing only the config caches
    would leave those constants describing the PREVIOUS title, so a caller that
    switched projects would keep translating under the old profile while
    `config.source_lang()` already reported the new one.

    Only modules that are already imported are touched. Importing them here
    would invert the dependency and create a cycle, since each of them imports
    this module.
    """
    # This catches accidental cached-profile corruption before a rebuild, but not
    # in-place mutation that is never rebuilt; the threat model is model error
    # and accidental corruption, not a malicious local editor.
    validate_profile(profile())

    for name, mod in list(sys.modules.items()):
        if not name.startswith('hanpatch.') or mod is None:
            continue
        fn = getattr(mod, 'reset', None)
        if callable(fn) and getattr(mod, '__name__', '') != __name__:
            fn()


def cfg():
    global _cfg
    if _cfg is None:
        p = os.path.join(root(), PROJECT_FILE)
        _cfg = load_object(p, 'the project file') if os.path.exists(p) else {}
        _cfg.setdefault('target', 'ko')
        _cfg.setdefault('platform', 'threeds')
        _cfg.setdefault('title', os.path.basename(root()))
    return _cfg


def p(*parts):
    """Absolute path inside the project."""
    return os.path.join(root(), *parts)


def target():
    return cfg()['target']


def allow_game_data():
    """Whether this project intends to track game data / keys in git.

    hanpatch never inspects a repository itself; this only tells the bundled
    pre-commit hook to stand down. The decision is the operator's.
    """
    import os as _os
    if _os.environ.get('HANPATCH_ALLOW_GAME_DATA') == '1':
        return True
    return bool(cfg().get('allow_game_data'))


def work(*parts):
    return p('work', *parts)


def out(*parts):
    """Working directory for the target language."""
    d = p('work', target())
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, *parts) if parts else d


def built_name():
    """Name of the built artifact, carrying the SOURCE container's extension.

    A cartridge project used to produce `<title> (ko).cia` - an NCSD image with a
    CIA extension - so the operator's first hardware action was a CIA installer
    that rejects it for reasons that say nothing about the real cause. `release`
    already derived the extension for the recipient's file; the author's file has
    to agree with it.
    """
    c = cfg()
    ext = os.path.splitext(c.get('rom', 'game.cia'))[1] or '.cia'
    return f"{c['title']} ({target()}){ext}"


def src_path():
    return work('text_src.json')


def extracted(*parts):
    return p('extracted', *parts)


def dist(*parts):
    d = p('dist')
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, *parts) if parts else d


# ---------------------------------------------------------------- title profile

DEFAULT_PROFILE = {
    # markup grammar of the title's message container
    'tag_pattern': r'<[^>\n]*>',
    'hard_break': ['<br>'],
    'page_break': ['<page>'],
    'movable_tags': [],
    'control_tags': [],
    # Tokens the SOURCE carries that must not appear in the translation - reading aids and
    # other annotations meaningful only in the source language. Absent means the title has
    # none; it never means "match anything".
    'source_only_pattern': '',
    # Delimiters that are literal source content for this container, not markup. An empty
    # list is the safe default; a title must measure and declare an exception explicitly.
    'literal_delimiters': [],
    # Korean display name for the script book. Declared, because transliterating a latin
    # title in code invents a rendering the project never decided.
    'book_title_ko': '',
    # glossary scoping
    # Optional per-title model pool. Absent means the registry decides by role.
    'models': [],
    'name_keys': [],
    'terms': {},
    'ui_only_families': [],
    'ui_only_terms': [],
    'hard_families': [],
    # Terms whose Korean form is contractually fixed, declared explicitly.
    # Needed because promotion cannot be inferred from orthography: CJK is
    # caseless, so a kana/kanji term is never `isupper()`.
    'hard_terms': [],
    # Substitution tokens whose rendered text is FIXED - a party member whose
    # Korean name the player cannot change, e.g. {'{KEAFA}': '키파'}. This is the
    # only fact that lets a Korean particle after the token be resolved to one
    # form; every other substitution takes a value that is unknown until the
    # engine draws the line, and `josa` writes the both-forms particle there.
    # Deliberately separate from `placeholder_text`, which holds ONE example
    # rendering for the script book's search and legitimately names a hero the
    # player will rename. Empty means every substitution is treated as variable,
    # which is correct but reads worse - it is never treated as "guess".
    'substitution_values': {},
    # source language of the extracted rows. `en` keeps every Latin-source
    # heuristic exactly as it was; `ja` switches the ones that are wrong for a
    # spaceless script. Nothing else in the pipeline may branch on the title.
    'source_lang': 'en',
    # whether fullwidth Latin is a deliberate authoring device in this title
    # (Crimson Shroud used it for inner monologue) or ordinary content.
    # None means AUTO: true for a Latin source, false otherwise. Auto exists so
    # an existing project that never declared the key keeps its exact prompt.
    'fullwidth_is_content': None,
    # Whether the engine lays out a row that carries no line break of its
    # own. No default: guessing true silently disables the layout gate for
    # every unbroken row, which for a one-line-per-row container is the
    # whole title. Demanded by `wrap.engine_lays_out` where it decides.
    'engine_wraps': None,
    # tokeniser used to detect source text copied verbatim into the target
    'copied_spans_tokenizer': 'latin',
    # source script surviving in the target; None means AUTO: 'off' for `en`,
    # 'kana+kanji' otherwise. An explicit profile value overrides the auto rule.
    'residual_script_flag': None,
    # kanji legitimately retained in the target (in-game symbols, sigils)
    'kanji_allowlist': [],
    # per-title policy block injected into the judge prompt; empty means none
    'judge_policy': '',
    # minimum inputs a gate must examine, keyed by gate name. An ABSENT key
    # means NO FLOOR, so an empty mapping preserves today's behaviour.
    'gate_thresholds': {},
    # layout. `budget` is deliberately EMPTY: a budget is the widest page the
    # title actually renders, so shipping a generic default here would let the
    # capacity gate pass against a width nobody measured. There is no fallback
    # and no per-language carve-out anywhere - `wrap.budget_for()` fails closed
    # for every title that has not declared its own measured width, and the
    # reference title's 384 lives in its own profile because that is where a
    # measurement belongs.
    'budget': {},
    'capacity': {},
    # fonts used for measurement (relative to the project)
    'font_src': [],
    'font_out': [],
    # prompt/style
    'style': '',
    'register': {},
    # families that must never ship translated (debug rows, placeholders)
    'skip_families': [],
    'skip_key_patterns': [],
    'skip_value_patterns': [],
}


def validate_profile(data):
    """Reject a profile schema this build cannot honour."""
    # Validated before any module caches the profile: an unsupported mode would
    # otherwise look configured and silently gate nothing until a row reaches
    # the validator.
    mode = data['residual_script_flag']
    if mode not in _RESIDUAL_MODES:
        raise SystemExit(
            f'unsupported residual_script_flag {mode!r}; '
            f'accepted values: {", ".join(_RESIDUAL_MODES)}')
    # An uncompilable grammar otherwise surfaces only when a layout or wording
    # module happens to be imported. A wrongly shaped budget is checked here for
    # the same reason, but note the division of labour: SHAPE and positivity are
    # a property of the document and belong here, while the demand for a MEASURED
    # width belongs at the point that consumes one (`wrap.budget_for`), because
    # resolving a profile happens on paths that legitimately precede measurement.
    try:
        re.compile(data['tag_pattern'])
    except (re.error, TypeError) as exc:
        raise SystemExit(f'tag_pattern does not compile: {exc}')
    for key in ('budget', 'capacity'):
        value = data[key]
        if not isinstance(value, dict):
            raise SystemExit(
                f'{key} must be an object keyed by layout group, got '
                f'{type(value).__name__}')
    # NOTE: a missing `budget.default` is deliberately NOT rejected here.
    # Resolving a profile happens on any import path - `hanpatch info`, `keys`,
    # `release inspect` - and a title that has not measured its widths yet must
    # still be able to run those. The demand for a MEASURED width lives at the
    # single point that consumes one, `wrap.budget_for()`, which fails closed
    # there with no generic fallback and no per-language carve-out.
    for _key, _unit in (('budget', 'positive pixel widths'),
                        ('capacity', 'positive line counts')):
        bad = {k: v for k, v in data.get(_key, {}).items()
               if not isinstance(v, int) or isinstance(v, bool) or v <= 0}
        if bad:
            raise SystemExit(f'{_key} values must be {_unit}: {bad}')
    for key in _OBJECT_KEYS:
        if key in data and not isinstance(data[key], dict):
            raise SystemExit(f'{key} must be a JSON object, got '
                             f'{type(data[key]).__name__}')
    # A fixed rendering for a token the extractor never produces is a typo that
    # silently resolves nothing: the josa pass would keep writing the both-forms
    # particle after the real token and the declaration would look honoured.
    unknown = sorted(set(data.get('substitution_values', ()))
                     - set(data.get('movable_tags', ())))
    if unknown:
        raise SystemExit(f'substitution_values names tokens that are not declared '
                         f'movable_tags: {unknown}')
    for key in _LIST_KEYS:
        if key in data and not isinstance(data[key], list):
            raise SystemExit(f'{key} must be a JSON list, got '
                             f'{type(data[key]).__name__}')
    for key, allowed in _MODE_KEYS.items():
        # A mode this build does not implement must be refused here, not in the
        # middle of a gate: the tokeniser raises deep inside `translate.check`,
        # which reaches the operator as a traceback rather than a repair.
        if key in data and data[key] not in allowed:
            raise SystemExit(f'unsupported {key} {data[key]!r}; accepted values: '
                             f'{", ".join(allowed)}')
    for key in _BOOL_KEYS:
        if key in data and data[key] is not None and not isinstance(data[key], bool):
            raise SystemExit(f'{key} must be true, false or absent, got '
                             f'{type(data[key]).__name__}')
    for key in _STRING_KEYS:
        if key in data and not isinstance(data[key], str):
            raise SystemExit(f'{key} must be a string, got '
                             f'{type(data[key]).__name__}')


def profile():
    global _profile
    if _profile is None:
        rel = cfg().get('profile')
        data = {}
        if rel:
            for cand in (p(rel), os.path.join(REPO, rel)):
                if os.path.exists(cand):
                    data = load_object(cand, 'the title profile')
                    break
            else:
                raise SystemExit(f'profile not found: {rel}')
        merged = dict(DEFAULT_PROFILE)
        merged.update(data)
        _profile = merged
        src = source_lang()
        if merged['residual_script_flag'] is None:
            merged['residual_script_flag'] = (
                'off' if src == 'en' else 'kana+kanji')
    try:
        validate_profile(_profile)
    except SystemExit:
        _profile = None
        raise
    return _profile

def source_lang():
    """Validated language of the extracted source text."""
    value = (_profile if _profile is not None else profile()).get('source_lang')
    if value not in _SOURCE_LANGS:
        raise SystemExit(
            f'unsupported source_lang {value!r}; accepted values: {", ".join(_SOURCE_LANGS)}')
    return value


def prof(key, default=None):
    return profile().get(key, DEFAULT_PROFILE.get(key, default))


def tag_re():
    """Every engine token the source may contain, including source-only annotations.

    `source_only_pattern` is folded in here rather than left to each caller, because a
    token the recogniser does not match is not "text" - it is an unrecognised delimiter,
    and the acceptance check then reports the SOURCE as malformed. Measured on DQ7: 187135
    furigana annotations of the form {N<kana>} made 56824 of 66208 records fail the
    delimiter check, which is the check being wrong about the cartridge rather than the
    cartridge being wrong.
    """
    pat = prof('tag_pattern')
    extra = prof('source_only_pattern')
    return re.compile(f'{pat}|{extra}' if extra else pat)


def source_only_re():
    """Tokens that exist only in the source and must NOT survive translation.

    None when the title declares none: an absent declaration means "this title has no
    such tokens", not "check nothing", so callers get a null and skip the check rather
    than silently matching everything.
    """
    pat = prof('source_only_pattern')
    return re.compile(pat) if pat else None


def title():
    return cfg()['title']


def lang_name():
    return {'ko': '한국어', 'ja': '日本語', 'en': 'English'}.get(target(), target())


def describe():
    c = cfg()
    return (f"project  {root()}\n"
            f"title    {c['title']}\n"
            f"platform {c['platform']}\n"
            f"adapter  {c.get('adapter', '-')}\n"
            f"profile  {c.get('profile', '-')}\n"
            f"target   {c['target']}")
