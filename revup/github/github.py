import logging
from typing import Dict, List, Optional, Set, Tuple

from revup.forge import (
    MAX_COMMENTS_TO_QUERY,
    Forge,
    ForgeRepoInfo,
    PrComment,
    PrInfo,
    PrUpdate,
)
from revup.github.endpoint import GitHubEndpoint
from revup.github.graphql_client import GitHubGqlClient
from revup.github.graphql_client.create_pull_request import CreatePullRequest
from revup.github.graphql_client.get_assignable_users import GetAssignableUsers
from revup.github.graphql_client.get_label import GetLabel
from revup.github.graphql_client.get_pull_request import (
    GetPullRequestRepositoryPullRequestsNodesTimelineItemsNodesReviewRequestRemovedEventRequestedReviewerUser,  # pylint: disable=line-too-long
)
from revup.github.graphql_client.get_pull_request import (
    GetPullRequest,
    GetPullRequestRepositoryPullRequestsNodes,
    GetPullRequestRepositoryPullRequestsNodesLatestReviewsNodesAuthorUser,
    GetPullRequestRepositoryPullRequestsNodesReviewRequestsNodesRequestedReviewerTeam,
    GetPullRequestRepositoryPullRequestsNodesReviewRequestsNodesRequestedReviewerUser,
    GetPullRequestRepositoryPullRequestsNodesTimelineItemsNodesReviewRequestRemovedEvent,
    GetPullRequestRepositoryPullRequestsNodesTimelineItemsNodesUnassignedEvent,
    GetPullRequestRepositoryPullRequestsNodesTimelineItemsNodesUnassignedEventAssigneeUser,
)
from revup.github.graphql_client.get_team import GetTeam
from revup.github.graphql_client.input_types import (
    AddAssigneesToAssignableInput,
    AddCommentInput,
    AddLabelsToLabelableInput,
    ConvertPullRequestToDraftInput,
    CreatePullRequestInput,
    MarkPullRequestReadyForReviewInput,
    RequestReviewsInput,
    UpdateIssueCommentInput,
    UpdatePullRequestInput,
)
from revup.types import RevupForgeException

PrNode = GetPullRequestRepositoryPullRequestsNodes
ReviewAuthorUser = GetPullRequestRepositoryPullRequestsNodesLatestReviewsNodesAuthorUser
RequestedTeam = GetPullRequestRepositoryPullRequestsNodesReviewRequestsNodesRequestedReviewerTeam
RequestedUser = GetPullRequestRepositoryPullRequestsNodesReviewRequestsNodesRequestedReviewerUser
ReviewRemovedEvent = (
    GetPullRequestRepositoryPullRequestsNodesTimelineItemsNodesReviewRequestRemovedEvent
)
ReviewRemovedUser = GetPullRequestRepositoryPullRequestsNodesTimelineItemsNodesReviewRequestRemovedEventRequestedReviewerUser  # pylint: disable=line-too-long  # noqa: E501
UnassignedEvent = GetPullRequestRepositoryPullRequestsNodesTimelineItemsNodesUnassignedEvent
UnassignedUser = (
    GetPullRequestRepositoryPullRequestsNodesTimelineItemsNodesUnassignedEventAssigneeUser
)


class Github(Forge):
    def __init__(
        self,
        endpoint: GitHubEndpoint,
        repo_info: ForgeRepoInfo,
        fork_info: ForgeRepoInfo,
    ):
        self.endpoint = endpoint
        self.repo_info = repo_info
        self.fork_info = fork_info
        self.client = GitHubGqlClient(endpoint=endpoint)

    @property
    def repo_owner(self) -> str:
        return self.fork_info.owner

    @property
    def repo_name(self) -> str:
        return self.repo_info.name

    @property
    def is_fork(self) -> bool:
        return self.fork_info.owner != self.repo_info.owner

    async def close(self) -> None:
        await self.endpoint.close()

    def _parse_pr_node(self, node: PrNode, branch_name: str) -> PrInfo:
        pr_labels: Set[str] = set()
        pr_label_ids: Set[str] = set()
        reviewers: Set[str] = set()
        reviewer_ids: Set[str] = set()
        reviewer_teams: Set[str] = set()
        reviewer_team_ids: Set[str] = set()
        assignees: Set[str] = set()
        assignee_ids: Set[str] = set()

        if node.labels and node.labels.nodes:
            for label in node.labels.nodes:
                if label:
                    pr_labels.add(label.name)
                    pr_label_ids.add(label.id)

        if node.review_requests and node.review_requests.nodes:
            for rr in node.review_requests.nodes:
                if not rr or not rr.requested_reviewer:
                    continue
                requested = rr.requested_reviewer
                if isinstance(requested, RequestedTeam):
                    reviewer_teams.add(f"{requested.organization.login}/{requested.slug}")
                    reviewer_team_ids.add(requested.id)
                elif isinstance(requested, RequestedUser):
                    reviewers.add(requested.login)
                    reviewer_ids.add(requested.id)

        if node.latest_reviews and node.latest_reviews.nodes:
            for review in node.latest_reviews.nodes:
                if not review or review.viewer_did_author or not review.author:
                    continue
                if isinstance(review.author, ReviewAuthorUser):
                    reviewers.add(review.author.login)
                    reviewer_ids.add(review.author.id)

        if node.assignees.nodes:
            for user in node.assignees.nodes:
                if user:
                    assignees.add(user.login)
                    assignee_ids.add(user.id)

        headRefOid: Optional[str] = None
        if node.head_commit.nodes:
            head_node = node.head_commit.nodes[0]
            if head_node:
                headRefOid = head_node.commit.oid

        baseRefOid: Optional[str] = None
        if node.base_commit.nodes:
            base_node = node.base_commit.nodes[0]
            if base_node and base_node.commit.parents.nodes:
                parent = base_node.commit.parents.nodes[0]
                if parent:
                    baseRefOid = parent.oid

        comments = []
        if node.comments.nodes:
            for c in node.comments.nodes:
                if c:
                    comments.append(PrComment(c.body, c.id))

        removed_reviewers: Set[str] = set()
        removed_reviewer_ids: Set[str] = set()
        removed_assignees: Set[str] = set()
        removed_assignee_ids: Set[str] = set()
        if node.timeline_items.nodes:
            for event in node.timeline_items.nodes:
                if event is None:
                    continue
                if isinstance(event, ReviewRemovedEvent):
                    rr_user = event.requested_reviewer
                    if isinstance(rr_user, ReviewRemovedUser) and rr_user.login not in reviewers:
                        removed_reviewers.add(rr_user.login)
                        removed_reviewer_ids.add(rr_user.id)
                elif isinstance(event, UnassignedEvent):
                    assignee_node = event.assignee
                    if (
                        isinstance(assignee_node, UnassignedUser)
                        and assignee_node.login not in assignees
                    ):
                        removed_assignees.add(assignee_node.login)
                        removed_assignee_ids.add(assignee_node.id)

        return PrInfo(
            id=node.id,
            url=node.url,
            baseRef=node.base_ref_name,
            headRef=branch_name,
            baseRefOid=baseRefOid,
            headRefOid=headRefOid,
            body=node.body,
            title=node.title,
            reviewers=reviewers,
            reviewer_ids=reviewer_ids,
            reviewer_teams=reviewer_teams,
            reviewer_team_ids=reviewer_team_ids,
            assignees=assignees,
            assignee_ids=assignee_ids,
            labels=pr_labels,
            label_ids=pr_label_ids,
            removed_reviewers=removed_reviewers,
            removed_reviewer_ids=removed_reviewer_ids,
            removed_assignees=removed_assignees,
            removed_assignee_ids=removed_assignee_ids,
            is_draft=node.is_draft,
            state=node.state.value,
            comments=comments,
        )

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
        batch = self.client.batch()

        pr_indices = [
            batch.add(
                self.client.get_pull_request_query(),
                {
                    "owner": self.repo_info.owner,
                    "name": self.repo_info.name,
                    "headRefName": ref,
                    "maxComments": MAX_COMMENTS_TO_QUERY,
                },
                GetPullRequest,
            )
            for ref in head_refs
        ]

        user_indices = [
            batch.add(
                self.client.get_assignable_users_query(),
                {
                    "owner": self.repo_info.owner,
                    "name": self.repo_info.name,
                    "query": uid,
                },
                GetAssignableUsers,
            )
            for uid in user_ids
        ]

        label_indices = [
            batch.add(
                self.client.get_label_query(),
                {
                    "owner": self.repo_info.owner,
                    "name": self.repo_info.name,
                    "labelName": lbl,
                },
                GetLabel,
            )
            for lbl in labels
        ]

        team_indices = [
            batch.add(
                self.client.get_team_query(),
                {"org": org, "slug": slug},
                GetTeam,
            )
            for org, slug in teams
        ]

        await batch.flush()

        repo_id = ""
        prs: List[Optional[PrInfo]] = []
        for i, branch_name in enumerate(head_refs):
            pr_result: GetPullRequest = batch.get(pr_indices[i])  # type: ignore[assignment]
            if pr_result.repository is None:
                prs.append(None)
                continue
            repo_id = pr_result.repository.id
            pr_conn = pr_result.repository.pull_requests
            if pr_conn.nodes and len(pr_conn.nodes) == 1 and pr_conn.nodes[0]:
                prs.append(self._parse_pr_node(pr_conn.nodes[0], branch_name))
            else:
                prs.append(None)

        names_to_ids: Dict[str, str] = {}
        names_to_logins: Dict[str, str] = {}
        for i, user_id in enumerate(user_ids):
            user_result: GetAssignableUsers = batch.get(user_indices[i])  # type: ignore[assignment]
            if user_result.repository is None:
                continue
            users_conn = user_result.repository.assignable_users
            if not users_conn.nodes or len(users_conn.nodes) == 0:
                logging.warning("No matching user found for {}".format(user_id))
            else:
                if users_conn.total_count > len(users_conn.nodes):
                    logging.warning(
                        "Too many matching users found for {}, try being more"
                        " specific".format(user_id)
                    )
                shortest_name = users_conn.nodes[0].login if users_conn.nodes[0] else ""
                names_to_ids[user_id] = users_conn.nodes[0].id if users_conn.nodes[0] else ""
                found_match = False
                for user in users_conn.nodes:
                    if user is None:
                        continue
                    if len(user.login) <= len(shortest_name) and user.login.startswith(user_id):
                        shortest_name = user.login
                        names_to_ids[user_id] = user.id
                        names_to_logins[user_id] = user.login
                        found_match = True
                if not found_match:
                    logging.warning(
                        "Couldn't find a prefixed match for {}, going with {}"
                        " instead".format(user_id, shortest_name)
                    )

        labels_to_ids: Dict[str, str] = {}
        for i, label in enumerate(labels):
            label_result: GetLabel = batch.get(label_indices[i])  # type: ignore[assignment]
            if label_result.repository and label_result.repository.label:
                labels_to_ids[label] = label_result.repository.label.id
            else:
                logging.warning("Couldn't find an existing label named {}".format(label))

        teams_to_ids: Dict[str, str] = {}
        teams_to_members: Dict[str, Optional[Set[str]]] = {}
        for i, (org, slug) in enumerate(teams):
            team_result: GetTeam = batch.get(team_indices[i])  # type: ignore[assignment]
            if team_result.organization and team_result.organization.team:
                team_ref = f"{org}/{slug}"
                team = team_result.organization.team
                teams_to_ids[team_ref] = team.id
                if team.members.nodes is not None:
                    member_logins = {m.login for m in team.members.nodes if m is not None}
                    if team.members.total_count > len(team.members.nodes):
                        teams_to_members[team_ref] = None
                    else:
                        teams_to_members[team_ref] = member_logins
                else:
                    teams_to_members[team_ref] = None
            else:
                logging.warning("Couldn't find a team matching {}/{}".format(org, slug))

        return (
            repo_id,
            prs,
            names_to_ids,
            names_to_logins,
            labels_to_ids,
            teams_to_ids,
            teams_to_members,
        )

    async def create_pull_requests(self, repo_id: str, prs: List[PrInfo]) -> None:
        if not prs:
            return

        batch = self.client.batch()
        indices = []
        for pr in prs:
            headRef = (
                pr.headRef
                if self.fork_info.owner == self.repo_info.owner
                else f"{self.fork_info.owner}:{pr.headRef}"
            )
            idx = batch.add(
                self.client.create_pull_request_query(),
                {
                    "input": CreatePullRequestInput(
                        base_ref_name=pr.baseRef,
                        body=pr.body,
                        client_mutation_id="revup",
                        head_ref_name=headRef,
                        repository_id=repo_id,
                        title=pr.title,
                        draft=pr.is_draft,
                    )
                },
                CreatePullRequest,
            )
            indices.append(idx)

        await batch.flush()

        for i, pr in enumerate(prs):
            result: CreatePullRequest = batch.get(indices[i])  # type: ignore[assignment]
            if result.create_pull_request and result.create_pull_request.pull_request:
                pr.id = result.create_pull_request.pull_request.id
                pr.url = result.create_pull_request.pull_request.url

    async def update_pull_requests(self, prs: List[PrUpdate]) -> None:
        from revup.github.graphql_client.add_assignees import AddAssignees
        from revup.github.graphql_client.add_comment import AddComment
        from revup.github.graphql_client.add_labels import AddLabels
        from revup.github.graphql_client.convert_to_draft import ConvertToDraft
        from revup.github.graphql_client.mark_ready_for_review import MarkReadyForReview
        from revup.github.graphql_client.request_reviews import RequestReviews
        from revup.github.graphql_client.update_issue_comment import UpdateIssueComment
        from revup.github.graphql_client.update_pull_request import UpdatePullRequest

        batch = self.client.batch()

        for pr in prs:
            for c in pr.comments:
                if c.id:
                    batch.add(
                        self.client.update_issue_comment_query(),
                        {
                            "input": UpdateIssueCommentInput(
                                body=c.text,
                                client_mutation_id="revup",
                                id=c.id,
                            )
                        },
                        UpdateIssueComment,
                    )
                else:
                    batch.add(
                        self.client.add_comment_query(),
                        {
                            "input": AddCommentInput(
                                body=c.text,
                                client_mutation_id="revup",
                                subject_id=pr.id,
                            )
                        },
                        AddComment,
                    )

            batch.add(
                self.client.update_pull_request_query(),
                {
                    "input": UpdatePullRequestInput(
                        client_mutation_id="revup",
                        pull_request_id=pr.id,
                        base_ref_name=pr.baseRef,
                        body=pr.body,
                        title=pr.title,
                    )
                },
                UpdatePullRequest,
            )

            if pr.reviewer_ids or pr.reviewer_team_ids:
                batch.add(
                    self.client.request_reviews_query(),
                    {
                        "input": RequestReviewsInput(
                            user_ids=list(pr.reviewer_ids) if pr.reviewer_ids else None,
                            team_ids=(list(pr.reviewer_team_ids) if pr.reviewer_team_ids else None),
                            client_mutation_id="revup",
                            pull_request_id=pr.id,
                            union=True,
                        )
                    },
                    RequestReviews,
                )

            if pr.assignee_ids:
                batch.add(
                    self.client.add_assignees_query(),
                    {
                        "input": AddAssigneesToAssignableInput(
                            assignee_ids=list(pr.assignee_ids),
                            client_mutation_id="revup",
                            assignable_id=pr.id,
                        )
                    },
                    AddAssignees,
                )

            if pr.label_ids:
                batch.add(
                    self.client.add_labels_query(),
                    {
                        "input": AddLabelsToLabelableInput(
                            label_ids=list(pr.label_ids),
                            client_mutation_id="revup",
                            labelable_id=pr.id,
                        )
                    },
                    AddLabels,
                )

            if pr.is_draft is not None:
                if pr.is_draft:
                    batch.add(
                        self.client.convert_to_draft_query(),
                        {
                            "input": ConvertPullRequestToDraftInput(
                                client_mutation_id="revup",
                                pull_request_id=pr.id,
                            )
                        },
                        ConvertToDraft,
                    )
                else:
                    batch.add(
                        self.client.mark_ready_for_review_query(),
                        {
                            "input": MarkPullRequestReadyForReviewInput(
                                client_mutation_id="revup",
                                pull_request_id=pr.id,
                            )
                        },
                        MarkReadyForReview,
                    )

        if not batch.pending:
            return

        try:
            await batch.flush()
        except RevupForgeException as e:
            if "timeout" in e.message:
                logging.warning(
                    "Github update request timed out! Most likely this is a false"
                    " alarm and changes actually succeeded. You may want to rerun"
                    " this command to verify."
                )
            else:
                raise

    async def query_pr_by_number(self, owner: str, name: str, number: int) -> Tuple[str, str]:
        result = await self.client.get_pr_by_number(owner=owner, name=name, number=number)
        pr = result.repository.pull_request  # type: ignore[union-attr]
        return pr.head_ref_name, pr.base_ref_name  # type: ignore[union-attr]
