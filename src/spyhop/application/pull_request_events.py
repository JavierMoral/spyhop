"""What a pull request tells the user: the events that its state, its CI and
its comments make. Pure code: it reads no GitHub and writes nothing."""

from dataclasses import dataclass
from datetime import datetime

from spyhop.domain.models import (
    CommitStatus,
    PullComment,
    PullRequest,
    PullRequestEvent,
)

# The review states that tell something without a text.
_VERDICT_STATES = ("APPROVED", "CHANGES_REQUESTED")


@dataclass(frozen=True)
class Detection:
    events: list[PullRequestEvent]
    # merged, rejected or closed: the pull request has an end, and Spyhop
    # reads it no more. None: it can still change.
    verdict: str | None


class EventDetector:
    def __init__(self, user: str, bots: list[str], merged_comment: str) -> None:
        self._user = user
        self._bots = bots
        self._merged_comment = merged_comment.strip().lower()

    def detect(
        self,
        pull_request: PullRequest,
        comments: list[PullComment],
        status: CommitStatus | None,
        *,
        latest: bool,
        current: bool,
        head: str | None,
        now: datetime,
    ) -> Detection:
        """``latest``: the pull request is the newest of the worktree.
        ``current``: its head is the head of the newest. ``head``: the HEAD of
        the worktree. A pull request that is not current only informs: its
        events are never actionable."""

        events: list[PullRequestEvent] = []
        sha = pull_request.head_sha

        def add(kind: str, key: str, at: datetime, actionable: bool, **fields: str | None) -> None:
            events.append(
                PullRequestEvent(
                    kind, key, pull_request, at, actionable and current, current, **fields
                )
            )

        third = [comment for comment in comments if self._is_third(comment.author)]
        merged = next((comment for comment in third if self._is_merged(comment)), None)
        closer = pull_request.closed_by if self._is_third(pull_request.closed_by) else None
        verdict = None

        if merged:
            add("merged", pull_request.name, merged.created_at, False, author=merged.author)

            verdict = "merged"
        elif pull_request.state == "closed" and closer is None:
            verdict = "closed"
        elif pull_request.state == "closed":
            words = [comment for comment in comments if comment.author == closer]

            if words:
                add(
                    "rejected",
                    pull_request.name,
                    words[-1].created_at,
                    True,
                    author=closer,
                    text=words[-1].body,
                    location=words[-1].location,
                )

                verdict = "rejected"
            else:
                add("closed_pending", pull_request.name, now, False, author=closer)

        for comment in third:
            if comment.author == closer or self._is_merged(comment):
                continue

            if not comment.body.strip() and comment.state not in _VERDICT_STATES:
                continue

            add(
                "review",
                comment.id,
                comment.created_at,
                True,
                author=comment.author,
                text=comment.body.strip() or None,
                location=comment.location,
                detail=comment.state if comment.state in _VERDICT_STATES else None,
            )

        if pull_request.state == "open" and status is not None:
            for context in status.contexts:
                if context.state in ("failure", "error"):
                    add(
                        "ci_failed",
                        f"{sha}:{context.context}:{context.state}",
                        context.updated_at or now,
                        True,
                        detail=context.context,
                        text=self._failure_comment(comments, sha),
                    )

            if status.state == "success":
                add("ci_passed", f"{sha}:success", now, False)

        if latest and pull_request.state == "open":
            if pull_request.mergeable_state == "dirty":
                add("conflict", f"{pull_request.name}:{sha}", now, True)

            if head and head != sha:
                add("push_pending", head, now, False)

        return Detection(events, verdict)

    def _failure_comment(self, comments: list[PullComment], sha: str) -> str | None:
        """The last comment of a bot with :x: for the commit."""

        for comment in reversed(comments):
            if (
                comment.author in self._bots
                and ":x:" in comment.body
                and (sha in comment.body or sha[:7] in comment.body)
            ):
                return comment.body

        return None

    def _is_third(self, login: str | None) -> bool:
        return bool(login) and login != self._user and login not in self._bots

    def _is_merged(self, comment: PullComment) -> bool:
        return (
            bool(self._merged_comment)
            and comment.body.strip().lower().startswith(self._merged_comment)
            and self._is_third(comment.author)
        )
