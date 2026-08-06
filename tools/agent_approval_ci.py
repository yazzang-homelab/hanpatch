"""GitHub side of the agent-authored approval gate.

This module does the talking: it fetches the facts, hands them to the pure
decision core in ``tools.agent_approval``, then writes a commit status and one
sticky comment.  Keeping the two apart is deliberate - the rules are testable
from literals and the network lives in exactly one file.

Run from a workflow:

    python3 tools/agent_approval_ci.py

Environment:
    GITHUB_TOKEN        needs statuses:write and pull-requests:write
    GITHUB_REPOSITORY   owner/name
    GITHUB_EVENT_PATH   the event payload, used only to find the PR number
    GITHUB_API_URL      optional, defaults to https://api.github.com

The head sha is taken from the pull request object the API returns, never from
a comment.  A comment can only match that value; that is what makes an
``/approve <sha>`` token die when someone pushes.

Failure is closed.  Anything unexpected leaves a `pending` status behind and
exits non-zero, so the required check stays red rather than absent.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.agent_approval import (  # noqa: E402
    Comment, Commit, Config, PullRequest, Review,
    PENDING, SKIPPED, SUCCESS, evaluate, render,
)

CONTEXT = 'agent-approval-check'
MARKER = '<!-- agent-approval-check -->'
CONFIG_FILE = '.github/agent-identities.json'
WRITE_PERMISSIONS = ('admin', 'maintain', 'write')


class Api:
    def __init__(self, token, repo, base=None):
        self.token = token
        self.repo = repo
        self.base = (base or 'https://api.github.com').rstrip('/')

    def _request(self, method, path, body=None):
        url = path if path.startswith('http') else '%s%s' % (self.base, path)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header('Authorization', 'Bearer %s' % self.token)
        req.add_header('Accept', 'application/vnd.github+json')
        req.add_header('X-GitHub-Api-Version', '2022-11-28')
        if data is not None:
            req.add_header('Content-Type', 'application/json')
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read()
            link = resp.headers.get('Link', '')
        return (json.loads(payload) if payload else None), link

    def get(self, path):
        return self._request('GET', path)[0]

    def post(self, path, body):
        return self._request('POST', path, body)[0]

    def patch(self, path, body):
        return self._request('PATCH', path, body)[0]

    def page(self, path):
        """One page of up to 100, plus whether more pages exist."""
        sep = '&' if '?' in path else '?'
        items, link = self._request('GET', '%s%sper_page=100' % (path, sep))
        return items or [], 'rel="next"' in link


def _pr_number(event):
    if 'pull_request' in event and isinstance(event['pull_request'], dict):
        return event['pull_request'].get('number')
    issue = event.get('issue') or {}
    if issue.get('pull_request'):
        return issue.get('number')
    return None


def load_config(root='.'):
    """Identities come from the BASE branch checkout, never from the PR head.

    A pull request that could edit this file could exempt itself.
    """
    path = os.path.join(root, CONFIG_FILE)
    defaults = Config()
    if not os.path.exists(path):
        return defaults
    with open(path, encoding='utf-8') as fh:
        raw = json.load(fh)
    return Config(
        required_approvals=int(raw.get('required_approvals', defaults.required_approvals)),
        agent_emails=tuple(raw.get('agent_emails', defaults.agent_emails)),
        agent_logins=tuple(raw.get('agent_logins', defaults.agent_logins)),
        excluded_approvers=tuple(raw.get('excluded_approvers', ())),
        protected_bases=tuple(raw.get('protected_bases', defaults.protected_bases)),
        exempt_path_prefixes=tuple(raw.get('exempt_path_prefixes', ())),
    )


def fetch(api, number, cfg):
    repo = api.repo
    pr = api.get('/repos/%s/pulls/%d' % (repo, number))
    head_sha = pr['head']['sha']
    base_ref = pr['base']['ref']
    author = (pr.get('user') or {}).get('login', '')

    raw_commits, more_commits = api.page('/repos/%s/pulls/%d/commits' % (repo, number))
    commits = [Commit(sha=c.get('sha', ''),
                      author_email=((c.get('commit') or {}).get('author') or {}).get('email', ''),
                      committer_email=((c.get('commit') or {}).get('committer') or {}).get('email', ''))
               for c in raw_commits]

    raw_reviews, _ = api.page('/repos/%s/pulls/%d/reviews' % (repo, number))
    reviews = [Review(login=(r.get('user') or {}).get('login', ''),
                      state=r.get('state', ''),
                      submitted_at=r.get('submitted_at') or '')
               for r in raw_reviews if (r.get('user') or {}).get('login')]

    raw_comments, _ = api.page('/repos/%s/issues/%d/comments' % (repo, number))
    comments = [Comment(login=(c.get('user') or {}).get('login', ''),
                        body=c.get('body') or '')
                for c in raw_comments if (c.get('user') or {}).get('login')]

    changed_paths = []
    if cfg.exempt_path_prefixes:
        raw_files, _ = api.page('/repos/%s/pulls/%d/files' % (repo, number))
        changed_paths = [f.get('filename', '') for f in raw_files]

    # Only logins that could possibly matter are probed, and an unreadable
    # permission is recorded as no permission.
    candidates = {r.login for r in reviews} | {c.login for c in comments} | {author}
    write_access = {}
    for login in sorted(l for l in candidates if l):
        try:
            perm = api.get('/repos/%s/collaborators/%s/permission' % (repo, login))
            write_access[login.lower()] = perm.get('permission') in WRITE_PERMISSIONS
        except urllib.error.HTTPError:
            write_access[login.lower()] = False

    siblings = []
    open_prs, _ = api.page('/repos/%s/pulls?state=open' % repo)
    for other in open_prs:
        if other.get('number') == number:
            continue
        if (other.get('head') or {}).get('sha') != head_sha:
            continue
        if (other.get('base') or {}).get('ref') in cfg.protected_bases:
            siblings.append(other['number'])

    return PullRequest(
        number=number,
        base_ref=base_ref,
        head_sha=head_sha,
        author_login=author,
        commits=commits,
        commits_truncated=more_commits,
        reviews=reviews,
        comments=comments,
        changed_paths=changed_paths,
        write_access=write_access,
        sibling_prs_sharing_head=siblings,
    )


def publish(api, pr, decision, body):
    state = SUCCESS if decision.state == SUCCESS else PENDING
    description = decision.reasons[0][:140] if decision.reasons else CONTEXT
    api.post('/repos/%s/statuses/%s' % (api.repo, pr.head_sha),
             {'state': state, 'context': CONTEXT, 'description': description})

    existing = None
    comments, _ = api.page('/repos/%s/issues/%d/comments' % (api.repo, pr.number))
    for comment in comments:
        if MARKER in (comment.get('body') or ''):
            existing = comment['id']
            break
    if existing:
        api.patch('/repos/%s/issues/comments/%d' % (api.repo, existing), {'body': body})
    else:
        api.post('/repos/%s/issues/%d/comments' % (api.repo, pr.number), {'body': body})


def describe_change(api, number, head_sha):
    """The facts the owner needs to judge a pull request from a phone.

    Counted, never summarised by a model: a language model writing the summary
    would put an unverifiable authority in the exact place this project has
    decided to keep human.
    """
    from tools.telegram_approval import Change

    pull = api.get('/repos/%s/pulls/%d' % (api.repo, number)) or {}
    files, _ = api.page('/repos/%s/pulls/%d/files' % (api.repo, number))
    commits, _ = api.page('/repos/%s/pulls/%d/commits' % (api.repo, number))

    checks = []
    for status in (api.get('/repos/%s/commits/%s/status' % (api.repo, head_sha))
                   or {}).get('statuses', []):
        if status.get('context') != CONTEXT:
            checks.append((status.get('context', ''), status.get('state', '')))
    runs = api.get('/repos/%s/commits/%s/check-runs' % (api.repo, head_sha)) or {}
    for run in runs.get('check_runs', []):
        checks.append((run.get('name', ''), run.get('conclusion') or run.get('status', '')))

    return pull.get('title', ''), Change(
        files=len(files),
        additions=sum(f.get('additions', 0) for f in files),
        deletions=sum(f.get('deletions', 0) for f in files),
        paths=[f.get('filename', '') for f in files],
        commit_titles=[((c.get('commit') or {}).get('message') or '').splitlines()[0]
                       for c in commits if (c.get('commit') or {}).get('message')],
        body=pull.get('body') or '',
        checks=checks,
    )



def ask_owner(api, pr, cfg, decision):
    """Put the question on the owner's phone and wait for the tap.

    A tap does not approve anything on its own: it posts the ordinary
    `/approve <sha>` comment under the owner's own credential, and the gate is
    then re-derived from the pull request as if the owner had typed it. Without
    that credential the tap is refused out loud, because a comment written by
    the workflow's own bot identity would never count - and a notification that
    silently does nothing is worse than no notification.
    """
    bot_token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID', '')
    if not (bot_token and chat_id):
        print('telegram not configured; leaving the pull request pending')
        return decision

    from tools.telegram_approval import (  # imported late: the gate must not need it
        DEFAULT_WAIT_SECONDS, Telegram, compose, keyboard, should_ask, wait_for_tap,
    )

    policy = os.environ.get('TELEGRAM_NOTIFY_AUTHORS', 'write')
    allowed, why = should_ask(pr.author_login,
                              pr.write_access.get(pr.author_login.lower(), False),
                              policy)
    if not allowed:
        print('not asking: %s' % why)
        return decision

    url = 'https://github.com/%s/pull/%d' % (api.repo, pr.number)
    title, change = describe_change(api, pr.number, pr.head_sha)
    bot = Telegram(bot_token)
    message_id = bot.send(chat_id, compose(pr.number, pr.head_sha, url,
                                           decision.reasons, decision.agent_evidence,
                                           title=title, author=pr.author_login,
                                           change=change),
                          keyboard(pr.number, pr.head_sha))
    print('asked the owner on telegram (message %s)' % message_id)

    wait = int(os.environ.get('TELEGRAM_WAIT_SECONDS', DEFAULT_WAIT_SECONDS))
    tap = wait_for_tap(bot, pr.number, pr.head_sha, chat_id, time.time() + wait)

    if tap is None:
        bot.settle(chat_id, message_id,
                   '개죽이 — PR #%d 응답 없음. GitHub에서 직접 승인하십시오.\n%s'
                   % (pr.number, url))
        print('no tap within %ds; still pending' % wait)
        return decision

    if not tap.accepted:
        bot.answer(tap.callback_id, tap.reason)
        bot.settle(tap.chat_id, tap.message_id,
                   '개죽이 — PR #%d 승인되지 않음: %s\n%s' % (pr.number, tap.reason, url))
        print('tap refused: %s' % tap.reason)
        return decision

    owner_token = os.environ.get('OWNER_TOKEN', '')
    if not owner_token:
        bot.answer(tap.callback_id, 'OWNER_TOKEN 시크릿이 없어 승인을 기록할 수 없습니다')
        bot.settle(tap.chat_id, tap.message_id,
                   '개죽이 — PR #%d 승인 기록 실패: OWNER_TOKEN 시크릿 없음.\n%s'
                   % (pr.number, url))
        print('OWNER_TOKEN is missing; a tap cannot be recorded')
        return decision

    owner_api = Api(owner_token, api.repo, os.environ.get('GITHUB_API_URL'))
    owner_api.post('/repos/%s/issues/%d/comments' % (api.repo, pr.number),
                   {'body': '/approve %s' % pr.head_sha})
    bot.answer(tap.callback_id, '승인했습니다')

    fresh = fetch(api, pr.number, cfg)
    verdict = evaluate(fresh, cfg)
    bot.settle(tap.chat_id, tap.message_id,
               '개죽이 — PR #%d %s\n%s'
               % (pr.number, '승인 완료' if verdict.state == SUCCESS else '아직 대기', url))
    publish(api, fresh, verdict, render(fresh, verdict, cfg))
    return verdict


def main():
    token = os.environ.get('GITHUB_TOKEN', '')
    repo = os.environ.get('GITHUB_REPOSITORY', '')
    event_path = os.environ.get('GITHUB_EVENT_PATH', '')
    if not (token and repo and event_path):
        print('GITHUB_TOKEN, GITHUB_REPOSITORY and GITHUB_EVENT_PATH are required')
        return 2

    with open(event_path, encoding='utf-8') as fh:
        event = json.load(fh)
    number = _pr_number(event)
    if number is None:
        print('not a pull request event; nothing to check')
        return 0

    api = Api(token, repo, os.environ.get('GITHUB_API_URL'))
    cfg = load_config()
    pr = fetch(api, number, cfg)
    decision = evaluate(pr, cfg)

    if decision.state == SKIPPED:
        # A status here would attach to a sha that a protected-base pull
        # request may also point at, so nothing is published.
        print('skipped: %s' % '; '.join(decision.reasons))
        return 0

    publish(api, pr, decision, render(pr, decision, cfg))
    if decision.state == PENDING:
        decision = ask_owner(api, pr, cfg, decision)
    print('%s: %s' % (decision.state, '; '.join(decision.reasons)))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:                      # fail closed, loudly
        print('agent-approval-check failed: %r' % (exc,))
        sys.exit(1)
