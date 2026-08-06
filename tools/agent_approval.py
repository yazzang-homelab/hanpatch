"""Agent-authored PR approval gate.

A pull request that carries commits written by an agent needs human approvals
before it can merge.  A pull request written entirely by humans is untouched.

The decision core here is pure: it is handed facts that were already fetched
and returns a verdict.  It opens no socket, reads no file and shells out to
nothing, so the boundary assertion "the gate does not reach the network" holds
by construction and the whole rule set is testable from literals.

Two invariants are load-bearing and easy to lose:

  * The head sha is an input, never a claim read out of a comment.  The caller
    derives it with ``git rev-parse HEAD`` and passes it in; an ``/approve``
    comment can only ever *match* that value.  A comment naming some other sha
    is stale, and pushing a commit invalidates every approval token minted for
    the previous head.

  * An approving review from an agent is not an approval.  Agents may review,
    comment and argue; they may not be the reason a merge is allowed.  Every
    path that counts approvals filters agent identities out first.

Failure is closed.  A fact the caller could not establish - a login whose
write access is unknown, a commit list too long to read - counts against the
pull request, never for it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

# A commit list longer than this cannot be verified in one page, so the gate
# stops trying to prove the negative and assumes an agent wrote something.
MAX_VERIFIABLE_COMMITS = 100

# `/approve <sha>`: a short sha is allowed because that is what people paste,
# but it must be a prefix of the real head, and 12 hex digits is the shortest
# prefix that is not worth colliding.
_APPROVE_RE = re.compile(r'(?:^|\s)/approve\s+([0-9a-fA-F]{12,40})\b')

SUCCESS = 'success'
PENDING = 'pending'
SKIPPED = 'skipped'


@dataclass(frozen=True)
class Commit:
    sha: str
    author_email: str = ''
    committer_email: str = ''


@dataclass(frozen=True)
class Review:
    login: str
    state: str          # APPROVED / CHANGES_REQUESTED / COMMENTED / DISMISSED
    submitted_at: str   # ISO-8601, compared as a string on purpose


@dataclass(frozen=True)
class Comment:
    login: str
    body: str


@dataclass(frozen=True)
class PullRequest:
    """Everything the gate is allowed to know, fetched by the caller."""
    number: int
    base_ref: str
    head_sha: str
    author_login: str
    commits: Sequence[Commit] = ()
    commits_truncated: bool = False
    reviews: Sequence[Review] = ()
    comments: Sequence[Comment] = ()
    changed_paths: Sequence[str] = ()
    # login -> has write access on the BASE repository.  A login absent from
    # this map has unknown access and is treated as having none.
    write_access: Dict[str, bool] = field(default_factory=dict)
    # Numbers of other open pull requests against a protected base that point
    # at the same head commit.  A commit status attaches to a sha, not to a
    # pull request, so a green status here would unblock those too.
    sibling_prs_sharing_head: Sequence[int] = ()


@dataclass(frozen=True)
class Config:
    required_approvals: int = 2
    agent_emails: Tuple[str, ...] = ('noreply@anthropic.com',)
    agent_logins: Tuple[str, ...] = ('claude[bot]', 'claude-code[bot]')
    excluded_approvers: Tuple[str, ...] = ()
    protected_bases: Tuple[str, ...] = ('main',)
    # Paths whose changes need no human vouching.  Deliberately no branch-name
    # exemption exists: a branch name is chosen by the author of the change,
    # so it can carry no trust.
    exempt_path_prefixes: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Decision:
    state: str
    reasons: Tuple[str, ...]
    approvers: Tuple[str, ...] = ()
    stale_tokens: Tuple[str, ...] = ()
    agent_evidence: Tuple[str, ...] = ()

    @property
    def blocks_merge(self) -> bool:
        return self.state == PENDING


def _fold(values) -> set:
    return {v.strip().lower() for v in values if v and v.strip()}


def _is_agent_login(login: str, cfg: Config) -> bool:
    return login.strip().lower() in _fold(cfg.agent_logins)


def agent_evidence(pr: PullRequest, cfg: Config) -> List[str]:
    """Why this pull request counts as agent-authored, most durable first."""
    found: List[str] = []
    emails = _fold(cfg.agent_emails)

    if pr.commits_truncated or len(pr.commits) > MAX_VERIFIABLE_COMMITS:
        found.append('commit list too long to verify (%d+)' % len(pr.commits))

    for commit in pr.commits:
        for role, addr in (('author', commit.author_email),
                           ('committer', commit.committer_email)):
            if addr and addr.strip().lower() in emails:
                found.append('commit %s %s=%s' % (commit.sha[:12], role, addr))

    if _is_agent_login(pr.author_login, cfg):
        found.append('pull request opened by %s' % pr.author_login)

    for review in pr.reviews:
        if review.state.upper() == 'APPROVED' and _is_agent_login(review.login, cfg):
            found.append('approving review by %s' % review.login)

    return found


def _latest_review_state(pr: PullRequest) -> Dict[str, str]:
    """Per login, only the newest review counts; an APPROVED that was later
    withdrawn is not an approval."""
    newest: Dict[str, Review] = {}
    for review in pr.reviews:
        login = review.login.strip().lower()
        prior = newest.get(login)
        if prior is None or review.submitted_at >= prior.submitted_at:
            newest[login] = review
    return {login: r.state.upper() for login, r in newest.items()}


def _token_shas(pr: PullRequest) -> List[Tuple[str, str]]:
    """(login, sha) for every `/approve <sha>` written in a comment."""
    out: List[Tuple[str, str]] = []
    for comment in pr.comments:
        for sha in _APPROVE_RE.findall(comment.body or ''):
            out.append((comment.login.strip().lower(), sha.lower()))
    return out


def _may_approve(login: str, pr: PullRequest, cfg: Config) -> bool:
    if _is_agent_login(login, cfg):
        return False                                  # rule 16
    if login in _fold(cfg.excluded_approvers):
        return False
    return bool(pr.write_access.get(login, False))    # unknown access: no


def evaluate(pr: PullRequest, cfg: Optional[Config] = None) -> Decision:
    cfg = cfg or Config()
    head = pr.head_sha.strip().lower()

    if pr.base_ref not in cfg.protected_bases:
        return Decision(SKIPPED, ('base %r is not protected' % pr.base_ref,))

    evidence = agent_evidence(pr, cfg)
    if not evidence:
        return Decision(SUCCESS, ('no agent activity',))

    if cfg.exempt_path_prefixes and pr.changed_paths:
        if all(any(p.startswith(prefix) for prefix in cfg.exempt_path_prefixes)
               for p in pr.changed_paths):
            return Decision(SUCCESS, ('every changed path is exempt',),
                            agent_evidence=tuple(evidence))

    approvers: List[str] = []
    stale: List[str] = []

    for login, state in _latest_review_state(pr).items():
        if state == 'APPROVED' and _may_approve(login, pr, cfg):
            approvers.append(login)

    for login, sha in _token_shas(pr):
        if not (head.startswith(sha) or sha.startswith(head)):
            stale.append('%s approved %s, head is %s' % (login, sha[:12], head[:12]))
            continue
        if _may_approve(login, pr, cfg) and login not in approvers:
            approvers.append(login)

    approvers.sort()
    reasons: List[str] = []

    if pr.sibling_prs_sharing_head:
        reasons.append('another open pull request shares this head commit: %s'
                       % ', '.join('#%d' % n for n in pr.sibling_prs_sharing_head))
        return Decision(PENDING, tuple(reasons), tuple(approvers), tuple(stale),
                        tuple(evidence))

    if len(approvers) >= cfg.required_approvals:
        reasons.append('%d of %d human approvals' % (len(approvers), cfg.required_approvals))
        return Decision(SUCCESS, tuple(reasons), tuple(approvers), tuple(stale),
                        tuple(evidence))

    reasons.append('%d of %d human approvals' % (len(approvers), cfg.required_approvals))
    return Decision(PENDING, tuple(reasons), tuple(approvers), tuple(stale),
                    tuple(evidence))


def render(pr: PullRequest, decision: Decision, cfg: Optional[Config] = None) -> str:
    """The sticky comment.  It says what is missing, not merely that something is."""
    cfg = cfg or Config()
    lines = ['<!-- agent-approval-check -->']
    if decision.state == SKIPPED:
        lines.append('This check does not gate `%s`.' % pr.base_ref)
        return '\n'.join(lines)
    if decision.state == SUCCESS and not decision.agent_evidence:
        lines.append('No agent activity on this pull request.')
        return '\n'.join(lines)

    lines.append('**Agent-authored change — %d of %d human approvals.**'
                 % (len(decision.approvers), cfg.required_approvals))
    lines.append('')
    lines.append('Detected as agent-authored because:')
    for item in decision.agent_evidence:
        lines.append('- %s' % item)
    if decision.approvers:
        lines.append('')
        lines.append('Approved by: %s' % ', '.join(decision.approvers))
    if decision.stale_tokens:
        lines.append('')
        lines.append('Stale approvals (head moved):')
        for item in decision.stale_tokens:
            lines.append('- %s' % item)
    if decision.state == PENDING:
        lines.append('')
        lines.append('Approve with a review, or comment `/approve %s`.' % pr.head_sha[:12])
        for reason in decision.reasons:
            if reason.startswith('another open pull request'):
                lines.append('')
                lines.append('Withheld: %s' % reason)
    return '\n'.join(lines)
