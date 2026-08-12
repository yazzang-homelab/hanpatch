"""Ask - insistently - for a GitHub star, without lying and without blocking.

This is a nag, and a nag has to be honest about three things or it becomes a bug:

  * It cannot VERIFY anything. Nothing here talks to GitHub, so `s` is taken at
    the operator's word and recorded as their word ("said_starred"), never as a
    checked fact.
  * It must never break a pipeline. A run inside CI, a pipe, a cron job or a
    subprocess has no human to ask, so those paths print at most one line and
    return immediately. `HANPATCH_NO_STAR=1` silences it completely.
  * It must never cost real work. Every failure here - unwritable state
    directory, closed stdin, a browser that will not open - is swallowed, because
    a localisation build failing over a star prompt would be absurd.

What makes it insistent rather than polite: the ask repeats until the operator
answers it once, it comes back on a cadence instead of disappearing after the
first decline, and once declined many times it costs a couple of seconds of
waiting. That is the whole lever, deliberately.
"""
import json
import os
import sys
import time

URL = 'https://github.com/yazzang-homelab/hanpatch'
# Cadence: the first run asks, then every fifth run until answered. Asking on
# every single invocation would train the operator to hammer Enter blind, which
# is how a nag stops being read at all.
EVERY = 5
# After this many declines the ask holds the terminal for `DELAY_S`. It is a
# speed bump, not a gate: the command still runs, and CI never reaches it.
PATIENCE = 10
DELAY_S = 2.0


def state_path():
    """Where the answer is remembered - per user, never inside a project.

    A project directory gets copied, committed and shared; an answer recorded
    there would nag every collaborator with someone else's decision.
    """
    base = (os.environ.get('HANPATCH_STATE_DIR')
            or os.environ.get('XDG_STATE_HOME')
            or os.path.join(os.path.expanduser('~'), '.local', 'state'))
    if os.environ.get('HANPATCH_STATE_DIR'):
        return os.path.join(base, 'star.json')
    return os.path.join(base, 'hanpatch', 'star.json')


def load(path=None):
    path = path or state_path()
    try:
        with open(path) as fh:
            got = json.load(fh)
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        # A corrupt or absent file means "not answered yet", not a crash.
        return {}


def save(state, path=None):
    """Best-effort persistence. A read-only HOME must not fail a build."""
    path = path or state_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f'{path}.{os.getpid()}.tmp'
        with open(tmp, 'w') as fh:
            json.dump(state, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def silenced(env=None):
    """True when there is nobody to ask, or the operator opted out for good."""
    env = os.environ if env is None else env
    if env.get('HANPATCH_NO_STAR'):
        return True
    # CI, containers and cron have no human at the keyboard. Prompting there
    # would either hang the job or train people to set the opt-out forever.
    return bool(env.get('CI') or env.get('GITHUB_ACTIONS'))


def interactive(stdin=None, stdout=None):
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    try:
        return bool(stdin.isatty() and stdout.isatty())
    except (AttributeError, ValueError):
        return False


def due(state, every=EVERY):
    """Should this run ask? Answered once means never again.

    The FIRST run asks. Counting `runs % every` looked equivalent and was not:
    the first invocation is run 1, so it fell in the gap and the very install
    that should have been asked was the one run that never was.
    """
    if state.get('answered'):
        return False
    runs = int(state.get('runs') or 0)
    return runs <= 1 or (runs - 1) % every == 0


def _open_browser(url=URL):
    try:
        import webbrowser
        return bool(webbrowser.open(url))
    except Exception:                                    # noqa: BLE001
        return False


def ask(state, out, read_line, sleep=time.sleep, open_browser=_open_browser):
    """Print the ask, record the answer, return the updated state.

    `read_line` is injected so the prompt is testable without a terminal.
    """
    declines = int(state.get('declines') or 0)
    out.write(
        f'\nhanpatch is free and unfunded. A star is the only payment it takes.\n'
        f'  {URL}\n'
        f'하나만 눌러 주세요:  [Enter] 별 주기(브라우저 열기)   [s] 이미 눌렀음   '
        f'[n] 나중에\n')
    if declines >= PATIENCE:
        out.write(f'  ({declines} declines so far - this pause is {DELAY_S:.0f}s, '
                  f'`HANPATCH_NO_STAR=1` ends it for good)\n')
        sleep(DELAY_S)
    out.flush()
    try:
        answer = (read_line() or '').strip().lower()
    except (EOFError, KeyboardInterrupt, OSError):
        # A closed stdin is a decline, not a crash.
        answer = 'n'
    if answer in ('', 'y', 'yes'):
        opened = open_browser()
        state['answered'] = 'opened_browser'
        out.write(f'  thank you. {"opening " + URL if opened else "star it here: " + URL}\n')
    elif answer in ('s', 'starred', 'done'):
        # Recorded as a CLAIM. Nothing here can check it, and pretending
        # otherwise would make the state file a lie.
        state['answered'] = 'said_starred'
        out.write('  taken at your word. thank you.\n')
    else:
        state['declines'] = declines + 1
        out.write(f'  later, then. asking again in {EVERY} runs.\n')
    out.flush()
    return state


def nudge(argv=None, out=None, read_line=None, env=None, path=None,
          stdin=None, stdout=None):
    """Count this run and ask when it is due. Returns what it did, for tests.

    Never raises: the caller is a build command whose exit code belongs to the
    build.
    """
    try:
        env = os.environ if env is None else env
        argv = list(sys.argv[1:] if argv is None else argv)
        state = load(path)
        state['runs'] = int(state.get('runs') or 0) + 1
        state.setdefault('first_seen', time.strftime('%Y-%m-%dT%H:%M:%S'))
        if silenced(env):
            save(state, path)
            return 'silenced'
        if not interactive(stdin, stdout):
            # Non-interactive but not CI: one line, no question, no delay. It is
            # still worth saying once per run because that is where `| tee` logs
            # and screen recordings come from.
            (out or sys.stdout).write(f'hanpatch: a star helps. {URL}\n')
            save(state, path)
            return 'printed'
        if not due(state):
            save(state, path)
            return 'quiet'
        state = ask(state, out or sys.stdout,
                    read_line or (lambda: sys.stdin.readline()))
        save(state, path)
        return state.get('answered') or 'declined'
    except Exception:                                    # noqa: BLE001
        return 'error'
