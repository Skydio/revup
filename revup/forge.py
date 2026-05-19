from abc import ABCMeta, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple


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
    id: Optional[str] = None


@dataclass
class PrInfo:
    baseRef: str
    headRef: str
    baseRefOid: Optional[str]
    headRefOid: Optional[str]
    body: str
    title: str
    id: str = ""
    url: str = ""
    state: str = ""
    reviewers: Set[str] = field(default_factory=set)
    reviewer_ids: Set[str] = field(default_factory=set)
    reviewer_teams: Set[str] = field(default_factory=set)
    reviewer_team_ids: Set[str] = field(default_factory=set)
    assignees: Set[str] = field(default_factory=set)
    assignee_ids: Set[str] = field(default_factory=set)
    labels: Set[str] = field(default_factory=set)
    label_ids: Set[str] = field(default_factory=set)
    removed_reviewers: Set[str] = field(default_factory=set)
    removed_reviewer_ids: Set[str] = field(default_factory=set)
    removed_assignees: Set[str] = field(default_factory=set)
    removed_assignee_ids: Set[str] = field(default_factory=set)
    is_draft: bool = False
    comments: List[PrComment] = field(default_factory=list)


@dataclass
class PrUpdate:
    baseRef: Optional[str] = None
    body: Optional[str] = None
    title: Optional[str] = None
    id: str = ""
    reviewer_ids: Set[str] = field(default_factory=set)
    reviewer_team_ids: Set[str] = field(default_factory=set)
    assignee_ids: Set[str] = field(default_factory=set)
    label_ids: Set[str] = field(default_factory=set)
    is_draft: Optional[bool] = None
    comments: List[PrComment] = field(default_factory=list)


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
        head_refs: List[str],
        user_ids: List[str],
        labels: List[str],
        teams: List[Tuple[str, str]],
    ) -> Tuple[
        str,
        List[Optional[PrInfo]],
        Dict[str, str],
        Dict[str, str],
        Dict[str, str],
        Dict[str, str],
        Dict[str, Optional[Set[str]]],
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
    async def create_pull_requests(self, repo_id: str, prs: List[PrInfo]) -> None:
        """Create pull requests. Modifies prs in-place to set id and url."""

    @abstractmethod
    async def update_pull_requests(self, prs: List[PrUpdate]) -> None:
        """Update existing pull requests."""

    @abstractmethod
    async def query_pr_by_number(self, owner: str, name: str, number: int) -> Tuple[str, str]:
        """Query a pull request by number and return (headRefName, baseRefName)."""

    @abstractmethod
    async def close(self) -> None:
        """Clean up any connections."""
