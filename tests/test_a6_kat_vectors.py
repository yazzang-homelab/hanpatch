"""Pin the frozen KAT vector file against the live implementation.

If this fails, the wire contract moved. Every published U6 census and every
recorded proof derived from the old contract is void; regenerate with
`python3 tools/a6_kat_vectors.py --out tests/vectors/a6dq7-kat-v1.json`, bump
`ORACLE_VERSION`, and re-run the census.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from hanpatch import a6batch as batch
from hanpatch import a6census as census

VECTORS_PATH = pathlib.Path(__file__).parent / 'vectors' / 'a6dq7-kat-v1.json'
VECTORS = json.loads(VECTORS_PATH.read_text(encoding='utf-8'))
RUN_ID = VECTORS['run_id']


def corpus(n: int) -> list[dict]:
    return [
        batch.leaf_from_source(RUN_ID, f'#{i:06d}.txt', i, f'ふつうの文 {i}。')
        for i in range(n)
    ]


def test_vectors_declare_the_current_oracle_version():
    assert VECTORS['oracle']['version'] == census.ORACLE_VERSION


@pytest.mark.parametrize('case', VECTORS['d1_wire_bytes'],
                         ids=lambda c: c['name'])
def test_d1_wire_byte_vectors(case):
    literals = {
        'empty': '',
        'boundary_2048': '漢' * 415 + 'a' * 2,
        'boundary_2049': '漢' * 415 + 'a' * 3,
        'quote_at_scalar_cap': '"' * census.MAX_LITERAL_SCALARS,
        'cjk_at_scalar_cap': '漢' * census.MAX_LITERAL_SCALARS,
        'backslash_at_raw_byte_cap': '\\' * census.MAX_LITERAL_NFC_BYTES,
        'emoji_at_raw_byte_cap': '\U0001f600' * 384,
        'emoji_over_raw_byte_cap': '\U0001f600' * 385,
    }
    text = census.nfc(literals[case['name']])
    assert len(text) == case['scalars']
    assert len(text.encode('utf-8')) == case['nfc_utf8_bytes']
    frame = census.worst_case_chunk(text, '#000004.txt', 0, RUN_ID)
    assert census.jcs_bytes(frame) == case['frame_jcs_bytes']
    assert census.admissible(text, '#000004.txt', 0, RUN_ID) == case['admissible']
    assert census.classify(text, '#000004.txt', 0, RUN_ID, False)[0] == case['class']


@pytest.mark.parametrize('case', VECTORS['d2_merkle'], ids=lambda c: f"n{c['n']}")
def test_d2_merkle_vectors(case):
    leaves = corpus(case['n'])
    levels = batch.build_levels(leaves)

    assert [len(level) for level in levels] == case['level_sizes']
    assert [[node.hex() for node in level] for level in levels] == case['levels']
    assert levels[-1][0].hex() == case['batch_root']
    assert len(levels) - 1 == case['depth']
    assert [census.jcs(leaf) for leaf in leaves] == case['leaf_jcs']
    assert [census.jcs_bytes(leaf) for leaf in leaves] == case['leaf_jcs_bytes']

    root = levels[-1][0]
    for proof_case in case['proofs']:
        index = proof_case['index']
        proof = batch.build_proof(leaves, index)
        assert proof == proof_case['proof_b64u']
        assert batch.verify_proof(leaves[index], proof, root) is True
        assert proof_case['verifies'] is True


def test_every_frozen_size_is_covered():
    assert [case['n'] for case in VECTORS['d2_merkle']] == [1, 2, 3, 5, 100, 200]
    depths = {case['n']: case['depth'] for case in VECTORS['d2_merkle']}
    assert depths == {1: 0, 2: 1, 3: 2, 5: 3, 100: 7, 200: 8}
