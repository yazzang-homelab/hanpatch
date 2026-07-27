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

PROJECT_FILE = 'hanpatch.json'
_root = None
_cfg = None
_profile = None

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
    """Point the whole pipeline at another project (used by tests)."""
    global _root, _cfg, _profile
    _root = os.path.abspath(path)
    _cfg = None
    _profile = None


def cfg():
    global _cfg
    if _cfg is None:
        p = os.path.join(root(), PROJECT_FILE)
        _cfg = json.load(open(p)) if os.path.exists(p) else {}
        _cfg.setdefault('target', 'ko')
        _cfg.setdefault('platform', 'threeds')
        _cfg.setdefault('title', os.path.basename(root()))
    return _cfg


def p(*parts):
    """Absolute path inside the project."""
    return os.path.join(root(), *parts)


def target():
    return cfg()['target']


def work(*parts):
    return p('work', *parts)


def out(*parts):
    """Working directory for the target language."""
    d = p('work', target())
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, *parts) if parts else d


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
    # glossary scoping
    'name_keys': [],
    'terms': {},
    'ui_only_families': [],
    'ui_only_terms': [],
    'hard_families': [],
    # layout
    'budget': {'default': 384},
    'capacity': {},
    'freeform_width': 420,
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


def profile():
    global _profile
    if _profile is None:
        rel = cfg().get('profile')
        data = {}
        if rel:
            for cand in (p(rel), os.path.join(REPO, rel)):
                if os.path.exists(cand):
                    data = json.load(open(cand))
                    break
            else:
                raise SystemExit(f'profile not found: {rel}')
        merged = dict(DEFAULT_PROFILE)
        merged.update(data)
        _profile = merged
    return _profile


def prof(key, default=None):
    return profile().get(key, DEFAULT_PROFILE.get(key, default))


def tag_re():
    return re.compile(prof('tag_pattern'))


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
