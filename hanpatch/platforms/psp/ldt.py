"""NUL-delimited text slots in Classic Dungeon X2 ``*.LDT`` payloads.

``OPENING.LDT`` is not script bytecode. It is a data asset loaded by name from
the script, and its narration lives in ordinary NUL-terminated Shift-JIS slots
inside the asset payload. A slot is addressed by its byte offset and cannot
grow: the byte after its terminator belongs to the next field in the asset.

This module deliberately recognises only whole C strings containing kana. A
plain "decodes as Shift-JIS" scan would claim binary words as kanji, while
starting at the first lead byte would return suffixes of a real sentence. Both
mistakes turn extraction into permission to overwrite bytes that are not text.
"""
import collections
import re


Entry = collections.namedtuple('Entry', 'key jp budget')

_KANA = re.compile(r'[\u3040-\u309f\u30a0-\u30ff\u3005\u3006\u30fc]')

#: Broader than `_KANA`. Translation writes must reject kanji-only binary
#: lookalikes, while this read-only preserve scan fails safe by keeping their
#: cells. The prologue payload holds `見本` next to the six narration lines.
_JAPANESE = re.compile(
    r'[\u3040-\u309f\u30a0-\u30ff\u3400-\u9fff\u3005\u3006\u30fc]')


class LdtError(Exception):
    pass


def _decode(raw):
    try:
        text = raw.decode('shift_jis')
    except UnicodeDecodeError:
        return None
    if any(ord(ch) < 0x20 and ch not in '\n\t' for ch in text):
        return None
    return text


def _cells(blob):
    """Yield ``(offset, bytes)`` for whole NUL-delimited cells in ``blob``."""
    start = 0
    for i, value in enumerate(blob):
        if value:
            continue
        yield start, bytes(blob[start:i])
        start = i + 1
    # An unterminated tail is data, not a writable string slot.


def _collect(blob, min_len, marker):
    out = []
    for off, raw in _cells(blob):
        if len(raw) < min_len:
            continue
        text = _decode(raw)
        if text and marker.search(text):
            out.append(Entry('off%x' % off, text, len(raw)))
    return out


def strings(blob, min_len=4):
    """Player-visible Japanese slots, keyed by stable payload offset."""
    return _collect(blob, min_len, _KANA)


def reference_strings(blob, min_len=2):
    """Japanese C strings whose glyphs an untouched slot may still render.

    This is a read-only preserve surface, never an extraction/write authority.
    """
    return _collect(blob, min_len, _JAPANESE)


def stored(blob, keys):
    """``{key: raw bytes}`` for known offsets, independent of language."""
    out = {}
    for key in keys:
        if not key.startswith('off'):
            continue
        try:
            off = int(key[3:], 16)
        except ValueError:
            continue
        if off < 0 or off >= len(blob):
            continue
        end = blob.find(b'\x00', off)
        if end >= 0:
            out[key] = bytes(blob[off:end])
    return out


def build(blob, edits):
    """Rewrite extracted slots in place; ``edits`` maps keys to encoded bytes."""
    slots = {entry.key: entry for entry in strings(blob)}
    unknown = set(edits) - set(slots)
    if unknown:
        raise LdtError('no extracted LDT slot named %s'
                       % ', '.join(sorted(unknown)[:5]))
    out = bytearray(blob)
    for key, raw in edits.items():
        if b'\x00' in raw:
            raise LdtError('replacement for %s contains a NUL' % key)
        slot = slots[key]
        if len(raw) > slot.budget:
            raise LdtError('%d bytes at %s exceed the %d-byte LDT slot'
                           % (len(raw), key, slot.budget))
        off = int(key[3:], 16)
        out[off:off + slot.budget] = raw + b'\x00' * (slot.budget - len(raw))
    return bytes(out), len(edits)
