"""Adversarial regression tests for the agent-authored approval gate.

Run:  python3 tests/test_agent_approval.py

Every case is a way the gate could be talked into letting an agent merge its
own work.  The gate has no corpus and no network, so all of them run anywhere.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools.agent_approval import (  # noqa: E402
    Comment, Commit, Config, PullRequest, Review,
    PENDING, SKIPPED, SUCCESS, evaluate, render,
)

PASS = []
FAIL = []

HEAD = 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678'
OLD = '0f1e2d3c4b5a69788796a5b4c3d2e1f098765432'

AGENT = Commit(sha=HEAD, author_email='noreply@anthropic.com',
               committer_email='noreply@anthropic.com')
HUMAN = Commit(sha=HEAD, author_email='yazzang@example.com',
               committer_email='yazzang@example.com')

CFG = Config(required_approvals=2, protected_bases=('main',))


def case(name, ok):
    (PASS if ok else FAIL).append(name)
    print(('  ok   ' if ok else '  FAIL ') + name)


def sec(title):
    print()
    print(title)


def pr(**kw):
    base = dict(number=1, base_ref='main', head_sha=HEAD, author_login='yazzang',
                commits=[AGENT], write_access={'yazzang': True, 'curator': True,
                                               'maintainer': True})
    base.update(kw)
    return PullRequest(**base)


def approved(login, at='2026-08-06T00:00:00Z'):
    return Review(login=login, state='APPROVED', submitted_at=at)


sec('the five states the gate can be in')

case('an agent commit with one human approval still blocks',
     evaluate(pr(reviews=[approved('curator')]), CFG).state == PENDING)

d = evaluate(pr(reviews=[approved('curator'), approved('maintainer')]), CFG)
case('an agent commit with two human approvals passes', d.state == SUCCESS)
case('and it names who approved', d.approvers == ('curator', 'maintainer'))

d = evaluate(pr(comments=[Comment('curator', '/approve ' + OLD),
                          Comment('maintainer', '/approve ' + OLD)]), CFG)
case('approvals minted for a previous head do not survive a push',
     d.state == PENDING and d.approvers == ())
case('and the stale tokens are reported, not silently dropped',
     len(d.stale_tokens) == 2 and OLD[:12] in d.stale_tokens[0])

# A bot with write access is the realistic case: it can approve in the UI, so
# only the identity rule keeps its approval from counting.
d = evaluate(pr(reviews=[approved('claude[bot]'), approved('curator')],
                write_access={'claude[bot]': True, 'curator': True,
                              'maintainer': True, 'yazzang': True}), CFG)
case('an approving review by an agent with write access is not an approval',
     d.state == PENDING and d.approvers == ('curator',))
case('and the agent review is itself evidence the change is agent-authored',
     any('claude[bot]' in e for e in d.agent_evidence))

d = evaluate(pr(reviews=[approved('curator'), approved('maintainer')],
                sibling_prs_sharing_head=[42]), CFG)
case('success is withheld while another open PR shares the head commit',
     d.state == PENDING and '#42' in d.reasons[0])


sec('what the gate must not touch')

case('a pull request with no agent activity passes untouched',
     evaluate(pr(commits=[HUMAN]), CFG).state == SUCCESS)
case('a base branch the gate does not protect is skipped, not failed',
     evaluate(pr(base_ref='experiment'), CFG).state == SKIPPED)
case('an exempt path set lets a docs-only agent change through',
     evaluate(pr(changed_paths=['docs/a.md', 'docs/b.md']),
              Config(exempt_path_prefixes=('docs/',))).state == SUCCESS)
case('one non-exempt path is enough to require approval',
     evaluate(pr(changed_paths=['docs/a.md', 'hanpatch/cli.py']),
              Config(exempt_path_prefixes=('docs/',))).state == PENDING)
case('there is no branch-name exemption to abuse',
     not any('branch' in f for f in Config.__dataclass_fields__))


sec('who is allowed to count')

case('an approval from a login without write access does not count',
     evaluate(pr(reviews=[approved('drive_by'), approved('curator')]),
              CFG).approvers == ('curator',))
case('an approval from a login whose access is unknown does not count',
     evaluate(pr(reviews=[approved('stranger')], write_access={}),
              CFG).approvers == ())
case('an explicitly excluded approver never counts',
     evaluate(pr(reviews=[approved('curator'), approved('maintainer')]),
              Config(required_approvals=2, excluded_approvers=('curator',))
              ).state == PENDING)

d = evaluate(pr(comments=[Comment('yazzang', '/approve ' + HEAD)],
                reviews=[approved('curator')]), CFG)
case('the author can vouch for commits an agent pushed for them',
     d.state == SUCCESS and 'yazzang' in d.approvers)
case('but the author counts once, not twice',
     evaluate(pr(comments=[Comment('yazzang', '/approve ' + HEAD),
                           Comment('yazzang', '/approve ' + HEAD[:12])]),
              CFG).approvers == ('yazzang',))
case('a review and a token from the same person are still one approval',
     evaluate(pr(reviews=[approved('curator')],
                 comments=[Comment('curator', '/approve ' + HEAD)]),
              CFG).approvers == ('curator',))


sec('withdrawal and staleness')

case('a withdrawn approval stops counting',
     evaluate(pr(reviews=[approved('curator', '2026-08-06T00:00:00Z'),
                          Review('curator', 'CHANGES_REQUESTED',
                                 '2026-08-06T01:00:00Z')]),
              CFG).approvers == ())
case('re-approving after requesting changes counts again',
     evaluate(pr(reviews=[Review('curator', 'CHANGES_REQUESTED',
                                 '2026-08-06T00:00:00Z'),
                          approved('curator', '2026-08-06T01:00:00Z')]),
              CFG).approvers == ('curator',))
case('a short sha that prefixes the head is accepted',
     evaluate(pr(comments=[Comment('curator', '/approve ' + HEAD[:12])]),
              CFG).approvers == ('curator',))
case('eleven hex digits is not a token',
     evaluate(pr(comments=[Comment('curator', '/approve ' + HEAD[:11])]),
              CFG).approvers == ())
case('the word approve inside prose is not a token',
     evaluate(pr(comments=[Comment('curator', 'I would /approved this')]),
              CFG).approvers == ())


sec('failing closed')

many = [HUMAN] * 3
case('a commit list too long to read is treated as agent-authored',
     evaluate(pr(commits=many, commits_truncated=True), CFG).state == PENDING)
case('and the reason survives into the comment',
     'too long' in render(pr(commits=many, commits_truncated=True),
                          evaluate(pr(commits=many, commits_truncated=True), CFG),
                          CFG))
case('a pending decision blocks the merge',
     evaluate(pr(), CFG).blocks_merge is True)
case('a successful decision does not',
     evaluate(pr(commits=[HUMAN]), CFG).blocks_merge is False)


sec('the comment tells the reviewer what is missing')

body = render(pr(reviews=[approved('curator')]),
              evaluate(pr(reviews=[approved('curator')]), CFG), CFG)
case('it carries the marker that keeps it sticky',
     body.startswith('<!-- agent-approval-check -->'))
case('it states the count against the requirement', '1 of 2' in body)
case('it names the command with the current head',
     '/approve ' + HEAD[:12] in body)
case('it does not leak the command into a passing comment',
     '/approve' not in render(pr(commits=[HUMAN]),
                              evaluate(pr(commits=[HUMAN]), CFG), CFG))


sec('the module reaches nothing')

src = open(os.path.join(ROOT, 'tools', 'agent_approval.py')).read()
for banned in ('import urllib', 'import requests', 'import socket',
               'import subprocess', 'subprocess.', 'urlopen', 'open('):
    case('the decision core does not %s' % banned.strip('.').replace('import ', 'import '),
         banned not in src)

sec('the shipped identity list and the workflow that carries it')

import json  # noqa: E402
import tempfile  # noqa: E402
from tools.agent_approval_ci import CONFIG_FILE, load_config, _pr_number  # noqa: E402

shipped = load_config(ROOT)
case('the repository ships an identity list', os.path.exists(os.path.join(ROOT, CONFIG_FILE)))
case('it protects main', shipped.protected_bases == ('main',))
case('it requires at least one human', shipped.required_approvals >= 1)
case('it names the agents that write here',
     'claude[bot]' in shipped.agent_logins and 'github-actions[bot]' in shipped.agent_logins)
case('it exempts no path by default', shipped.exempt_path_prefixes == ())

_cfgroot = tempfile.mkdtemp(prefix='hanpatch-agentcfg-')
os.makedirs(os.path.join(_cfgroot, '.github'))
with open(os.path.join(_cfgroot, CONFIG_FILE), 'w') as _fh:
    json.dump({'required_approvals': 3, 'agent_logins': ['x[bot]']}, _fh)
_loaded = load_config(_cfgroot)
case('a declared count overrides the default', _loaded.required_approvals == 3)
case('an undeclared key keeps its default',
     _loaded.protected_bases == Config().protected_bases)
case('a missing file is the default, not a crash',
     load_config(tempfile.mkdtemp()).required_approvals == Config().required_approvals)

case('a pull_request event yields its number',
     _pr_number({'pull_request': {'number': 7}}) == 7)
case('a comment on a pull request yields its number',
     _pr_number({'issue': {'number': 9, 'pull_request': {'url': 'x'}}}) == 9)
case('a comment on a plain issue yields nothing to check',
     _pr_number({'issue': {'number': 9}}) is None)

_wf = open(os.path.join(ROOT, '.github', 'workflows', 'agent-approval.yml')).read()
case('the workflow never triggers on pull_request_review',
     'pull_request_review' not in _wf.split('jobs:')[0].replace(
         '# `pull_request_review` is deliberately absent: it runs from the merge ref,', ''))
case('the workflow does not check out the pull request head',
     'head.sha' not in _wf and 'head.ref' not in _wf)
case('the workflow asks for no write on contents',
     'contents: read' in _wf and 'contents: write' not in _wf)
case('the identity list is owned by owners, not by whoever opens a PR',
     '/.github/agent-identities.json' in open(
         os.path.join(ROOT, '.github', 'CODEOWNERS')).read())

sec('the fetch layer reads the fields it thinks it reads')

from tools.agent_approval_ci import fetch, publish  # noqa: E402


class FakeApi:
    """No socket.  Every route returns what GitHub documents it returns."""

    def __init__(self, repo='yazzang-homelab/hanpatch'):
        self.repo = repo
        self.posted = []
        self.patched = []

    def get(self, path):
        if path.endswith('/pulls/1'):
            return {'head': {'sha': HEAD}, 'base': {'ref': 'main'},
                    'user': {'login': 'yazzang'}}
        if '/collaborators/' in path:
            login = path.split('/collaborators/')[1].split('/')[0]
            return {'permission': 'write' if login in ('yazzang', 'curator') else 'read'}
        raise AssertionError('unexpected GET ' + path)

    def page(self, path):
        if '/pulls/1/commits' in path:
            return [{'sha': HEAD, 'commit': {
                'author': {'email': 'noreply@anthropic.com'},
                'committer': {'email': 'noreply@anthropic.com'}}}], False
        if '/pulls/1/reviews' in path:
            return [{'user': {'login': 'curator'}, 'state': 'APPROVED',
                     'submitted_at': '2026-08-06T00:00:00Z'}], False
        if '/issues/1/comments' in path:
            return [{'id': 5, 'user': {'login': 'github-actions[bot]'},
                     'body': '<!-- agent-approval-check -->\nan earlier round'},
                    {'id': 6, 'user': {'login': 'yazzang'},
                     'body': '/approve ' + HEAD[:12]}], False
        if '/pulls/1/files' in path:
            return [{'filename': 'hanpatch/cli.py'}], False
        if path.startswith('/repos/%s/pulls?state=open' % self.repo):
            return [{'number': 2, 'head': {'sha': HEAD}, 'base': {'ref': 'main'}}], False
        raise AssertionError('unexpected page ' + path)

    def post(self, path, body):
        self.posted.append((path, body))

    def patch(self, path, body):
        self.patched.append((path, body))


_api = FakeApi()
_fetched = fetch(_api, 1, Config(required_approvals=2))
case('the head sha comes from the pull request object, not a comment',
     _fetched.head_sha == HEAD)
case('the agent committer email survives two levels of nesting',
     _fetched.commits[0].committer_email == 'noreply@anthropic.com')
case('a review keeps its login, state and time',
     _fetched.reviews[0] == Review('curator', 'APPROVED', '2026-08-06T00:00:00Z'))
case('write access is probed per login and lowercased',
     _fetched.write_access == {'curator': True, 'github-actions[bot]': False,
                               'yazzang': True})
case('a sibling pull request on the same head is found',
     _fetched.sibling_prs_sharing_head == [2])
case('changed paths are not fetched when nothing is exempt',
     _fetched.changed_paths == [])
case('changed paths are fetched when something is exempt',
     fetch(FakeApi(), 1, Config(exempt_path_prefixes=('docs/',))).changed_paths
     == ['hanpatch/cli.py'])

_decision = evaluate(_fetched, Config(required_approvals=2))
case('the fetched pull request is withheld for the sibling, not merged',
     _decision.state == PENDING)

publish(_api, _fetched, _decision, render(_fetched, _decision, Config()))
case('a commit status is posted against the head sha',
     any('/statuses/' + HEAD in p for p, _ in _api.posted))
case('a pending decision posts a pending status',
     any(b.get('state') == PENDING for p, b in _api.posted if '/statuses/' in p))
case('an existing marked comment is edited rather than duplicated',
     _api.patched and '/issues/comments/5' in _api.patched[0][0]
     and not any('/issues/1/comments' in p for p, _ in _api.posted))

print()
print(f'{len(PASS)} passed, {len(FAIL)} failed')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
sys.exit(1 if FAIL else 0)
