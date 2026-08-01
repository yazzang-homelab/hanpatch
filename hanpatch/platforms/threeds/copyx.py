"""One counted copy for the whole 3DS layer.

Every byte mover here used to loop `while left: b = f.read(...); left -= len(b)`,
which at EOF reads `b''` forever - an unbounded silent loop - or, where the loop
did terminate, reported a short copy as success so the caller padded the shortfall
and shipped something that looked complete. Both failures were found in review, in
three separate modules, so the loop lives in one place now.
"""


CHUNK = 1 << 22


def copy_exact(fsrc, out, total, what, at=None):
    """Copy exactly `total` bytes or raise, naming the source and the shortfall."""
    if total < 0:
        raise SystemExit(
            f'{what}: asked to copy {total} bytes, which is a caller bug - a '
            f'negative length reads to EOF on a buffered file and would silently '
            f'write the whole remainder of the source')
    left = total
    while left:
        b = fsrc.read(min(CHUNK, left))
        if not b:
            raise SystemExit(
                f'{what}: needed {total} bytes'
                + (f' from offset {at}' if at is not None else '')
                + f' but the source ran out after {total - left}; refusing to pad '
                  f'a truncated read into something that looks complete')
        out.write(b)
        left -= len(b)
    return total
