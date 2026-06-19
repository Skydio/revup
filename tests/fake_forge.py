from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from revup.forge import Forge, PrInfo, PrUpdate


@dataclass
class FakeForge(Forge):
    """
    In-memory forge implementation for testing the full upload pipeline.
    Tracks all PRs created/updated and simulates query responses.
    Performs consistency checks that mirror real forge constraints.
    """

    _owner: str = "testowner"
    _name: str = "testrepo"
    _fork_owner: str = ""
    _repo_id: str = "repo_123"

    # Registered users: query string -> (node_id, full_login)
    users: Dict[str, Tuple[str, str]] = field(default_factory=dict)

    # Registered labels: name -> node_id
    labels: Dict[str, str] = field(default_factory=dict)

    # Registered teams: "org/slug" -> (node_id, member_logins)
    teams: Dict[str, Tuple[str, Set[str]]] = field(default_factory=dict)

    # PRs that exist on the forge, keyed by headRef
    prs: Dict[str, PrInfo] = field(default_factory=dict)

    # All known PR IDs (including closed/merged) to prevent reuse
    _all_pr_ids: Set[str] = field(default_factory=set)

    # Tracking of operations performed
    created_prs: List[PrInfo] = field(default_factory=list)
    updated_prs: List[PrUpdate] = field(default_factory=list)

    _next_pr_id: int = field(default=1)

    @property
    def repo_owner(self) -> str:
        return self._fork_owner or self._owner

    @property
    def repo_name(self) -> str:
        return self._name

    @property
    def is_fork(self) -> bool:
        return bool(self._fork_owner) and self._fork_owner != self._owner

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
        assert head_refs, "query_everything called with no head_refs"
        assert len(head_refs) == len(set(head_refs)), "duplicate head_refs in query"
        assert len(user_ids) == len(set(user_ids)), "duplicate user_ids in query"
        assert len(labels) == len(set(labels)), "duplicate labels in query"

        pr_results: List[Optional[PrInfo]] = []
        for ref in head_refs:
            assert ref, "empty head_ref in query"
            pr_results.append(self.prs.get(ref))

        user_id_map = {}
        user_login_map = {}
        for uid in user_ids:
            assert uid, "empty user_id in query"
            if uid in self.users:
                node_id, login = self.users[uid]
                user_id_map[uid] = node_id
                user_login_map[uid] = login

        label_id_map = {}
        for label in labels:
            assert label, "empty label in query"
            if label in self.labels:
                label_id_map[label] = self.labels[label]

        team_id_map: Dict[str, str] = {}
        team_members_map: Dict[str, Optional[Set[str]]] = {}
        for org, slug in teams:
            assert org and slug, "empty org or slug in team query"
            ref = f"{org}/{slug}"
            if ref in self.teams:
                tid, members = self.teams[ref]
                team_id_map[ref] = tid
                team_members_map[ref] = members

        return (
            self._repo_id,
            pr_results,
            user_id_map,
            user_login_map,
            label_id_map,
            team_id_map,
            team_members_map,
        )

    async def create_pull_requests(self, repo_id: str, prs: List[PrInfo]) -> None:
        assert repo_id == self._repo_id, f"wrong repo_id: {repo_id}"
        assert prs, "create_pull_requests called with empty list"

        for pr in prs:
            assert pr.headRef, "PR missing headRef"
            assert pr.baseRef, "PR missing baseRef"
            assert pr.title, "PR missing title"
            assert pr.headRef != pr.baseRef, f"PR headRef and baseRef are the same: {pr.headRef}"

            # Cannot create a PR if one is already OPEN on the same branch
            existing = self.prs.get(pr.headRef)
            if existing is not None and existing.state == "OPEN":
                raise RuntimeError(
                    f"Cannot create PR: an OPEN PR already exists for branch {pr.headRef}"
                )

            # PR should not already have an ID assigned
            assert not pr.id, f"PR already has id {pr.id} before creation"

            pr.id = f"pr_{self._next_pr_id}"
            pr.url = f"https://test.com/{self._owner}/{self._name}/pull/{self._next_pr_id}"
            pr.state = "OPEN"
            self._next_pr_id += 1
            self._all_pr_ids.add(pr.id)
            self.prs[pr.headRef] = pr
            self.created_prs.append(pr)

    async def update_pull_requests(self, prs: List[PrUpdate]) -> None:
        assert prs, "update_pull_requests called with empty list"

        seen_ids: Set[str] = set()
        for update in prs:
            assert update.id, "PrUpdate missing id"
            assert update.id not in seen_ids, f"duplicate update for PR {update.id}"
            seen_ids.add(update.id)

            # Find the target PR
            target_pr = None
            for pr in self.prs.values():
                if pr.id == update.id:
                    target_pr = pr
                    break
            assert target_pr is not None, f"update targets unknown PR id {update.id}"
            assert target_pr.state != "MERGED", f"cannot update merged PR {update.id}"

            # Validate the update has at least one meaningful change
            has_change = (
                update.baseRef is not None
                or update.body is not None
                or update.title is not None
                or update.is_draft is not None
                or update.reviewer_ids
                or update.reviewer_team_ids
                or update.assignee_ids
                or update.label_ids
                or update.comments
            )
            assert has_change, f"update for PR {update.id} has no changes"

            # baseRef must differ from current if specified
            if update.baseRef is not None:
                assert update.baseRef, "baseRef cannot be empty string"
                assert update.baseRef != target_pr.headRef, (
                    f"cannot set baseRef to headRef ({update.baseRef})"
                )

            # title cannot be empty if specified
            if update.title is not None:
                assert update.title, "title cannot be set to empty string"

            # Verify reviewer/assignee IDs reference known node IDs
            all_known_user_ids = {nid for nid, _ in self.users.values()}
            all_known_team_ids = {tid for tid, _ in self.teams.values()}
            all_known_label_ids = set(self.labels.values())

            for rid in update.reviewer_ids:
                assert rid in all_known_user_ids, f"reviewer_id {rid} not a known user node id"
            for tid in update.reviewer_team_ids:
                assert tid in all_known_team_ids, f"reviewer_team_id {tid} not a known team node id"
            for aid in update.assignee_ids:
                assert aid in all_known_user_ids, f"assignee_id {aid} not a known user node id"
            for lid in update.label_ids:
                assert lid in all_known_label_ids, f"label_id {lid} not a known label node id"

            # Apply the update
            self.updated_prs.append(update)
            if update.baseRef is not None:
                target_pr.baseRef = update.baseRef
            if update.body is not None:
                target_pr.body = update.body
            if update.title is not None:
                target_pr.title = update.title
            if update.is_draft is not None:
                target_pr.is_draft = update.is_draft
            target_pr.reviewer_ids |= update.reviewer_ids
            target_pr.reviewer_team_ids |= update.reviewer_team_ids
            target_pr.assignee_ids |= update.assignee_ids
            target_pr.label_ids |= update.label_ids

    async def query_pr_by_number(self, owner: str, name: str, number: int) -> Tuple[str, str]:
        assert owner, "owner cannot be empty"
        assert name, "name cannot be empty"
        assert number > 0, f"invalid PR number: {number}"

        for pr in self.prs.values():
            if pr.url and pr.url.endswith(f"/pull/{number}"):
                return pr.headRef, pr.baseRef
        raise RuntimeError(f"PR #{number} not found in {owner}/{name}")

    async def close(self) -> None:
        pass
