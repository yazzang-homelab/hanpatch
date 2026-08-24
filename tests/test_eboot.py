"""Text stored inside the game executable: how it is found, budgeted, patched.

Every case is a defect this module actually had, or a property whose loss would
corrupt the executable rather than merely mistranslate it. The scanner is the
dangerous part: it decides which bytes get overwritten, so a wrong boundary
writes Korean into the middle of a sentence, into a pointer, or into code.

Run: python3 tests/test_eboot.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hanpatch.platforms.psp import eboot  # noqa: E402

PASS, FAIL, SKIP = [], [], []


def case(name, ok):
    (PASS if ok else FAIL).append(name)
    print(('  ok   ' if ok else '  FAIL ') + name)


def skip(name, why):
    SKIP.append(name)
    print('  skip ' + name + ' (' + why + ')')


def _elf(sections):
    """A minimal 32-bit LE ELF whose section table describes `sections`.

    `sections` is [(payload, executable)]. Payloads are laid out after the
    header and section table so the offsets in the table are real.
    """
    shentsize = 40
    shoff = 0x100
    table_end = shoff + shentsize * len(sections)
    body = bytearray()
    placed = []
    for payload, is_exec in sections:
        placed.append((table_end + len(body), len(payload), is_exec))
        body += payload

    head = bytearray(shoff)
    head[0:4] = b'\x7fELF'
    head[4] = 1                       # 32-bit
    head[5] = 1                       # little-endian
    head[0x20:0x24] = shoff.to_bytes(4, 'little')
    head[0x2e:0x30] = shentsize.to_bytes(2, 'little')
    head[0x30:0x32] = len(sections).to_bytes(2, 'little')

    table = bytearray()
    for off, size, is_exec in placed:
        h = bytearray(shentsize)
        h[4:8] = (1).to_bytes(4, 'little')            # SHT_PROGBITS
        flags = 0x2 | (0x4 if is_exec else 0)         # ALLOC | EXECINSTR
        h[8:12] = flags.to_bytes(4, 'little')
        h[0x10:0x14] = off.to_bytes(4, 'little')
        h[0x14:0x18] = size.to_bytes(4, 'little')
        table += h
    return bytes(head) + bytes(table) + bytes(body)


def _sj(text):
    return text.encode('shift_jis')


def test_refuses_non_elf():
    """The section table is the only non-guessing way to find data."""
    for blob, why in ((b'nope' + b'\x00' * 64, 'not an ELF'),
                      (b'\x7fELF\x02\x01' + b'\x00' * 64, '64-bit'),
                      (b'\x7fELF\x01\x01' + b'\x00' * 64, 'no section table')):
        try:
            eboot.data_ranges(blob)
            ok = False
        except eboot.EbootError:
            ok = True
        case('refused: ' + why, ok)


def test_skips_executable_sections():
    """Machine code decodes as kana often enough to matter.

    Measured on the shipped ELF: a whole-file scan found 16 kana-bearing runs
    inside executable sections and all 16 were noise, against 442 in data
    sections that were all real. Filtering by SHF_EXECINSTR removes the class,
    and it also means a patch can never write into code.
    """
    code = _sj('う% ') + b'\x00'
    data = _sj('せってい') + b'\x00'
    blob = _elf([(code, True), (data, False)])
    found = [t for _o, _r, t in eboot.strings(blob)]
    case('a kana-bearing run in an executable section is not text',
         'う% ' not in found)
    case('a string in a data section is found', 'せってい' in found)


def test_whole_cstrings_only():
    """A slot starts after a NUL, never at the first Shift-JIS lead byte.

    This shipped as a bug: anchoring on the lead byte cut
    `記録メディアの空き容量が…` down to `新しく…` and dropped the `%s ` from
    `%s ゲームデータ`, because the head of the string was ASCII. Overwriting such
    a slot leaves the dropped head in place, so the sentence renders half
    Japanese.
    """
    payload = _sj('%s ゲームデータ') + b'\x00' + _sj('せってい') + b'\x00'
    blob = _elf([(payload, False)])
    got = [t for _o, _r, t in eboot.strings(blob)]
    case('an ASCII prefix stays part of its string', '%s ゲームデータ' in got)
    case('the suffix alone is not reported as a separate slot',
         'ゲームデータ' not in got)
    starts = [o for o, _r, _t in eboot.strings(blob)]
    lo = eboot.data_ranges(blob)[0][0]
    case('every slot starts at a cell boundary',
         all(o == lo or blob[o - 1] == 0 for o in starts))


def test_requires_kana():
    """Kanji-only runs are indistinguishable from binary; kana is the evidence."""
    payload = _sj('設定') + b'\x00' + _sj('せってい') + b'\x00'
    blob = _elf([(payload, False)])
    got = [t for _o, _r, t in eboot.strings(blob)]
    case('a kana-bearing string is text', 'せってい' in got)
    case('a kanji-only run is not claimed as text', '設定' not in got)


def test_budget_is_the_tightest_slot():
    """One translation serves every slot sharing a source.

    So the budget is the MINIMUM across them. Honouring the average would
    overrun the tightest slot, and the byte past a slot is the next datum.
    """
    wide = _sj('せってい') + b'\x00' * 8
    tight = _sj('せってい') + b'\x00'
    blob = _elf([(wide + tight, False)])
    b = eboot.budgets(blob)
    case('the budget is the shortest occurrence', b['せってい'] == len(_sj('せってい')))


def test_identity_build():
    """Replacing nothing must reproduce the blob exactly.

    This is the property that makes the writer trustworthy: if a no-op build
    can perturb a byte, no patched build can be believed.
    """
    payload = _sj('せってい') + b'\x00' + _sj('はじめから') + b'\x00'
    blob = _elf([(payload, False)])
    new, applied = eboot.build(blob, {}, lambda s: b'')
    case('an empty replacement set reproduces the blob', new == blob)
    case('and applies nothing', applied == 0)


def test_patch_is_in_place_and_padded():
    """A slot cannot move, so the write is length-preserving.

    The bytes after the NUL belong to whatever the compiler put there, and a
    zero is not proof of free space - it is equally the first byte of an aligned
    pointer. So the write pads with NUL to the ORIGINAL length and never past it.
    """
    payload = _sj('せってい') + b'\x00' + b'\xde\xad\xbe\xef'
    blob = _elf([(payload, False)])
    new, applied = eboot.build(blob, {'せってい': '설정'},
                               lambda s: b'\x8c\xcd\x8f\x5f')
    case('one slot was written', applied == 1)
    case('the file length is unchanged', len(new) == len(blob))
    off = eboot.strings(blob)[0][0]
    n = len(_sj('せってい'))
    case('the slot holds the new bytes then NUL',
         new[off:off + 4] == b'\x8c\xcd\x8f\x5f'
         and new[off + 4:off + n] == b'\x00' * (n - 4))
    case('the neighbour after the slot is untouched',
         new[off + n:off + n + 5] == b'\x00\xde\xad\xbe\xef')


def test_overflow_is_refused_not_truncated():
    """Half a syllable is a wrong glyph, not a short line.

    Truncating would also run the risk of leaving a lone lead byte, which the
    renderer pairs with whatever follows.
    """
    payload = _sj('せってい') + b'\x00'
    blob = _elf([(payload, False)])
    try:
        eboot.build(blob, {'せってい': 'x'}, lambda s: b'\x01' * 99)
        ok = False
    except eboot.EbootError:
        ok = True
    case('a translation larger than its slot is an error', ok)


def test_real_elf():
    """The shipped executable, when this box has it."""
    path = os.environ.get('HANPATCH_EBOOT_ELF')
    if not path:
        proj = os.environ.get('HANPATCH_PROJECT')
        if proj:
            path = os.path.join(proj, 'EBOOT.elf')
    if not path or not os.path.exists(path):
        skip('shipped ELF', 'set HANPATCH_EBOOT_ELF or HANPATCH_PROJECT')
        return
    with open(path, 'rb') as fh:
        blob = fh.read()
    found = eboot.strings(blob)
    case('the shipped ELF yields text', len(found) > 100)
    lo_hi = eboot.data_ranges(blob)
    inside = all(any(lo <= o < hi for lo, hi in lo_hi) for o, _r, _t in found)
    case('every slot lies inside a non-executable section', inside)
    case('every slot starts at a cell boundary',
         all(blob[o - 1] == 0 or any(o == lo for lo, _h in lo_hi)
             for o, _r, _t in found))
    new, applied = eboot.build(blob, {}, lambda s: b'')
    case('identity build on the shipped ELF', new == blob and applied == 0)
    case('the fourth title-menu item is present',
         any(t == 'インストール' for _o, _r, t in found))


def main():
    print('elf structure')
    test_refuses_non_elf()
    test_skips_executable_sections()
    print('string boundaries')
    test_whole_cstrings_only()
    test_requires_kana()
    print('budgets')
    test_budget_is_the_tightest_slot()
    print('writing')
    test_identity_build()
    test_patch_is_in_place_and_padded()
    test_overflow_is_refused_not_truncated()
    print('shipped corpus')
    test_real_elf()
    print('\n%d passed, %d failed, %d skipped'
          % (len(PASS), len(FAIL), len(SKIP)))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
