"""Merkle batch commitment and binary proof grammar for the A6/DQ7 relay.

Revision 6 D2 freezes the encoding; every rejection path here must yield zero
relay bytes, so decoding is strict and total: any deviation raises
`ProofReject` before a caller can act on the contents.
"""
from __future__ import annotations

import base64
import hashlib

from hanpatch.a6census import jcs, literal_digest, nfc

LEAF_DOMAIN = b'A6-DQ7/batch-leaf/v2\x00'
NODE_DOMAIN = b'A6-DQ7/batch-node/v2\x00'

MAX_LEAVES = 200
MAX_DEPTH = 8
HASH_BYTES = 32

_B64U_ALPHABET = frozenset(
    'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'
)


class ProofReject(ValueError):
    """The proof container is out of contract. Zero relay bytes."""


class BatchReject(ValueError):
    """The batch itself is out of contract."""


# ---------------------------------------------------------------------------
# Leaves and tree
# ---------------------------------------------------------------------------

def ordered_leaf(run_id: str, unit_id: str, seq: int, literal_nfc: str) -> dict:
    """The canonical ordered leaf object, committed in `seq` order."""
    return {
        'codepoints': len(literal_nfc),
        'literal_digest': literal_digest(literal_nfc),
        'run_id': run_id,
        'seq': seq,
        'unit_id': unit_id,
    }


def leaf_from_source(run_id: str, unit_id: str, seq: int, literal_raw: str) -> dict:
    return ordered_leaf(run_id, unit_id, seq, nfc(literal_raw))


def leaf_hash(leaf: dict) -> bytes:
    return hashlib.sha256(LEAF_DOMAIN + jcs(leaf).encode('utf-8')).digest()


def node_hash(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(NODE_DOMAIN + left + right).digest()


def build_levels(leaves: list[dict]) -> list[list[bytes]]:
    """Level 0 upward. Duplicate-last padding applies per level only."""
    if not leaves:
        raise BatchReject('a batch needs at least one leaf')
    if len(leaves) > MAX_LEAVES:
        raise BatchReject(f'batch of {len(leaves)} exceeds the {MAX_LEAVES}-leaf cap')
    for index, leaf in enumerate(leaves):
        if leaf['seq'] != leaves[0]['seq'] + index:
            raise BatchReject('leaf seq must be contiguous and ascending')

    levels = [[leaf_hash(leaf) for leaf in leaves]]
    while len(levels[-1]) > 1:
        current = levels[-1]
        if len(current) % 2 == 1:
            current = current + [current[-1]]
        levels.append([
            node_hash(current[i], current[i + 1])
            for i in range(0, len(current), 2)
        ])
    return levels


def batch_root(leaves: list[dict]) -> bytes:
    return build_levels(leaves)[-1][0]


def batch_depth(leaves: list[dict]) -> int:
    return len(build_levels(leaves)) - 1


# ---------------------------------------------------------------------------
# Proof container
# ---------------------------------------------------------------------------

def _encode_direction_bits(directions: list[int]) -> bytes:
    depth = len(directions)
    out = bytearray((depth + 7) // 8)
    for i, bit in enumerate(directions):
        if bit:
            out[i >> 3] |= 1 << (i & 7)
    return bytes(out)


def encode_proof(siblings: list[bytes], directions: list[int]) -> str:
    if len(siblings) != len(directions):
        raise ProofReject('sibling count and direction count disagree')
    depth = len(siblings)
    if depth > MAX_DEPTH:
        raise ProofReject(f'depth {depth} exceeds {MAX_DEPTH}')
    for sibling in siblings:
        if len(sibling) != HASH_BYTES:
            raise ProofReject('every sibling is exactly 32 bytes')
    blob = bytes([depth]) + _encode_direction_bits(directions) + b''.join(siblings)
    return base64.urlsafe_b64encode(blob).decode('ascii').rstrip('=')


def build_proof(leaves: list[dict], index: int) -> str:
    levels = build_levels(leaves)
    if not 0 <= index < len(levels[0]):
        raise BatchReject('leaf index out of range')

    siblings: list[bytes] = []
    directions: list[int] = []
    position = index
    for level in levels[:-1]:
        nodes = level + [level[-1]] if len(level) % 2 == 1 else level
        if position % 2 == 0:
            siblings.append(nodes[position + 1])
            directions.append(0)      # sibling is the right child
        else:
            siblings.append(nodes[position - 1])
            directions.append(1)      # sibling is the left child
        position //= 2
    return encode_proof(siblings, directions)


def decode_proof(proof_b64u: str) -> tuple[list[bytes], list[int]]:
    if not isinstance(proof_b64u, str):
        raise ProofReject('proof must be a string')
    if any(ch not in _B64U_ALPHABET for ch in proof_b64u):
        raise ProofReject('proof uses a character outside unpadded base64url')

    remainder = len(proof_b64u) % 4
    if remainder == 1:
        raise ProofReject('proof length is not a valid unpadded base64url length')
    blob = base64.urlsafe_b64decode(proof_b64u + '=' * ((4 - remainder) % 4))

    if not blob:
        raise ProofReject('proof is empty')
    depth = blob[0]
    if depth > MAX_DEPTH:
        raise ProofReject(f'depth byte {depth} exceeds {MAX_DEPTH}')

    direction_len = (depth + 7) // 8
    expected = 1 + direction_len + HASH_BYTES * depth
    if len(blob) != expected:
        raise ProofReject(
            f'decoded length {len(blob)} != {expected} for depth {depth}'
        )

    direction_bytes = blob[1:1 + direction_len]
    if depth % 8:
        unused_mask = (0xFF << (depth % 8)) & 0xFF
        if direction_bytes and direction_bytes[-1] & unused_mask:
            raise ProofReject('unused trailing direction bits must be zero')

    directions = [
        (direction_bytes[i >> 3] >> (i & 7)) & 1
        for i in range(depth)
    ]
    body = blob[1 + direction_len:]
    siblings = [
        body[i * HASH_BYTES:(i + 1) * HASH_BYTES]
        for i in range(depth)
    ]
    return siblings, directions


def verify_proof(leaf: dict, proof_b64u: str, expected_root: bytes) -> bool:
    """Recompute the root from a leaf and its proof. Never raises on mismatch."""
    try:
        siblings, directions = decode_proof(proof_b64u)
    except ProofReject:
        return False

    node = leaf_hash(leaf)
    for sibling, direction in zip(siblings, directions):
        node = node_hash(sibling, node) if direction else node_hash(node, sibling)
    return node == expected_root
