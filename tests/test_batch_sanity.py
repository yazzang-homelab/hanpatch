"""Guards against the batch-window collapse that cost 57,621 single-unit calls.

`--batch 8` prefix-matched onto `--batch-chars 8`, capping a batch at eight source
characters. Every call then paid a ~768-token prompt prefix to translate ~42 tokens
of Japanese. DeepSeek's cache priced the prefix at 1/50 and hid it; on a
uniform-rate endpoint that mistake IS the bill.
"""
from __future__ import annotations

import argparse

import pytest

from hanpatch import run


def args(**over):
    ns = argparse.Namespace(batch_chars=2600, max_items=14)
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def parser():
    p = argparse.ArgumentParser(prog='hanpatch translate', allow_abbrev=False)
    return p


# --- the exact historical failure -----------------------------------------

def test_cli_batch_now_forwards_to_max_items():
    # `hanpatch translate --batch 8` used to reach the runner as `--batch 8`, which
    # argparse resolved to `--batch-chars 8`. It must map to the flag that exists.
    import argparse as _ap
    from hanpatch import cli, run as runmod
    seen = {}
    real = runmod.main
    try:
        runmod.main = lambda argv: seen.setdefault('argv', argv) and 0
        cli.cmd_translate(_ap.Namespace(family='f', models='', limit=0, workers=0,
                                        batch=8, refail=False, qafail=False,
                                        qa_list=''))
    finally:
        runmod.main = real
    argv = seen['argv']
    assert '--max-items' in argv
    assert argv[argv.index('--max-items') + 1] == '8'
    assert '--batch' not in argv
    assert '--batch-chars' not in argv


def test_cli_batch_value_survives_the_runner_floor():
    # 8 ITEMS is sane; 8 CHARACTERS was not. The forwarded value must not trip the
    # batch-chars floor, which is what proves the two were confused.
    run.check_batch_sanity(parser(), args(max_items=8))


def test_eight_character_window_is_refused(capsys):
    with pytest.raises(SystemExit):
        run.check_batch_sanity(parser(), args(batch_chars=8))
    assert 'below the 400-char floor' in capsys.readouterr().err


def test_floor_is_the_boundary():
    run.check_batch_sanity(parser(), args(batch_chars=run.MIN_BATCH_CHARS))
    with pytest.raises(SystemExit):
        run.check_batch_sanity(parser(), args(batch_chars=run.MIN_BATCH_CHARS - 1))


def test_floor_clears_the_longest_measured_source_line():
    # Longest ELIGIBLE DQ7 source literal is 344 UTF-8 bytes; a window under that
    # cannot hold even one string.
    assert run.MIN_BATCH_CHARS > 344


def test_default_window_holds_a_real_batch():
    ns = args()
    # 133 bytes is the measured mean source length.
    assert ns.batch_chars // 133 >= ns.max_items


def test_zero_or_negative_max_items_refused():
    for bad in (0, -1):
        with pytest.raises(SystemExit):
            run.check_batch_sanity(parser(), args(max_items=bad))


def test_sane_configuration_passes():
    run.check_batch_sanity(parser(), args())


# --- abbreviations are off everywhere -------------------------------------

def build(module_parser_args):
    """Parse with each real parser, asserting abbreviations no longer resolve."""
    return module_parser_args


def test_runner_has_no_batch_option_at_all():
    # Keeps the existing invariant: the runner must not own a flag that can be
    # mistaken for --batch-chars.
    with pytest.raises(SystemExit):
        run.main(['--family', 'x', '--batch', '8'])


@pytest.mark.parametrize('abbrev', ['--work', '--model', '--max-item', '--limi'])
def test_translate_parser_rejects_every_partial_flag(abbrev):
    with pytest.raises(SystemExit):
        run.main(['--family', 'x', abbrev, '4'])


def test_qa_parser_still_accepts_its_own_batch_flag():
    from hanpatch import qa
    # `--batch` is real on qa; it must keep working, that asymmetry is the trap.
    with pytest.raises(SystemExit):
        qa.main(['--batc', '8'])          # abbreviation: rejected


def test_qa_and_translate_disagree_on_batch_by_design():
    from hanpatch import qa
    import inspect
    assert '--batch' in inspect.getsource(qa.main)
    src = inspect.getsource(run.main)
    assert 'allow_abbrev=False' in src
    assert "'--batch'" not in src, 'the runner must not own a --batch flag'


def test_cli_dispatcher_disables_abbreviations():
    from hanpatch import cli
    import inspect
    assert 'allow_abbrev=False' in inspect.getsource(cli.main)
