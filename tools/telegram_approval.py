"""Ask the owner for approval over Telegram, and record the answer on GitHub.

The gate in ``tools.agent_approval`` decides that a pull request needs a human.
This module is how the human is reached when they are not at a keyboard: a
message with two buttons, and a wait for the tap.

What a tap actually does is post the ordinary ``/approve <sha>`` comment as the
owner.  Nothing here can approve anything by itself - it borrows the owner's
credential to say the same sentence the owner would have typed, and the gate
re-derives the verdict from scratch afterwards.  The audit trail on the pull
request is identical whether the tap happened in a browser or on a phone.

Three properties are load-bearing:

  * The button carries the head sha.  A tap on a message written for an older
    commit is refused, not applied to whatever is current.
  * Only one Telegram account may approve.  Anyone else who finds the bot gets
    a refusal, and the refusal is logged.
  * The bot must be its own.  ``getUpdates`` hands each update to exactly one
    reader, so pointing this at a token some other daemon already polls would
    make the two steal each other's messages at random.

The decision logic is pure and lives at the top; the network is at the bottom.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

API = 'https://api.telegram.org/bot%s/%s'

APPROVE = 'ok'
HOLD = 'no'

# A tap is answered within this window or the message is abandoned.  A push in
# the meantime cancels the whole job anyway, which is the behaviour we want:
# the question was about a commit that no longer exists.
DEFAULT_WAIT_SECONDS = 600
LONG_POLL_SECONDS = 25


@dataclass(frozen=True)
class Tap:
    """A button press, already judged."""
    action: str                 # ok / no
    accepted: bool
    reason: str
    callback_id: str = ''
    message_id: Optional[int] = None
    chat_id: Optional[int] = None
    # False when the button belonged to a different pull request. Such a tap
    # must not be reported as this pull request's verdict - two waiters share
    # one bot, and getUpdates hands each update to whoever asks first.
    matches_pr: bool = True


def button_data(action: str, number: int, head_sha: str) -> str:
    """Telegram allows 64 bytes of callback data, so the sha is abbreviated."""
    return '%s:%d:%s' % (action, number, head_sha[:12])


def read_tap(update: dict, number: int, head_sha: str, owner_id: str) -> Optional[Tap]:
    """Turn one raw update into a verdict, or None when it is not for us.

    Every refusal keeps its reason: a silent drop here looks exactly like a
    bot that is down, and the owner would sit waiting for a tap that was
    already discarded.
    """
    query = update.get('callback_query')
    if not query:
        return None

    data = query.get('data') or ''
    parts = data.split(':')
    if len(parts) != 3:
        return None
    action, raw_number, sha = parts
    if action not in (APPROVE, HOLD):
        return None

    message = query.get('message') or {}
    common = {
        'callback_id': query.get('id', ''),
        'message_id': message.get('message_id'),
        'chat_id': (message.get('chat') or {}).get('id'),
    }

    sender = str((query.get('from') or {}).get('id', ''))
    if sender != str(owner_id):
        return Tap(action, False, 'not the owner (%s)' % sender, **common)

    if raw_number != str(number):
        return Tap(action, False, 'meant for pull request #%s' % raw_number,
                   matches_pr=False, **common)

    if not head_sha.lower().startswith(sha.lower()):
        return Tap(action, False, 'stale: written for %s, head is %s'
                   % (sha, head_sha[:12]), **common)

    if action == HOLD:
        return Tap(action, False, 'held by the owner', **common)

    return Tap(action, True, 'approved by the owner', **common)


def should_ask(author_login: str, has_write: bool, policy: str) -> Tuple[bool, str]:
    """Whether a stranger's pull request is allowed to ring the owner's phone.

    The repository is public, so anyone can open a pull request, and a
    notification channel that anyone can trigger is a notification channel the
    owner will mute. Strangers' pull requests still block on the gate - they
    just wait on GitHub instead of on a phone.
    """
    if policy == 'all':
        return True, 'notify_authors=all'
    if has_write:
        return True, '%s has write access' % author_login
    return False, '%s has no write access; not ringing the phone' % author_login


@dataclass(frozen=True)
class Change:
    """What the pull request does to the repository, as counted facts."""
    files: int = 0
    additions: int = 0
    deletions: int = 0
    paths: Sequence[str] = ()
    commit_titles: Sequence[str] = ()
    body: str = ''
    # (name, conclusion) for every other check that has reported on this head.
    checks: Sequence[Tuple[str, str]] = ()


# Paths that decide who is allowed to decide. A change here is not necessarily
# wrong, but it is never routine, and the owner is the only one who can say so.
LOAD_BEARING = (
    ('.github/workflows/', '검문소 자체(자동 검사 규칙)를 바꿉니다'),
    ('.github/agent-identities.json', '누구를 AI로 볼지, 몇 명이 승인해야 하는지를 바꿉니다'),
    ('.github/CODEOWNERS', '어느 파일을 누가 책임지는지를 바꿉니다'),
    ('tools/agent_approval', '승인 게이트의 코드 자체를 바꿉니다'),
    ('tools/telegram_approval', '지금 이 알림을 보내는 코드를 바꿉니다'),
)

_BUCKETS = (
    ('테스트', lambda p: p.startswith('tests/')),
    ('워크플로', lambda p: p.startswith('.github/')),
    ('문서', lambda p: p.endswith(('.md', '.txt'))),
    ('설정', lambda p: p.endswith(('.json', '.yml', '.yaml', '.toml', '.cfg'))),
    ('코드', lambda p: p.endswith('.py')),
)


def buckets(paths: Sequence[str]) -> List[Tuple[str, int]]:
    """Group changed paths into kinds a non-programmer can act on.

    First match wins, so `tests/x.py` is a test rather than code and
    `.github/x.yml` is a workflow rather than configuration.
    """
    counts = {name: 0 for name, _ in _BUCKETS}
    other = 0
    for path in paths:
        for name, matches in _BUCKETS:
            if matches(path):
                counts[name] += 1
                break
        else:
            other += 1
    out = [(name, counts[name]) for name, _ in _BUCKETS if counts[name]]
    if other:
        out.append(('기타', other))
    return out


def signals(change: Change) -> Tuple[List[str], List[str]]:
    """Reasons to relax and reasons to look twice, in plain language.

    Deterministic on purpose. A summary produced by a language model would be
    another unverifiable authority in the one place the project has decided to
    keep human.
    """
    good: List[str] = []
    warn: List[str] = []
    kinds = dict(buckets(change.paths))
    touched = sum(1 for path in change.paths)
    churn = change.additions + change.deletions

    for prefix, why in LOAD_BEARING:
        if any(path.startswith(prefix) for path in change.paths):
            warn.append(why)

    if kinds.get('테스트'):
        good.append('테스트가 같이 들어왔습니다')
    elif kinds.get('코드'):
        warn.append('코드는 바뀌는데 테스트는 그대로입니다')

    if kinds.get('문서'):
        good.append('문서도 같이 고쳤습니다')

    if touched and churn <= 200:
        good.append('변경 규모가 작습니다 (%d줄)' % churn)
    elif churn >= 800:
        warn.append('변경 규모가 큽니다 (%d줄) — 한 번에 보기 어렵습니다' % churn)

    if change.deletions > max(50, change.additions * 3):
        warn.append('지운 줄이 넣은 줄보다 훨씬 많습니다 — 기능이 빠졌는지 확인이 필요합니다')

    failed = [name for name, verdict in change.checks
              if verdict.lower() in ('failure', 'error', 'timed_out', 'cancelled')]
    waiting = [name for name, verdict in change.checks
               if verdict.lower() in ('pending', 'queued', 'in_progress')]
    if failed:
        warn.append('자동 검사 실패: %s' % ', '.join(failed[:3]))
    elif change.checks and not waiting:
        good.append('자동 검사는 전부 통과했습니다')

    return good, warn


def _first_sentences(text: str, limit: int = 2) -> List[str]:
    out = []
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line or line.startswith(('#', '<!--', '|', '---')):
            continue
        out.append(line)
        if len(out) >= limit:
            break
    return out


def compose(number: int, head_sha: str, url: str, reasons: Sequence[str],
            evidence: Sequence[str], title: str = '', author: str = '',
            change: Optional[Change] = None) -> str:
    """Plain text on purpose: file paths are full of characters that Telegram's
    Markdown would swallow or refuse."""
    change = change or Change()
    lines = ['개죽이 — 사람 승인이 필요합니다', '']
    lines.append('PR #%d · %s' % (number, head_sha[:12]))
    if title:
        lines.append(title[:120])
    if author:
        lines.append('올린 사람: %s (AI가 쓴 커밋)' % author)
    if reasons:
        lines.append(reasons[0])

    summary = _first_sentences(change.body) or list(change.commit_titles[:2])
    if summary:
        lines += ['', '■ 무엇을 하는 PR인가']
        lines += ['· %s' % item[:110] for item in summary]

    lines += ['', '■ 무엇이 바뀌나']
    lines.append('파일 %d개  +%d −%d' % (change.files, change.additions, change.deletions))
    kinds = buckets(change.paths)
    if kinds:
        lines.append('  '.join('%s %d' % (name, count) for name, count in kinds))
    for path in list(change.paths)[:5]:
        lines.append('· %s' % path)
    if len(change.paths) > 5:
        lines.append('· 외 %d개' % (len(change.paths) - 5))

    good, warn = signals(change)
    if good or warn:
        lines += ['', '■ 봐야 할 점']
        for item in warn:
            lines.append('⚠ %s' % item)
        for item in good:
            lines.append('○ %s' % item)

    lines += ['', '■ 검사 결과']
    if change.checks:
        for name, verdict in list(change.checks)[:5]:
            lines.append('· %s — %s' % (name, verdict))
    else:
        lines.append('· 이 저장소에는 아직 다른 자동 검사가 없습니다')
    lines.append('· 사람 승인만 남았습니다: %s' % (reasons[0] if reasons else ''))

    if evidence:
        lines += ['', 'AI가 쓴 커밋이라 판정된 이유:']
        for item in list(evidence)[:3]:
            lines.append('· %s' % item)
        if len(evidence) > 3:
            lines.append('· 외 %d건' % (len(evidence) - 3))

    lines += ['', url]
    return '\n'.join(lines)


def keyboard(number: int, head_sha: str) -> dict:
    return {'inline_keyboard': [[
        {'text': '✅ 승인', 'callback_data': button_data(APPROVE, number, head_sha)},
        {'text': '⏸ 보류', 'callback_data': button_data(HOLD, number, head_sha)},
    ]]}


class Telegram:
    def __init__(self, token: str):
        self.token = token

    def call(self, method: str, payload: dict, timeout: int = 40):
        data = json.dumps(payload).encode()
        req = urllib.request.Request(API % (self.token, method), data=data, method='POST')
        req.add_header('Content-Type', 'application/json')
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
        if not body.get('ok'):
            raise RuntimeError('telegram %s failed: %s' % (method, body.get('description')))
        return body.get('result')

    def send(self, chat_id: str, text: str, markup: dict) -> int:
        result = self.call('sendMessage', {
            'chat_id': chat_id, 'text': text,
            'reply_markup': markup, 'disable_web_page_preview': True,
        })
        return result['message_id']

    def updates(self, offset: Optional[int]) -> Tuple[list, Optional[int]]:
        payload = {'timeout': LONG_POLL_SECONDS, 'allowed_updates': ['callback_query']}
        if offset is not None:
            payload['offset'] = offset
        result = self.call('getUpdates', payload, timeout=LONG_POLL_SECONDS + 15) or []
        last = result[-1]['update_id'] if result else None
        return result, last

    def answer(self, callback_id: str, text: str):
        if not callback_id:
            return
        try:
            self.call('answerCallbackQuery', {'callback_query_id': callback_id,
                                              'text': text, 'show_alert': False})
        except (urllib.error.URLError, RuntimeError):
            pass          # the tap already happened; the toast is cosmetic

    def settle(self, chat_id, message_id, text: str):
        if chat_id is None or message_id is None:
            return
        try:
            self.call('editMessageText', {'chat_id': chat_id, 'message_id': message_id,
                                          'text': text,
                                          'disable_web_page_preview': True})
        except (urllib.error.URLError, RuntimeError):
            pass


def wait_for_tap(bot: Telegram, number: int, head_sha: str, owner_id: str,
                 deadline: float, clock=time.time) -> Optional[Tap]:
    """Drain updates until the owner taps, or until the window closes.

    Updates that are not ours are still confirmed, because leaving them queued
    would make the next run replay them.
    """
    offset: Optional[int] = None
    while clock() < deadline:
        try:
            batch, last = bot.updates(offset)
        except (urllib.error.URLError, RuntimeError):
            time.sleep(3)
            continue
        if last is not None:
            offset = last + 1
        for update in batch:
            tap = read_tap(update, number, head_sha, owner_id)
            if tap is None:
                continue
            if not tap.matches_pr:
                # Someone else's question. Say so on the button rather than
                # letting it look like this pull request was refused.
                bot.answer(tap.callback_id, tap.reason)
                continue
            return tap
    return None
