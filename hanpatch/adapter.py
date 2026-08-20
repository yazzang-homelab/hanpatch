"""Container adapter contract.

The translation and QA core never touches a ROM.  It reads and writes exactly
two normalised documents, and an adapter is the only code that knows how a
particular title stores its text:

    work/text_src.json      {family: [{key, en, jp, note?}, ...]}
    `jp` is REQUIRED-PRESENT and may be an empty string.
    work/<lang>/manifest.json
                            {"digest": hex, "entries": {family/key: text}}

An adapter implements three operations:

    extract()  ROM            -> work/text_src.json + extracted/
    inject()   manifest       -> patched ROM
    verify()   patched ROM    -> raises on any mismatch

Register one with `@register('name')` and select it in `hanpatch.json` via
`"adapter": "name"`.  Adapters live in `hanpatch/adapters/`; importing the
package imports every module in it, so registration is automatic.
"""
import abc
import importlib
import os
import pkgutil

_REG = {}


def register(name):
    def deco(cls):
        _REG[name] = cls
        cls.adapter_name = name
        return cls
    return deco


def _load_all():
    import hanpatch.adapters as pkg
    for m in pkgutil.iter_modules(pkg.__path__):
        if not m.name.startswith('_'):
            importlib.import_module(f'hanpatch.adapters.{m.name}')


def get(name):
    if name not in _REG:
        _load_all()
    if name not in _REG:
        raise SystemExit(f'unknown adapter {name!r}; available: {available()}')
    return _REG[name]()


def available():
    _load_all()
    return sorted(_REG)


class Adapter(abc.ABC):
    """Base class for a title's container handling."""

    #: platform this adapter targets, informational
    platform = 'generic'

    @abc.abstractmethod
    def extract(self, rom):
        """Unpack `rom` and write `work/text_src.json`.

        Must also leave whatever the later stages need (fonts to measure
        against, the untouched containers to rebuild from) under `extracted/`.
        Returns the number of source entries written.
        """

    @abc.abstractmethod
    def inject(self, entries, rom, out):
        """Write a patched ROM to `out`.

        `entries` maps `family/key` to translated text and is taken from the
        sealed manifest, never from the working translation memory.
        """

    @abc.abstractmethod
    def verify(self, rom, entries):
        """Re-read the built ROM and prove every entry survived.

        Must raise (or return a non-empty list of problems) if any string in
        `entries` is missing, truncated, or differs after a container
        round-trip.  Returns a list of problem strings; empty means clean.
        """

    # -- optional hooks -----------------------------------------------------

    def write_plan(self, rom, entries):
        """Declare every byte this adapter will write, or None to opt out.

        `verify` proves each sealed entry survived. It says nothing about bytes
        nobody declared, so an adapter that writes its text correctly and also
        clobbers a reserved field passes verification. Returning a
        `expected_write.WritePlan` here lets the pipeline check that too: the
        source held what the plan expected, no two writes overlap, nothing lands
        in a protected span, and the built artifact differs only inside declared
        regions.

        Default None. A title that has not declared its write surface is simply
        not checked this way - the pipeline records that it was not, rather than
        reporting a guarantee it never obtained.
        """
        return None

    def build_fonts(self):
        """Generate target-language fonts. Default: nothing to do."""
        return []

    def font_paths(self):
        """(source_fonts, built_fonts) used for width measurement."""
        return ([], [])

    def font_metrics(self, blob):
        """A width source for the layout gates, or None to use the 3DS reader.

        The layout core measures a line by asking a font for `char_to_index`,
        `width_of` and `def_cw`. It used to construct a 3DS BCFNT directly,
        which silently made every non-3DS title unmeasurable: the gate does not
        report 'wrong format', it reports that the source has no font. Returning
        None keeps the existing titles on that path; a platform whose font is
        not BCFNT returns its own reader here.
        """
        return None

    def font_coverage(self, paths):
        """The characters the BUILT fonts can render, or None for the 3DS path.

        The glyph authority is the font that ships, so this is read back off
        the built files rather than assumed from a Unicode range. A title whose
        font is not a BCFNT answers here; returning None keeps the existing
        titles on the reader they already use.
        """
        return None

    def recipe_facts(self):
        """Observed container facts, or None when this adapter has not been
        reduced to a recipe yet.

        The default keeps existing adapters working untouched: an adapter that
        has never been asked where its text lives says so, rather than having a
        plausible answer invented for it.
        """
        return None


def project_adapter():
    from hanpatch import config
    name = config.cfg().get('adapter')
    if not name:
        raise SystemExit('hanpatch.json has no "adapter"')
    return get(name)


def require(path, what):
    if not os.path.exists(path):
        raise SystemExit(f'missing {what}: {path}')
    return path
