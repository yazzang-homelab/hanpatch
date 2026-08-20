"""Export the sealed text as host rows, with the language axes named.

`hancharacter` reads a family-to-rows document whose columns are `evidence`,
`pivot` and `target`. Those are roles, not languages: which actual language sits
in `evidence` depends on the project, and the reader resolves them by the
profile's declared languages.

An exporter that omitted that binding would look like it worked. It would emit
three populated columns, hancharacter would read three populated columns, and
the axes would be silently transposed - source text measured as if it were the
translation. Nothing downstream can detect that, because both sides agree on the
shape and disagree only about meaning.

So `LanguageMap` is required, not inferred. It names which language fills each
role, and export refuses when a declared role has no column to fill it from.

The exported document is a *view*. The sealed manifest stays the authority: the
export carries its digest and ruleset so a consumer can verify it is describing
the same seal rather than trusting the view.
"""

from __future__ import annotations

import json
import os
import tempfile

SCHEMA_VERSION = 1
DOCUMENT_KIND = 'hanpatch-host-rows'

#: Roles a host row carries. `evidence` and `pivot` are inputs to a judgement;
#: `target` is the text under judgement.
ROLES = ('evidence', 'pivot', 'target')


class InteropError(ValueError):
    """An export that would misrepresent which language is which."""


class LanguageMap:
    """Which language fills each host-row role.

    `pivot` is optional because not every project has one; `evidence` and
    `target` are not, because a judgement needs something to judge and something
    to judge it against.
    """

    __slots__ = ('evidence', 'pivot', 'target')

    def __init__(self, evidence, target, pivot=None):
        for name, value in (('evidence', evidence), ('target', target)):
            if not value or not isinstance(value, str):
                raise InteropError(
                    '%s language must be a non-empty string; got %r. Inferring '
                    'it would silently transpose the axes.' % (name, value))
        if pivot is not None and (not pivot or not isinstance(pivot, str)):
            raise InteropError('pivot language must be a non-empty string or None')
        if evidence == target:
            raise InteropError(
                'evidence and target languages are both %r; a judgement that '
                'compares text against itself measures nothing' % evidence)
        self.evidence = evidence
        self.pivot = pivot
        self.target = target

    def as_dict(self):
        return {'evidence': self.evidence, 'pivot': self.pivot,
                'target': self.target}

    def direction(self):
        return '%s->%s' % (self.evidence, self.target)


def _split_key(key):
    """`family/rest` -> (family, rest). A key without a family is refused."""
    if not isinstance(key, str) or '/' not in key:
        raise InteropError('entry key %r is not family/key shaped' % (key,))
    family, _, rest = key.partition('/')
    if not family or not rest:
        raise InteropError('entry key %r has an empty family or key' % (key,))
    return family, rest


def export_host_rows(sealed_entries, languages, source_entries=None,
                     pivot_entries=None, scenes=None, manifest_digest=None,
                     manifest_ruleset=None):
    """Build the host-rows document.

    `sealed_entries` is the shipped text - the target column. `source_entries`
    supplies the evidence column and `pivot_entries` the optional pivot. A role
    the language map declares must have a column; otherwise the export would
    emit a row whose axis is a guess.
    """
    if not isinstance(languages, LanguageMap):
        raise InteropError(
            'languages must be a LanguageMap so each role names its language; '
            'got %s' % type(languages).__name__)
    if not isinstance(sealed_entries, dict):
        raise InteropError('sealed entries must be a mapping')

    if source_entries is None:
        raise InteropError(
            'the evidence column has no source; export would emit rows whose '
            'evidence axis is empty while claiming language %r'
            % languages.evidence)
    if languages.pivot is not None and pivot_entries is None:
        raise InteropError(
            'the language map declares pivot %r but no pivot column was supplied'
            % languages.pivot)
    if languages.pivot is None and pivot_entries:
        raise InteropError(
            'pivot rows were supplied but the language map declares no pivot '
            'language; the column would have no axis')

    scenes = scenes or {}
    families = {}
    for key in sorted(sealed_entries):
        family, rest = _split_key(key)
        target = sealed_entries[key]
        if not isinstance(target, str):
            raise InteropError('entry %r target must be a string; got %s'
                               % (key, type(target).__name__))
        evidence = source_entries.get(key)
        if evidence is None:
            raise InteropError(
                'entry %r has no evidence-side text; a row whose evidence '
                'column is empty cannot be judged' % key)
        row = {'key': rest, 'evidence': evidence, 'target': target}
        if languages.pivot is not None:
            pivot_value = (pivot_entries or {}).get(key)
            if pivot_value is None:
                raise InteropError('entry %r has no pivot text' % key)
            row['pivot'] = pivot_value
        scene = scenes.get(key)
        if scene is not None:
            row['scene'] = scene
        families.setdefault(family, []).append(row)

    return {
        'schemaVersion': SCHEMA_VERSION,
        'kind': DOCUMENT_KIND,
        'languages': languages.as_dict(),
        'direction': languages.direction(),
        'manifestDigest': manifest_digest,
        'manifestRuleset': manifest_ruleset,
        'families': families,
    }


def export_from_manifest(languages, source_entries, pivot_entries=None,
                         scenes=None, doc=None):
    """Export straight from the sealed manifest, carrying its digest forward."""
    from hanpatch import manifest
    doc = doc or manifest.load()
    return export_host_rows(
        doc['entries'], languages,
        source_entries=source_entries, pivot_entries=pivot_entries,
        scenes=scenes, manifest_digest=doc.get('digest'),
        manifest_ruleset=doc.get('ruleset'))


def write(document, path):
    """Atomic replace; a torn view still parses."""
    payload = json.dumps(document, indent=2, sort_keys=True,
                         ensure_ascii=False) + '\n'
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix='.host-rows-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path
