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
from typing import Optional, Sequence, Tuple

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
        return Tap(action, False, 'meant for pull request #%s' % raw_number, **common)

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


def compose(number: int, head_sha: str, url: str, reasons: Sequence[str],
            evidence: Sequence[str], title: str = '', author: str = '') -> str:
    lines = ['*개죽이* — 사람 승인이 필요합니다', '']
    lines.append('PR #%d  `%s`' % (number, head_sha[:12]))
    if title:
        lines.append(title[:120])
    if author:
        lines.append('올린 사람: %s' % author)
    if reasons:
        lines.append(reasons[0])
    lines.append('')
    lines.append('AI가 쓴 커밋이라 판정된 이유:')
    for item in list(evidence)[:4]:
        lines.append('· %s' % item)
    if len(evidence) > 4:
        lines.append('· 외 %d건' % (len(evidence) - 4))
    lines.append('')
    lines.append(url)
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
            'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown',
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
                                          'text': text, 'parse_mode': 'Markdown',
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
            return tap
    return None
