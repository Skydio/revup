from __future__ import annotations

from abc import ABCMeta, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ForgeRepoInfo:
    name: str = ""
    owner: str = ""


@dataclass
class PullRequestParams:
    forge_url: str
    owner: str
    name: str
    number: int


@dataclass
class PrComment:
    text: str = ""
    id: str | None = None


@dataclass
class PrInfo:
    baseRef: str
    headRef: str
    headRefOid: str | None
    body: str
    title: str
    numCommits: int = 0
    id: str = ""
    url: str = ""
    state: str = ""
    reviewers: set[str] = field(default_factory=set)
    reviewer_ids: set[str] = field(default_factory=set)
    reviewer_teams: set[str] = field(default_factory=set)
    reviewer_team_ids: set[str] = field(default_factory=set)
    assignees: set[str] = field(default_factory=set)
    assignee_ids: set[str] = field(default_factory=set)
    labels: set[str] = field(default_factory=set)
    label_ids: set[str] = field(default_factory=set)
    removed_reviewers: set[str] = field(default_factory=set)
    removed_reviewer_ids: set[str] = field(default_factory=set)
    removed_assignees: set[str] = field(default_factory=set)
    removed_assignee_ids: set[str] = field(default_factory=set)
    is_draft: bool = False
    comments: list[PrComment] = field(default_factory=list)


@dataclass
class PrUpdate:
    baseRef: str | None = None
    body: str | None = None
    title: str | None = None
    id: str = ""
    reviewer_ids: set[str] = field(default_factory=set)
    reviewer_team_ids: set[str] = field(default_factory=set)
    assignee_ids: set[str] = field(default_factory=set)
    label_ids: set[str] = field(default_factory=set)
    is_draft: bool | None = None
    comments: list[PrComment] = field(default_factory=list)


MAX_COMMENTS_TO_QUERY = 3


class Forge(metaclass=ABCMeta):
    @property
    def name(self) -> str:
        return type(self).__name__.lower()

    @property
    @abstractmethod
    def repo_owner(self) -> str:
        """Owner of the fork remote (or repo remote if no fork)."""

    @property
    @abstractmethod
    def repo_name(self) -> str:
        """Name of the repository."""

    @property
    @abstractmethod
    def is_fork(self) -> bool:
        """True if the fork remote points to a different owner than the repo remote."""

    @abstractmethod
    async def query_everything(
        self,
        head_refs: list[str],
        user_ids: list[str],
        labels: list[str],
        teams: list[tuple[str, str]],
    ) -> tuple[
        str,
        list[PrInfo | None],
        dict[str, str],
        dict[str, str],
        dict[str, str],
        dict[str, str],
        dict[str, set[str] | None],
    ]:
        """
        Query all needed info in one request. Returns:
        - Repository node id
        - List of pull requests (None if not found for that ref)
        - Dict of user query strings to node ids
        - Dict of user query strings to full login names
        - Dict of label names to node ids
        - Dict of team refs ("org/slug") to node ids
        - Dict of team refs to member logins (None if membership unknown)
        """

    @abstractmethod
    async def create_pull_requests(self, repo_id: str, prs: list[PrInfo]) -> None:
        """Create pull requests. Modifies prs in-place to set id and url."""

    @abstractmethod
    async def update_pull_requests(self, prs: list[PrUpdate]) -> None:
        """Update existing pull requests."""

    @abstractmethod
    async def query_pr_by_number(self, owner: str, name: str, number: int) -> tuple[str, str]:
        """Query a pull request by number and return (headRefName, baseRefName)."""

    @abstractmethod
    async def close(self) -> None:
        """Clean up any connections."""
