#!/usr/bin/env python3
"""Emit the frozen D1/D2 KAT vector file.

Regenerating this file after a protocol change is the visible signal that every
published census and every recorded proof must be re-derived.
"""
from __future__ import annotations

import argparse
import json
import sys

from hanpatch import a6batch as batch
from hanpatch import a6census as census

RUN_ID = '00000000-0000-4000-8000-000000000001'
SIZES = (1, 2, 3, 5, 100, 200)


def corpus(n: int) -> list[dict]:
    return [
        batch.leaf_from_source(RUN_ID, f'#{i:06d}.txt', i, f'ふつうの文 {i}。')
        for i in range(n)
    ]


def d2_vectors() -> list[dict]:
    out = []
    for n in SIZES:
        leaves = corpus(n)
        levels = batch.build_levels(leaves)
        probes = sorted({0, 1, n - 1, n // 2} & set(range(n)))
        out.append({
            'n': n,
            'depth': len(levels) - 1,
            'batch_root': levels[-1][0].hex(),
            'level_sizes': [len(level) for level in levels],
            'levels': [[node.hex() for node in level] for level in levels],
            'leaf_jcs': [census.jcs(leaf) for leaf in leaves],
            'leaf_jcs_bytes': [census.jcs_bytes(leaf) for leaf in leaves],
            'proofs': [
                {
                    'index': index,
                    'proof_b64u': batch.build_proof(leaves, index),
                    'verifies': batch.verify_proof(
                        leaves[index], batch.build_proof(leaves, index),
                        levels[-1][0]),
                }
                for index in probes
            ],
        })
    return out


def d1_vectors() -> list[dict]:
    boundary = '漢' * 415 + 'a' * 2
    cases = {
        'empty': '',
        'boundary_2048': boundary,
        'boundary_2049': boundary + 'a',
        'quote_at_scalar_cap': '"' * census.MAX_LITERAL_SCALARS,
        'cjk_at_scalar_cap': '漢' * census.MAX_LITERAL_SCALARS,
        'backslash_at_raw_byte_cap': '\\' * census.MAX_LITERAL_NFC_BYTES,
        'emoji_at_raw_byte_cap': '\U0001f600' * 384,
        'emoji_over_raw_byte_cap': '\U0001f600' * 385,
    }
    out = []
    for name, literal in cases.items():
        text = census.nfc(literal)
        frame = census.worst_case_chunk(text, '#000004.txt', 0, RUN_ID)
        out.append({
            'name': name,
            'scalars': len(text),
            'nfc_utf8_bytes': len(text.encode('utf-8')),
            'frame_jcs_bytes': census.jcs_bytes(frame),
            'admissible': census.admissible(text, '#000004.txt', 0, RUN_ID),
            'class': census.classify(literal, '#000004.txt', 0, RUN_ID, False)[0],
        })
    return out


def build() -> dict:
    return {
        'schema': 'a6dq7.kat_vectors.v1',
        'run_id': RUN_ID,
        'oracle': {
            'version': census.ORACLE_VERSION,
            'source_digest': census.oracle_source_digest(),
        },
        'd1_wire_bytes': d1_vectors(),
        'd2_merkle': d2_vectors(),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    with open(args.out, 'w', encoding='utf-8') as handle:
        json.dump(build(), handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write('\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
