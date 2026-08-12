"""U6 full census for the A6/DQ7 isolated lane.

Revision 6 D1 requires that the census classifier and the grant-time installer
call the *same* admission oracle, so that census `ELIGIBLE` is by construction
protocol-admissible eligibility. `worst_case_chunk` / `admissible` in this
module are that shared oracle; the installer must import them from here rather
than reimplementing the frame.

Sampling, estimation and extrapolation are not implemented and must not be
added: exceeding a resource cap yields `BLOCKED`, never an approximation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import resource
import sys
import time
import unicodedata
import uuid
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Frozen protocol constants (stage-04-revision §1, Revision 6 D1/D2)
# ---------------------------------------------------------------------------

ORACLE_VERSION = 'A6-DQ7/wire-oracle/v1'

MAX_FRAME_BYTES = 2048          # INSTALL_CHUNK, serialized UTF-8, pre-parse
MAX_LITERAL_SCALARS = 512       # Unicode scalar values
MAX_LITERAL_NFC_BYTES = 1536    # raw NFC UTF-8 bytes
MAX_PROOF_DEPTH = 8             # n <= 200 leaves
MAX_CHUNK_INDEX = 24            # 25-chunk cap
PROOF_DECODED_BYTES = 1 + (MAX_PROOF_DEPTH + 7) // 8 + 32 * MAX_PROOF_DEPTH
PROOF_B64U_CHARS = (PROOF_DECODED_BYTES * 4 + 2) // 3

assert PROOF_DECODED_BYTES == 258, PROOF_DECODED_BYTES
assert PROOF_B64U_CHARS == 344, PROOF_B64U_CHARS

# Maximum-width stand-ins for the frozen envelope fields. Every one of these is
# a fixed-width encoding, so substituting a constant of the same width is
# byte-exact for length purposes and keeps the oracle packing-independent.
_MAX_UUID = 'ffffffff-ffff-ffff-ffff-ffffffffffff'
_MAX_PROOF = '_' * PROOF_B64U_CHARS

PROTOCOL = 'a6-dq7-install'
PROTOCOL_VERSION = 1


# ---------------------------------------------------------------------------
# JCS (RFC 8785) serialization, restricted to the types the wire format uses
# ---------------------------------------------------------------------------

def _utf16_sort_key(name: str) -> tuple[int, ...]:
    """RFC 8785 orders object members by UTF-16 code unit, not code point."""
    return tuple(name.encode('utf-16-be'))


def jcs(value) -> str:
    """Canonical JSON for str/int/bool/None/list/dict values.

    Floats are rejected: the wire format has no float field, and RFC 8785
    number canonicalization is a trap we do not need to walk into.
    """
    if value is None:
        return 'null'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    if isinstance(value, list):
        return '[' + ','.join(jcs(v) for v in value) + ']'
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda kv: _utf16_sort_key(kv[0]))
        return '{' + ','.join(
            f'{jcs(k)}:{jcs(v)}' for k, v in items
        ) + '}'
    raise TypeError(f'value of type {type(value).__name__} is not JCS-encodable here')


def jcs_bytes(value) -> int:
    return len(jcs(value).encode('utf-8'))


# ---------------------------------------------------------------------------
# The shared admission oracle
# ---------------------------------------------------------------------------

def nfc(text: str) -> str:
    return unicodedata.normalize('NFC', text)


def literal_digest(literal_nfc: str) -> str:
    return hashlib.sha256(literal_nfc.encode('utf-8')).hexdigest()


def worst_case_chunk(literal_nfc: str, unit_id: str, seq: int, run_id: str) -> dict:
    """A single-leaf `INSTALL_CHUNK` at its maximum encoded width.

    Deterministic and packing-independent: real `run_id`/`unit_id`/`seq`/
    `literal_digest`/`codepoints` from the snapshot, a maximum proof
    (depth 8, 258 decoded bytes, 344 base64url characters), and every other
    frozen envelope field at its widest encoding under the 25-chunk cap.
    """
    return {
        'boot_epoch': _MAX_UUID,
        'chunk_index': MAX_CHUNK_INDEX,
        'grant_id': _MAX_UUID,
        'leaves': [
            {
                'codepoints': len(literal_nfc),
                'literal': literal_nfc,
                'literal_digest': literal_digest(literal_nfc),
                'proof_b64u': _MAX_PROOF,
                'seq': seq,
                'unit_id': unit_id,
            }
        ],
        'protocol': PROTOCOL,
        'run_id': run_id,
        'type': 'INSTALL_CHUNK',
        'version': PROTOCOL_VERSION,
        'window_id': _MAX_UUID,
    }


def admissible(literal_nfc: str, unit_id: str, seq: int, run_id: str) -> bool:
    frame = worst_case_chunk(literal_nfc, unit_id, seq, run_id)
    return jcs_bytes(frame) <= MAX_FRAME_BYTES


def oracle_source_digest() -> str:
    with open(__file__, 'rb') as handle:
        return hashlib.sha256(handle.read()).hexdigest()


# ---------------------------------------------------------------------------
# Class predicates
# ---------------------------------------------------------------------------

CLASSES = (
    'SECRET',
    'MANIFEST',
    'TOO_LONG',
    'WIRE_BYTES',
    'MULTI_PLACEHOLDER',
    'HTML',
    'URL',
    'ICU_SELECT_PLURAL',
    'ELIGIBLE',
)

_SECRET_RES = tuple(re.compile(p) for p in (
    r'-----BEGIN [A-Z ]*PRIVATE KEY-----',
    r'\bsk-[A-Za-z0-9]{16,}',
    r'\bAKIA[0-9A-Z]{16}\b',
    r'\bgh[pousr]_[A-Za-z0-9]{20,}',
    r'\bxox[baprs]-[A-Za-z0-9-]{10,}',
    r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}',
    r'(?i)\b(api[_-]?key|secret|password|passwd|token)\b\s*[:=]\s*\S{8,}',
))

# DQ7 engine placeholders: profiles/dq7.json `tag_pattern`.
# Ruby/furigana `{1かな}` matches `source_only_pattern` instead and is NOT a
# placeholder — it is stripped source-side and never reaches the relay.
_PLACEHOLDER_RE = re.compile(r'<[^>\n]*>|\{[A-Z0-9_]+\}')

_HTML_RE = re.compile(
    r'(?i)</?(?:a|b|i|u|s|br|hr|p|div|span|em|strong|font|img|table|tbody|'
    r'thead|tr|td|th|ul|ol|li|h[1-6]|code|pre|small|sub|sup)\b[^>]*>'
)
_URL_RE = re.compile(r'(?i)\b(?:https?://|ftp://|www\.[a-z0-9-]+\.)')
_ICU_RE = re.compile(r'\{\s*[A-Za-z_][A-Za-z0-9_]*\s*,\s*(?:select|plural|selectordinal)\s*,')


def classify(literal_raw: str, unit_id: str, seq: int, run_id: str,
             is_manifest_slot: bool) -> tuple[str, str]:
    """Return `(class, literal_digest)` under the frozen disjoint precedence."""
    text = nfc(literal_raw)
    digest = literal_digest(text)

    for pattern in _SECRET_RES:
        if pattern.search(text):
            return 'SECRET', digest

    if is_manifest_slot:
        return 'MANIFEST', digest

    if len(text) > MAX_LITERAL_SCALARS or len(text.encode('utf-8')) > MAX_LITERAL_NFC_BYTES:
        return 'TOO_LONG', digest

    if not admissible(text, unit_id, seq, run_id):
        return 'WIRE_BYTES', digest

    if len(_PLACEHOLDER_RE.findall(text)) >= 2:
        return 'MULTI_PLACEHOLDER', digest

    if _HTML_RE.search(text):
        return 'HTML', digest

    if _URL_RE.search(text):
        return 'URL', digest

    if _ICU_RE.search(text):
        return 'ICU_SELECT_PLURAL', digest

    return 'ELIGIBLE', digest


# ---------------------------------------------------------------------------
# Deterministic snapshot iterator
# ---------------------------------------------------------------------------

# The DQ7 catalog schema is `{bank: [{key, en, jp}, ...]}` where `key` is the
# unit identifier (it becomes the frame's `unit_id`), `en` holds the Japanese
# source literal, and `jp` is the empty target slot.
_UNIT_ID_FIELD = 'key'
_SOURCE_FIELD = 'en'
_TARGET_FIELD = 'jp'


@dataclass
class Leaf:
    path: str
    unit_id: str
    seq: int
    text: str
    manifest_slot: bool


def iter_translation_units(catalog: dict):
    """U = one source literal per catalog entry. `key` is metadata, not a leaf."""
    seq = 0
    for bank in sorted(catalog):
        for index, entry in enumerate(catalog[bank]):
            unit_id = entry[_UNIT_ID_FIELD]
            yield Leaf(
                path=f'{bank}[{index}].{_SOURCE_FIELD}',
                unit_id=unit_id,
                seq=seq,
                text=entry[_SOURCE_FIELD],
                manifest_slot=False,
            )
            seq += 1


def iter_raw_leaves(catalog: dict):
    """U = every string leaf of the snapshot, verbatim Revision-6 wording."""
    seq = 0
    for bank in sorted(catalog):
        for index, entry in enumerate(catalog[bank]):
            unit_id = entry[_UNIT_ID_FIELD]
            for field_name in sorted(entry):
                value = entry[field_name]
                if not isinstance(value, str):
                    continue
                yield Leaf(
                    path=f'{bank}[{index}].{field_name}',
                    unit_id=unit_id,
                    seq=seq,
                    text=value,
                    manifest_slot=(field_name == _UNIT_ID_FIELD),
                )
                seq += 1


POPULATIONS = {
    'translation-units': iter_translation_units,
    'raw-leaves': iter_raw_leaves,
}


# ---------------------------------------------------------------------------
# Census runner
# ---------------------------------------------------------------------------

@dataclass
class Caps:
    wall_clock_s: float
    rss_bytes: int
    check_every: int = 4096


class Blocked(RuntimeError):
    """A resource cap was hit. Sampling is not an option; the census fails closed."""


@dataclass
class Census:
    population: str
    counts: dict = field(default_factory=lambda: {c: 0 for c in CLASSES})
    non_string: int = 0
    n: int = 0
    elapsed_s: float = 0.0
    peak_rss_bytes: int = 0


def _rss_bytes() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def run_census(catalog: dict, population: str, run_id: str, caps: Caps,
               index_writer=None) -> Census:
    iterator = POPULATIONS[population]
    census = Census(population=population)
    started = time.monotonic()

    for count, leaf in enumerate(iterator(catalog), start=1):
        cls, digest = classify(leaf.text, leaf.unit_id, leaf.seq, run_id,
                               leaf.manifest_slot)
        census.counts[cls] += 1
        census.n += 1
        if index_writer is not None:
            index_writer.write(jcs({
                'class': cls,
                'literal_digest': digest,
                'path_digest': hashlib.sha256(leaf.path.encode('utf-8')).hexdigest(),
                'seq': leaf.seq,
            }) + '\n')

        if count % caps.check_every == 0:
            elapsed = time.monotonic() - started
            if elapsed > caps.wall_clock_s:
                raise Blocked(
                    f'wall-clock cap {caps.wall_clock_s}s exceeded at leaf {count}'
                )
            if _rss_bytes() > caps.rss_bytes:
                raise Blocked(
                    f'RSS cap {caps.rss_bytes} B exceeded at leaf {count}'
                )

    census.elapsed_s = time.monotonic() - started
    census.peak_rss_bytes = _rss_bytes()
    assert sum(census.counts.values()) == census.n
    return census


# ---------------------------------------------------------------------------
# Signed decision table
# ---------------------------------------------------------------------------

ELIGIBLE_FLOOR = 0.80
EXCLUSION_CEILING = 0.25


def decide(census: Census) -> dict:
    n = census.n
    rates = {c: (census.counts[c] / n if n else 0.0) for c in CLASSES}
    exclusions = {c: r for c, r in rates.items() if c != 'ELIGIBLE'}
    over = sorted(c for c, r in exclusions.items() if r > EXCLUSION_CEILING)
    eligible_ok = rates['ELIGIBLE'] >= ELIGIBLE_FLOOR
    return {
        'eligible_rate': rates['ELIGIBLE'],
        'eligible_floor_met': eligible_ok,
        'exclusion_ceiling': EXCLUSION_CEILING,
        'exclusion_classes_over_ceiling': over,
        'rates': rates,
        'verdict': 'PROCEED' if (eligible_ok and not over) else 'STOP',
    }


def build_report(census: Census, snapshot_path: str, snapshot_digest: str,
                 run_id: str, caps: Caps) -> dict:
    return {
        'schema': 'a6dq7.u6_census.v1',
        'oracle': {
            'version': ORACLE_VERSION,
            'source_digest': oracle_source_digest(),
            'max_frame_bytes': MAX_FRAME_BYTES,
            'max_literal_scalars': MAX_LITERAL_SCALARS,
            'max_literal_nfc_bytes': MAX_LITERAL_NFC_BYTES,
            'proof_decoded_bytes': PROOF_DECODED_BYTES,
            'proof_b64u_chars': PROOF_B64U_CHARS,
            'chunk_index': MAX_CHUNK_INDEX,
        },
        'snapshot': {'path': snapshot_path, 'sha256': snapshot_digest},
        'run_id': run_id,
        'population': census.population,
        'n': census.n,
        'counts': dict(census.counts),
        'caps': {
            'wall_clock_s': caps.wall_clock_s,
            'rss_bytes': caps.rss_bytes,
            'retries': 0,
        },
        'measured': {
            'elapsed_s': round(census.elapsed_s, 3),
            'peak_rss_bytes': census.peak_rss_bytes,
        },
        'decision': decide(census),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='U6 full census (no sampling)')
    parser.add_argument('snapshot')
    parser.add_argument('--population', choices=sorted(POPULATIONS),
                        default='translation-units')
    parser.add_argument('--wall-clock-s', type=float, default=1800.0)
    parser.add_argument('--rss-bytes', type=int, default=4 * 1024 ** 3)
    parser.add_argument('--run-id', default=None)
    parser.add_argument('--index-out', default=None)
    parser.add_argument('--report-out', default=None)
    args = parser.parse_args(argv)

    run_id = args.run_id or str(uuid.uuid4())
    caps = Caps(wall_clock_s=args.wall_clock_s, rss_bytes=args.rss_bytes)

    with open(args.snapshot, 'rb') as handle:
        raw = handle.read()
    snapshot_digest = hashlib.sha256(raw).hexdigest()
    catalog = json.loads(raw.decode('utf-8'))

    index_handle = open(args.index_out, 'w', encoding='utf-8') if args.index_out else None
    try:
        census = run_census(catalog, args.population, run_id, caps, index_handle)
    except Blocked as exc:
        report = {
            'schema': 'a6dq7.u6_census.v1',
            'population': args.population,
            'run_id': run_id,
            'snapshot': {'path': args.snapshot, 'sha256': snapshot_digest},
            'decision': {'verdict': 'BLOCKED', 'reason': str(exc)},
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2
    finally:
        if index_handle is not None:
            index_handle.close()

    report = build_report(census, os.path.abspath(args.snapshot), snapshot_digest,
                          run_id, caps)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report_out:
        with open(args.report_out, 'w', encoding='utf-8') as handle:
            handle.write(text + '\n')
    print(text)
    return 0 if report['decision']['verdict'] == 'PROCEED' else 1


if __name__ == '__main__':
    sys.exit(main())
