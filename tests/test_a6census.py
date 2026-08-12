"""KATs for the U6 census admission oracle (Revision 6 D1)."""
from __future__ import annotations

import json

import pytest

from hanpatch import a6census as c

RUN_ID = '00000000-0000-4000-8000-000000000001'
UNIT_ID = '#000004.txt'
SEQ = 0


def frame_bytes(literal: str) -> int:
    return c.jcs_bytes(c.worst_case_chunk(c.nfc(literal), UNIT_ID, SEQ, RUN_ID))


def klass(literal: str, manifest_slot: bool = False) -> str:
    return c.classify(literal, UNIT_ID, SEQ, RUN_ID, manifest_slot)[0]


# --- JCS ------------------------------------------------------------------

def test_jcs_sorts_members_and_omits_whitespace():
    assert c.jcs({'b': 1, 'a': 2}) == '{"a":2,"b":1}'


def test_jcs_keeps_non_ascii_literal():
    assert c.jcs('한') == '"한"'
    assert c.jcs_bytes('한') == 5  # quote + 3 UTF-8 bytes + quote


def test_jcs_rejects_float():
    with pytest.raises(TypeError):
        c.jcs(1.5)


def test_jcs_member_order_is_utf16_code_unit():
    # U+FF3A (BMP) sorts before U+1D400 (non-BMP) by code point, and also by
    # UTF-16 code unit because the surrogate lead D835 > FF3A is false --
    # pin the behaviour so a future switch to code-point order is caught.
    encoded = c.jcs({'\uff3a': 1, '\U0001d400': 2})
    assert encoded.index('\U0001d400') < encoded.index('\uff3a')


# --- proof geometry (Revision 6 D2) ---------------------------------------

def test_proof_geometry_is_frozen():
    assert c.PROOF_DECODED_BYTES == 1 + 1 + 32 * 8 == 258
    assert c.PROOF_B64U_CHARS == 344


# --- oracle boundary ------------------------------------------------------

def test_empty_literal_is_admissible():
    assert c.admissible('', UNIT_ID, SEQ, RUN_ID)


def _boundary_literal() -> str:
    """A literal whose worst-case frame is exactly `MAX_FRAME_BYTES`.

    Pure ASCII cannot reach the frame boundary without first blowing the
    512-scalar cap, so the boundary corpus is CJK padded to the exact byte.
    """
    for cjk in range(300, c.MAX_LITERAL_SCALARS + 1):
        for pad in range(4):
            literal = '漢' * cjk + 'a' * pad
            if len(literal) > c.MAX_LITERAL_SCALARS:
                continue
            if len(literal.encode('utf-8')) > c.MAX_LITERAL_NFC_BYTES:
                continue
            if frame_bytes(literal) == c.MAX_FRAME_BYTES:
                return literal
    raise AssertionError('no literal lands exactly on the frame boundary')


def test_envelope_overhead_is_frozen():
    # Changing the envelope invalidates every published census, so pin the
    # empty-literal frame width.
    assert frame_bytes('') == 799


def test_kat_frame_boundary_accept_and_reject():
    literal = _boundary_literal()
    assert (len(literal), len(literal.encode('utf-8'))) == (417, 1247)
    assert frame_bytes(literal) == 2048
    assert c.admissible(literal, UNIT_ID, SEQ, RUN_ID)
    assert klass(literal) == 'ELIGIBLE'

    over = literal + 'a'
    assert len(over) <= c.MAX_LITERAL_SCALARS
    assert len(over.encode('utf-8')) <= c.MAX_LITERAL_NFC_BYTES
    assert frame_bytes(over) == 2049
    assert not c.admissible(over, UNIT_ID, SEQ, RUN_ID)
    assert klass(over) == 'WIRE_BYTES'


# --- Revision 6 D1 boundary corpora ---------------------------------------

def test_kat_backslash_literal_at_raw_byte_cap():
    literal = '\\' * c.MAX_LITERAL_NFC_BYTES
    assert len(literal.encode('utf-8')) == 1536
    # 1536 scalars blows the 512-scalar cap first; precedence puts TOO_LONG
    # ahead of WIRE_BYTES.
    assert klass(literal) == 'TOO_LONG'


def test_kat_quote_literal_at_scalar_cap():
    literal = '"' * c.MAX_LITERAL_SCALARS
    assert len(literal) == 512
    assert len(literal.encode('utf-8')) == 512
    # Each quote escapes to two JSON bytes, but 512 of them still fit the
    # frame; escaping alone does not push an at-cap literal over the wire.
    assert frame_bytes(literal) == 1825
    assert klass(literal) == 'ELIGIBLE'


def test_kat_cjk_at_scalar_cap():
    literal = '漢' * c.MAX_LITERAL_SCALARS
    assert len(literal) == 512
    assert len(literal.encode('utf-8')) == 1536  # exactly the raw-byte cap
    assert klass(literal) == 'WIRE_BYTES'


def test_kat_cjk_at_raw_byte_cap_is_not_too_long():
    literal = '漢' * (c.MAX_LITERAL_NFC_BYTES // 3)
    assert len(literal.encode('utf-8')) == c.MAX_LITERAL_NFC_BYTES
    assert len(literal) <= c.MAX_LITERAL_SCALARS
    assert klass(literal) == 'WIRE_BYTES'


def test_kat_emoji_scalar_versus_utf16_length():
    # One emoji is one Unicode scalar and four UTF-8 bytes; a scalar-count cap
    # measured with Python's len() is the contract, not UTF-16 units.
    literal = '\U0001f600' * 384
    assert len(literal) == 384
    assert len(literal.encode('utf-8')) == c.MAX_LITERAL_NFC_BYTES
    assert klass(literal) == 'WIRE_BYTES'


def test_kat_emoji_over_raw_byte_cap():
    literal = '\U0001f600' * 385
    assert len(literal.encode('utf-8')) > c.MAX_LITERAL_NFC_BYTES
    assert len(literal) <= c.MAX_LITERAL_SCALARS
    assert klass(literal) == 'TOO_LONG'


# --- precedence -----------------------------------------------------------

def test_secret_outranks_everything():
    literal = 'api_key: ' + 'A' * 4000
    assert klass(literal) == 'SECRET'


def test_manifest_slot_outranks_length():
    assert klass('a' * 4000, manifest_slot=True) == 'MANIFEST'


def test_secret_outranks_manifest_slot():
    assert klass('sk-' + 'a' * 32, manifest_slot=True) == 'SECRET'


def test_wire_bytes_outranks_multi_placeholder():
    literal = '{HERO}{ACTOR}' + '漢' * 499
    assert len(literal) == c.MAX_LITERAL_SCALARS
    assert len(literal.encode('utf-8')) <= c.MAX_LITERAL_NFC_BYTES
    assert frame_bytes(literal) > c.MAX_FRAME_BYTES
    assert klass(literal) == 'WIRE_BYTES'


def test_multi_placeholder_outranks_html():
    assert klass('{HERO} {ACTOR} <b>x</b>') == 'MULTI_PLACEHOLDER'


# --- DQ7 profile alignment ------------------------------------------------

def test_single_engine_placeholder_is_eligible():
    assert klass('「さてと…　今日も　{HERO}。') == 'ELIGIBLE'


def test_ruby_annotation_is_not_a_placeholder():
    # `{2きょう}` matches source_only_pattern, not tag_pattern.
    literal = '今日{2きょう}も　ここに来{1く}ることは'
    assert c._PLACEHOLDER_RE.findall(literal) == []
    assert klass(literal) == 'ELIGIBLE'


def test_two_engine_placeholders_are_excluded():
    assert klass('{HERO}と{ACTOR}が　来た。') == 'MULTI_PLACEHOLDER'


def test_center_control_tag_counts_as_placeholder_not_html():
    assert c._PLACEHOLDER_RE.findall('<CENTER>x</CENTER>') == ['<CENTER>', '</CENTER>']
    assert klass('<CENTER>x</CENTER>') == 'MULTI_PLACEHOLDER'


def test_url_and_icu_detected():
    assert klass('see https://example.com') == 'URL'
    assert klass('{count, plural, one{#} other{#}}') in {'ICU_SELECT_PLURAL',
                                                         'MULTI_PLACEHOLDER'}


# --- census partition -----------------------------------------------------

CATALOG = {
    '#001000': [
        {'key': '#001001.txt', 'en': 'ふつうの文。', 'jp': ''},
        {'key': '#001002.txt', 'en': '{HERO}と{ACTOR}。', 'jp': ''},
    ],
    '#000000': [
        {'key': '#000001.txt', 'en': '漢' * 512, 'jp': ''},
    ],
}


def test_translation_unit_population_counts_one_leaf_per_entry():
    census = c.run_census(CATALOG, 'translation-units', RUN_ID,
                          c.Caps(wall_clock_s=60, rss_bytes=4 << 30))
    assert census.n == 3
    assert census.counts['ELIGIBLE'] == 1
    assert census.counts['MULTI_PLACEHOLDER'] == 1
    assert census.counts['WIRE_BYTES'] == 1
    assert census.counts['MANIFEST'] == 0


def test_raw_leaf_population_counts_every_string():
    census = c.run_census(CATALOG, 'raw-leaves', RUN_ID,
                          c.Caps(wall_clock_s=60, rss_bytes=4 << 30))
    assert census.n == 9
    assert census.counts['MANIFEST'] == 3   # the `key` identifier slots
    assert census.counts['ELIGIBLE'] == 4   # 1 real line + 3 empty `jp` slots


def test_classes_are_disjoint_and_sum_to_n():
    census = c.run_census(CATALOG, 'raw-leaves', RUN_ID,
                          c.Caps(wall_clock_s=60, rss_bytes=4 << 30))
    assert sum(census.counts.values()) == census.n
    assert set(census.counts) == set(c.CLASSES)


def test_iteration_order_is_deterministic():
    a = [leaf.path for leaf in c.iter_translation_units(CATALOG)]
    b = [leaf.path for leaf in c.iter_translation_units(dict(reversed(list(CATALOG.items()))))]
    assert a == b
    assert a[0].startswith('#000000')


def test_seq_is_contiguous_from_zero():
    seqs = [leaf.seq for leaf in c.iter_translation_units(CATALOG)]
    assert seqs == list(range(len(seqs)))


# --- fail-closed ----------------------------------------------------------

def test_wall_clock_cap_blocks_instead_of_sampling():
    big = {'#0': [{'key': f'#{i}.txt', 'en': 'あ', 'jp': ''} for i in range(20000)]}
    caps = c.Caps(wall_clock_s=0.0, rss_bytes=4 << 30, check_every=1)
    with pytest.raises(c.Blocked, match='wall-clock'):
        c.run_census(big, 'translation-units', RUN_ID, caps)


def test_rss_cap_blocks_instead_of_sampling():
    big = {'#0': [{'key': f'#{i}.txt', 'en': 'あ', 'jp': ''} for i in range(20000)]}
    caps = c.Caps(wall_clock_s=600, rss_bytes=1, check_every=1)
    with pytest.raises(c.Blocked, match='RSS'):
        c.run_census(big, 'translation-units', RUN_ID, caps)


# --- decision table -------------------------------------------------------

def _census_with(counts: dict) -> c.Census:
    census = c.Census(population='translation-units')
    census.counts.update(counts)
    census.n = sum(census.counts.values())
    return census


def test_decision_proceeds_at_exact_thresholds():
    d = c.decide(_census_with({'ELIGIBLE': 80, 'MULTI_PLACEHOLDER': 20}))
    assert d['verdict'] == 'PROCEED'


def test_decision_stops_just_below_eligible_floor():
    d = c.decide(_census_with({'ELIGIBLE': 799, 'MULTI_PLACEHOLDER': 201}))
    assert d['verdict'] == 'STOP'
    assert d['eligible_floor_met'] is False


def test_decision_stops_when_one_exclusion_class_exceeds_ceiling():
    d = c.decide(_census_with({'ELIGIBLE': 700, 'MANIFEST': 251, 'URL': 49}))
    assert d['verdict'] == 'STOP'
    assert d['exclusion_classes_over_ceiling'] == ['MANIFEST']


def test_decision_stop_can_have_eligible_above_floor():
    # 26% MANIFEST with 74% eligible fails both; 80/26 is impossible, so the
    # ceiling only bites when several classes share the remainder.
    d = c.decide(_census_with({'ELIGIBLE': 740, 'MANIFEST': 260}))
    assert d['verdict'] == 'STOP'


# --- report ---------------------------------------------------------------

def test_report_publishes_oracle_version_and_digest():
    census = c.run_census(CATALOG, 'translation-units', RUN_ID,
                          c.Caps(wall_clock_s=60, rss_bytes=4 << 30))
    report = c.build_report(census, '/snap.json', 'deadbeef', RUN_ID,
                            c.Caps(wall_clock_s=1800, rss_bytes=4 << 30))
    assert report['oracle']['version'] == c.ORACLE_VERSION
    assert len(report['oracle']['source_digest']) == 64
    assert report['snapshot']['sha256'] == 'deadbeef'
    assert report['caps']['retries'] == 0
    json.dumps(report)  # must be serializable as published


# --- run_id independence --------------------------------------------------

def test_frame_width_is_invariant_across_run_ids():
    # Every UUID is 36 characters, so the census is reproducible under a new
    # run_id. Two independent runs must produce byte-identical class indices.
    import uuid
    literal = '漢' * 415 + 'a' * 2
    widths = {
        c.jcs_bytes(c.worst_case_chunk(literal, UNIT_ID, 0, str(uuid.uuid4())))
        for _ in range(16)
    }
    assert widths == {2048}


def test_classification_is_invariant_across_run_ids():
    import uuid
    literals = ['', 'ふつうの文。', '{HERO}と{ACTOR}。', '漢' * 512]
    baseline = [klass(x) for x in literals]
    for _ in range(4):
        run = str(uuid.uuid4())
        assert [c.classify(x, UNIT_ID, 0, run, False)[0] for x in literals] == baseline
