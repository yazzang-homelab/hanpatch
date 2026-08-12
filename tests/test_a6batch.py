"""KATs for the frozen Merkle batch/proof grammar (Revision 6 D2)."""
from __future__ import annotations

import base64
import hashlib

import pytest

from hanpatch import a6batch as b
from hanpatch.a6census import jcs

RUN_ID = '00000000-0000-4000-8000-000000000001'


def corpus(n: int) -> list[dict]:
    return [
        b.leaf_from_source(RUN_ID, f'#{i:06d}.txt', i, f'ふつうの文 {i}。')
        for i in range(n)
    ]


# --- domain separation ----------------------------------------------------

def test_leaf_and_node_domains_are_frozen():
    assert b.LEAF_DOMAIN == b'A6-DQ7/batch-leaf/v2\x00'
    assert b.NODE_DOMAIN == b'A6-DQ7/batch-node/v2\x00'


def test_leaf_hash_is_domain_separated_jcs():
    leaf = corpus(1)[0]
    assert b.leaf_hash(leaf) == hashlib.sha256(
        b.LEAF_DOMAIN + jcs(leaf).encode('utf-8')
    ).digest()


def test_leaf_object_has_exactly_the_committed_fields():
    assert set(corpus(1)[0]) == {'codepoints', 'literal_digest', 'run_id', 'seq',
                                 'unit_id'}


def test_leaf_is_nfc_normalized():
    decomposed = b.leaf_from_source(RUN_ID, 'u', 0, 'が')       # U+304B U+3099
    composed = b.leaf_from_source(RUN_ID, 'u', 0, '\u304c')      # U+304C
    assert decomposed == composed


# --- tree shape -----------------------------------------------------------

def test_single_leaf_root_equals_leaf_hash_and_depth_zero():
    leaves = corpus(1)
    assert b.batch_root(leaves) == b.leaf_hash(leaves[0])
    assert b.batch_depth(leaves) == 0


@pytest.mark.parametrize('n,depth', [(1, 0), (2, 1), (3, 2), (5, 3), (100, 7),
                                     (200, 8)])
def test_depth_for_representative_sizes(n, depth):
    assert b.batch_depth(corpus(n)) == depth


def test_two_hundred_leaves_is_the_cap():
    assert b.batch_depth(corpus(b.MAX_LEAVES)) <= b.MAX_DEPTH
    with pytest.raises(b.BatchReject, match='200-leaf cap'):
        b.build_levels(corpus(b.MAX_LEAVES + 1))


def test_empty_batch_rejects():
    with pytest.raises(b.BatchReject):
        b.build_levels([])


def test_non_contiguous_seq_rejects():
    leaves = corpus(3)
    leaves[2]['seq'] = 9
    with pytest.raises(b.BatchReject, match='contiguous'):
        b.build_levels(leaves)


def test_duplicate_last_padding_is_per_level_only():
    # n=3: level 0 pads to 4 -> level 1 has 2 nodes (not 3), so level 1 is even
    # and must not pad again.
    levels = b.build_levels(corpus(3))
    assert [len(level) for level in levels] == [3, 2, 1]


def test_duplicate_last_padding_at_two_levels():
    # n=5: level 0 pads to 6 -> level 1 has 3 -> pads to 4 -> level 2 has 2.
    levels = b.build_levels(corpus(5))
    assert [len(level) for level in levels] == [5, 3, 2, 1]


def test_self_pairing_uses_the_node_as_its_own_sibling():
    leaves = corpus(3)
    levels = b.build_levels(leaves)
    # leaf 2 is the odd one out at level 0 and pairs with itself
    siblings, directions = b.decode_proof(b.build_proof(leaves, 2))
    assert siblings[0] == levels[0][2]
    assert directions[0] == 0


# --- positive KATs --------------------------------------------------------

@pytest.mark.parametrize('n,depth', [(1, 0), (2, 1), (3, 2), (5, 3), (100, 7),
                                     (200, 8)])
def test_positive_kat_every_leaf_proves_to_the_root(n, depth):
    leaves = corpus(n)
    root = b.batch_root(leaves)
    for index, leaf in enumerate(leaves):
        proof = b.build_proof(leaves, index)
        siblings, _ = b.decode_proof(proof)
        assert len(siblings) == depth
        assert b.verify_proof(leaf, proof, root)


def test_kat_depth_zero_decodes_to_one_byte():
    proof = b.build_proof(corpus(1), 0)
    assert len(base64.urlsafe_b64decode(proof + '==')) == 1
    assert b.decode_proof(proof) == ([], [])


def test_kat_depth_eight_decodes_to_258_bytes_and_344_chars():
    proof = b.build_proof(corpus(200), 0)
    assert len(proof) == 344
    remainder = len(proof) % 4
    blob = base64.urlsafe_b64decode(proof + '=' * ((4 - remainder) % 4))
    assert len(blob) == 258
    assert blob[0] == 8


def test_direction_bits_are_lsb_first():
    leaves = corpus(200)
    # leaf 1 is a right child at level 0, so its sibling is the left child:
    # direction bit 0 must be set, i.e. the low bit of the first byte.
    proof = b.build_proof(leaves, 1)
    blob = base64.urlsafe_b64decode(proof + '=' * ((4 - len(proof) % 4) % 4))
    assert blob[1] & 0x01 == 1
    _, directions = b.decode_proof(proof)
    assert directions[0] == 1


def test_proof_is_unpadded_base64url():
    proof = b.build_proof(corpus(200), 0)
    assert '=' not in proof
    assert set(proof) <= set(
        'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'
    )


# --- negative KATs: all must reject, zero relay bytes ---------------------

def _blob(proof: str) -> bytearray:
    return bytearray(base64.urlsafe_b64decode(proof + '=' * ((4 - len(proof) % 4) % 4)))


def _reencode(blob: bytes) -> str:
    return base64.urlsafe_b64encode(bytes(blob)).decode('ascii').rstrip('=')


def test_negative_wrong_depth_byte():
    leaves = corpus(200)
    blob = _blob(b.build_proof(leaves, 0))
    blob[0] = 7
    with pytest.raises(b.ProofReject, match='decoded length'):
        b.decode_proof(_reencode(blob))
    assert not b.verify_proof(leaves[0], _reencode(blob), b.batch_root(leaves))


def test_negative_depth_byte_over_max():
    blob = bytearray([9]) + bytearray(2 + 32 * 9)
    with pytest.raises(b.ProofReject, match='exceeds'):
        b.decode_proof(_reencode(blob))


def test_negative_nonzero_unused_trailing_bits():
    leaves = corpus(100)          # depth 7 -> one direction byte, 1 unused bit
    blob = _blob(b.build_proof(leaves, 0))
    blob[1] |= 0x80
    with pytest.raises(b.ProofReject, match='unused trailing'):
        b.decode_proof(_reencode(blob))
    assert not b.verify_proof(leaves[0], _reencode(blob), b.batch_root(leaves))


def test_negative_truncated_sibling():
    leaves = corpus(200)
    blob = _blob(b.build_proof(leaves, 0))[:-1]
    with pytest.raises(b.ProofReject, match='decoded length'):
        b.decode_proof(_reencode(blob))


def test_negative_extra_sibling():
    leaves = corpus(200)
    blob = _blob(b.build_proof(leaves, 0)) + bytearray(32)
    with pytest.raises(b.ProofReject, match='decoded length'):
        b.decode_proof(_reencode(blob))


def test_negative_padded_base64():
    leaves = corpus(200)
    proof = b.build_proof(leaves, 0)
    padded = proof + '=='
    with pytest.raises(b.ProofReject, match='outside unpadded base64url'):
        b.decode_proof(padded)
    assert not b.verify_proof(leaves[0], padded, b.batch_root(leaves))


def test_negative_root_to_leaf_sibling_order():
    leaves = corpus(200)
    root = b.batch_root(leaves)
    siblings, directions = b.decode_proof(b.build_proof(leaves, 0))
    reversed_proof = b.encode_proof(list(reversed(siblings)), directions)
    assert not b.verify_proof(leaves[0], reversed_proof, root)


def test_negative_flipped_direction_bit():
    leaves = corpus(200)
    root = b.batch_root(leaves)
    siblings, directions = b.decode_proof(b.build_proof(leaves, 1))
    flipped = list(directions)
    flipped[0] ^= 1
    assert not b.verify_proof(leaves[1], b.encode_proof(siblings, flipped), root)


def test_negative_msb_first_direction_encoding():
    leaves = corpus(200)
    root = b.batch_root(leaves)
    _, directions = b.decode_proof(b.build_proof(leaves, 1))
    blob = _blob(b.build_proof(leaves, 1))
    msb = bytearray(len(directions) // 8 + (1 if len(directions) % 8 else 0))
    for i, bit in enumerate(directions):
        if bit:
            msb[i >> 3] |= 1 << (7 - (i & 7))
    rebuilt = bytearray([blob[0]]) + msb + blob[1 + len(msb):]
    encoded = _reencode(rebuilt)
    assert encoded != b.build_proof(leaves, 1)
    # MSB-first puts bit 0 in the high position, which is an unused trailing
    # bit for depth 8? no -- depth 8 fills the byte, so this must fail on the
    # recomputed root instead of the schema.
    assert not b.verify_proof(leaves[1], encoded, root)


def test_negative_substituted_leaf_does_not_verify():
    leaves = corpus(200)
    root = b.batch_root(leaves)
    proof = b.build_proof(leaves, 0)
    other = b.leaf_from_source(RUN_ID, '#000000.txt', 0, 'すり替えた文。')
    assert not b.verify_proof(other, proof, root)


def test_negative_reorder_is_caught_by_the_seq_guard():
    leaves = corpus(5)
    leaves[0], leaves[1] = leaves[1], leaves[0]
    with pytest.raises(b.BatchReject, match='contiguous'):
        b.build_levels(leaves)


def test_negative_swapping_content_under_fixed_seq_changes_the_root():
    # An attacker that keeps seq ascending but swaps which literal sits at
    # each position still moves the root.
    leaves = corpus(5)
    swapped = corpus(5)
    swapped[0]['unit_id'], swapped[1]['unit_id'] = (
        swapped[1]['unit_id'], swapped[0]['unit_id'])
    swapped[0]['literal_digest'], swapped[1]['literal_digest'] = (
        swapped[1]['literal_digest'], swapped[0]['literal_digest'])
    assert [leaf['seq'] for leaf in swapped] == [0, 1, 2, 3, 4]
    assert b.batch_root(leaves) != b.batch_root(swapped)


def test_negative_omitted_leaf_changes_the_root():
    leaves = corpus(5)
    assert b.batch_root(leaves) != b.batch_root(leaves[:4])


def test_negative_empty_proof_string():
    with pytest.raises(b.ProofReject, match='empty'):
        b.decode_proof('')


def test_negative_bad_base64_length():
    with pytest.raises(b.ProofReject, match='not a valid unpadded'):
        b.decode_proof('AAAAA')


def test_verify_never_raises_on_malformed_input():
    leaves = corpus(2)
    root = b.batch_root(leaves)
    for bad in ['', '=', 'A' * 5, '!!!!', 'A' * 400]:
        assert b.verify_proof(leaves[0], bad, root) is False


# --- encoder guards -------------------------------------------------------

def test_encode_rejects_mismatched_lengths():
    with pytest.raises(b.ProofReject, match='disagree'):
        b.encode_proof([b'\x00' * 32], [])


def test_encode_rejects_short_sibling():
    with pytest.raises(b.ProofReject, match='32 bytes'):
        b.encode_proof([b'\x00' * 31], [0])


def test_encode_rejects_over_max_depth():
    with pytest.raises(b.ProofReject, match='exceeds'):
        b.encode_proof([b'\x00' * 32] * 9, [0] * 9)
