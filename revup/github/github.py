import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from revup.forge import (
    MAX_COMMENTS_TO_QUERY,
    Forge,
    ForgeRepoInfo,
    PrComment,
    PrInfo,
    PrUpdate,
)
from revup.github.endpoint import GitHubEndpoint
from revup.github.graphql import GraphqlQuery, QueryGroup
from revup.types import RevupForgeException

PR_FRAGMENT = f"""
        fragment PrResult on PullRequestConnection {{
            nodes {{
                id
                state
                url
                baseRefName
                body
                title
                isDraft
                baseCommit: commits(first: 1) {{
                    nodes {{
                        commit {{
                            parents (first: 1) {{
                                nodes {{
                                    oid
                                }}
                            }}
                        }}
                    }}
                }}
                headCommit: commits(last: 1) {{
                    nodes {{
                        commit {{
                            oid
                        }}
                    }}
                }}
                reviewRequests (first: 25) {{
                    nodes {{
                        requestedReviewer {{
                            ... on User {{
                                login
                                id
                            }}
                            ... on Team {{
                                slug
                                id
                                organization {{
                                    login
                                }}
                            }}
                        }}
                    }}
                }}
                timelineItems(
                    itemTypes: [REVIEW_REQUEST_REMOVED_EVENT, UNASSIGNED_EVENT]
                    first: 50
                ) {{
                    nodes {{
                        ... on ReviewRequestRemovedEvent {{
                            requestedReviewer {{
                                ... on User {{
                                    login
                                    id
                                }}
                            }}
                        }}
                        ... on UnassignedEvent {{
                            assignee {{
                                ... on User {{
                                    login
                                    id
                                }}
                            }}
                        }}
                    }}
                }}
                latestReviews (first: 25) {{
                    nodes {{
                        author {{
                            ... on User {{
                                login
                                id
                            }}
                        }}
                        viewerDidAuthor
                    }}
                }}
                assignees (first: 25) {{
                    nodes {{
                        ... on User {{
                            login
                            id
                        }}
                    }}
                }}
                labels (first: 25) {{
                    nodes {{
                        name
                        id
                    }}
                }}
                comments (first: {MAX_COMMENTS_TO_QUERY}) {{
                    nodes {{
                        body
                        id
                    }}
                }}
            }}
            totalCount
        }}"""

USER_FRAGMENT = """
        fragment UserResult on UserConnection {
            nodes {
                login
                id
            }
            totalCount
        }"""

LABEL_FRAGMENT = """
        fragment LabelResult on Label {
            id
            name
        }"""


def _make_pr_group(head_refs: List[str]) -> QueryGroup:
    group = QueryGroup(
        prefix="pr",
        scope="repo",
        field_template=(
            "{}: pullRequests (headRefName: {}, states: [OPEN, MERGED], first: 1, "
            "orderBy: {{direction: DESC, field:UPDATED_AT}}) {{...PrResult}},"
        ),
        var_types=["String!"],
        fragment=PR_FRAGMENT,
    )
    for ref in head_refs:
        group.add(ref)
    return group


def _make_user_group(user_ids: List[str]) -> QueryGroup:
    group = QueryGroup(
        prefix="user",
        scope="repo",
        field_template="{}: assignableUsers (query: {}, first: 25) {{...UserResult}},",
        var_types=["String!"],
        fragment=USER_FRAGMENT,
    )
    for uid in user_ids:
        group.add(uid)
    return group


def _make_label_group(labels: List[str]) -> QueryGroup:
    group = QueryGroup(
        prefix="label",
        scope="repo",
        field_template="{}: label (name: {}) {{...LabelResult}},",
        var_types=["String!"],
        fragment=LABEL_FRAGMENT,
    )
    for label in labels:
        group.add(label)
    return group


def _make_team_group(teams: List[Tuple[str, str]]) -> QueryGroup:
    group = QueryGroup(
        prefix="team",
        scope="top",
        field_template=(
            "{}: organization(login: {}) "
            "{{team(slug: {}) "
            "{{id, members(first: 100) {{nodes {{login}}, totalCount}}}}}},"
        ),
        var_types=["String!", "String!"],
        fragment="",
    )
    for org, slug in teams:
        group.add(org, slug)
    return group


def _parse_prs(group: QueryGroup, result: Any, head_refs: List[str]) -> List[Optional[PrInfo]]:
    raw = group.extract(result)
    prs: List[Optional[PrInfo]] = []
    for i, branch_name in enumerate(head_refs):
        this_node = raw[i]
        if len(this_node["nodes"]) == 1:
            this_node = this_node["nodes"][0]
            pr_labels: Set[str] = set()
            pr_label_ids: Set[str] = set()
            reviewers: Set[str] = set()
            reviewer_ids: Set[str] = set()
            reviewer_teams: Set[str] = set()
            reviewer_team_ids: Set[str] = set()
            assignees: Set[str] = set()
            assignee_ids: Set[str] = set()
            for label in this_node["labels"]["nodes"]:
                pr_labels.add(label["name"])
                pr_label_ids.add(label["id"])
            for revs in this_node["reviewRequests"]["nodes"]:
                requested = revs["requestedReviewer"]
                if not requested:
                    continue
                elif "slug" in requested:
                    reviewer_teams.add(f"{requested['organization']['login']}/{requested['slug']}")
                    reviewer_team_ids.add(requested["id"])
                elif "login" in requested:
                    reviewers.add(requested["login"])
                    reviewer_ids.add(requested["id"])
            for revs in this_node["latestReviews"]["nodes"]:
                if not revs["viewerDidAuthor"] and "login" in revs["author"]:
                    reviewers.add(revs["author"]["login"])
                    reviewer_ids.add(revs["author"]["id"])
            for user in this_node["assignees"]["nodes"]:
                assignees.add(user["login"])
                assignee_ids.add(user["id"])

            headRefOid = (
                this_node["headCommit"]["nodes"][0]["commit"]["oid"]
                if this_node["headCommit"]["nodes"]
                else None
            )
            baseRefOid = (
                this_node["baseCommit"]["nodes"][0]["commit"]["parents"]["nodes"][0]["oid"]
                if this_node["baseCommit"]["nodes"]
                else None
            )

            comments = []
            for c in this_node["comments"]["nodes"]:
                comments.append(PrComment(c["body"], c["id"]))

            removed_reviewers: Set[str] = set()
            removed_reviewer_ids: Set[str] = set()
            removed_assignees: Set[str] = set()
            removed_assignee_ids: Set[str] = set()
            for event in this_node["timelineItems"]["nodes"]:
                rr = event.get("requestedReviewer")
                if rr and "login" in rr and rr["login"] not in reviewers:
                    removed_reviewers.add(rr["login"])
                    removed_reviewer_ids.add(rr["id"])
                assignee = event.get("assignee")
                if assignee and "login" in assignee and assignee["login"] not in assignees:
                    removed_assignees.add(assignee["login"])
                    removed_assignee_ids.add(assignee["id"])

            prs.append(
                PrInfo(
                    id=this_node["id"],
                    url=this_node["url"],
                    baseRef=this_node["baseRefName"],
                    headRef=branch_name,
                    baseRefOid=baseRefOid,
                    headRefOid=headRefOid,
                    body=this_node["body"],
                    title=this_node["title"],
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
                    is_draft=this_node["isDraft"],
                    state=this_node["state"],
                    comments=comments,
                )
            )
        else:
            prs.append(None)
    return prs


def _parse_users(
    group: QueryGroup, result: Any, user_ids: List[str]
) -> Tuple[Dict[str, str], Dict[str, str]]:
    raw = group.extract(result)
    names_to_ids: Dict[str, str] = {}
    names_to_logins: Dict[str, str] = {}
    for i, user_id in enumerate(user_ids):
        this_node = raw[i]
        if len(this_node["nodes"]) == 0:
            logging.warning("No matching user found for {}".format(user_id))
        else:
            if this_node["totalCount"] > len(this_node["nodes"]):
                logging.warning(
                    "Too many matching users found for {}, try being more specific".format(user_id)
                )
            shortest_name = this_node["nodes"][0]["login"]
            names_to_ids[user_id] = this_node["nodes"][0]["id"]
            found_match = False
            for user in this_node["nodes"]:
                if len(user["login"]) <= len(shortest_name) and user["login"].startswith(user_id):
                    shortest_name = user["login"]
                    names_to_ids[user_id] = user["id"]
                    names_to_logins[user_id] = user["login"]
                    found_match = True
            if not found_match:
                logging.warning(
                    "Couldn't find a prefixed match for {}, going with {} instead".format(
                        user_id, shortest_name
                    )
                )
    return names_to_ids, names_to_logins


def _parse_labels(group: QueryGroup, result: Any, labels: List[str]) -> Dict[str, str]:
    raw = group.extract(result)
    labels_to_ids: Dict[str, str] = {}
    for i, label in enumerate(labels):
        this_node = raw[i]
        if this_node is not None:
            labels_to_ids[label] = this_node["id"]
        else:
            logging.warning("Couldn't find an existing label named {}".format(label))
    return labels_to_ids


def _parse_teams(
    group: QueryGroup, result: Any, teams: List[Tuple[str, str]]
) -> Tuple[Dict[str, str], Dict[str, Optional[Set[str]]]]:
    raw = group.extract(result)
    teams_to_ids: Dict[str, str] = {}
    teams_to_members: Dict[str, Optional[Set[str]]] = {}
    for i, (org, slug) in enumerate(teams):
        team_node = raw[i]
        if team_node is not None and team_node["team"] is not None:
            team_ref = f"{org}/{slug}"
            teams_to_ids[team_ref] = team_node["team"]["id"]
            members_node = team_node["team"]["members"]
            member_logins = {m["login"] for m in members_node["nodes"]}
            if members_node["totalCount"] > len(members_node["nodes"]):
                teams_to_members[team_ref] = None
            else:
                teams_to_members[team_ref] = member_logins
        else:
            logging.warning("Couldn't find a team matching {}/{}".format(org, slug))
    return teams_to_ids, teams_to_members


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

    def _make_query_everything(
        self,
        head_refs: List[str],
        user_ids: List[str],
        labels: List[str],
        teams: List[Tuple[str, str]],
    ) -> Tuple[GraphqlQuery, QueryGroup, QueryGroup, QueryGroup, QueryGroup]:
        q = GraphqlQuery(name="GetEverything")
        q.add_fixed_var("owner", "String!", self.repo_info.owner)
        q.add_fixed_var("name", "String!", self.repo_info.name)
        q.fixed_repo_fields = "id\n"

        pr_group = _make_pr_group(head_refs)
        user_group = _make_user_group(user_ids)
        label_group = _make_label_group(labels)
        team_group = _make_team_group(teams)

        q.add_group(pr_group)
        q.add_group(user_group)
        q.add_group(label_group)
        q.add_group(team_group)

        return q, pr_group, user_group, label_group, team_group

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
        q, pr_group, user_group, label_group, team_group = self._make_query_everything(
            head_refs, user_ids, labels, teams
        )

        query_str, variables = q.build()
        result = await self.endpoint.graphql(query_str, **variables)

        repo_id = result["data"]["repository"]["id"]
        prs = _parse_prs(pr_group, result, head_refs)
        names_to_ids, names_to_logins = _parse_users(user_group, result, user_ids)
        labels_to_ids = _parse_labels(label_group, result, labels)
        teams_to_ids, teams_to_members = _parse_teams(team_group, result, teams)

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
        inputs = []
        for pr in prs:
            headRef = (
                pr.headRef
                if self.fork_info.owner == self.repo_info.owner
                else f"{self.fork_info.owner}:{pr.headRef}"
            )
            inputs.append({
                "baseRefName": pr.baseRef,
                "body": pr.body,
                "clientMutationId": "revup",
                "headRefName": headRef,
                "repositoryId": repo_id,
                "title": pr.title,
                "draft": pr.is_draft,
            })

        group = QueryGroup(
            prefix="pr",
            scope="mutation",
            field_template="""
            {}: createPullRequest(input: {}) {{
                pullRequest {{
                    id
                    url
                }}
            }},""",
            var_types=["CreatePullRequestInput!"],
        )
        for inp in inputs:
            group.add(inp)

        q = GraphqlQuery(operation="mutation")
        q.add_group(group)
        query_str, variables = q.build()

        pr_results = await self.endpoint.graphql(query_str, **variables)
        raw = group.extract(pr_results)
        for i, pr in enumerate(prs):
            result_node = raw[i]["pullRequest"]
            if result_node is not None:
                pr.id = result_node["id"]
                pr.url = result_node["url"]

    async def update_pull_requests(self, prs: List[PrUpdate]) -> None:
        q = self._build_update_mutation(prs)
        query_str, variables = q.build()
        try:
            await self.endpoint.graphql(query_str, **variables)
        except RevupForgeException as e:
            if "timeout" in e.message:
                logging.warning(
                    "Github update request timed out! Most likely this is a false alarm and changes"
                    " actually succeeded. You may want to rerun this command to verify."
                )
            else:
                raise

    def _build_update_mutation(self, prs: List[PrUpdate]) -> GraphqlQuery:
        inputs = []
        labels = []
        reviewers = []
        assignees = []
        convert_to_draft = []
        convert_from_draft = []
        comments = []
        edit_comments = []
        for pr in prs:
            update_dict: Dict[str, Any] = {
                "clientMutationId": "revup",
                "pullRequestId": pr.id,
            }
            if pr.baseRef is not None:
                update_dict["baseRefName"] = pr.baseRef
            if pr.body is not None:
                update_dict["body"] = pr.body
            if pr.title is not None:
                update_dict["title"] = pr.title
            inputs.append(update_dict)

            if pr.label_ids:
                labels.append({
                    "labelIds": list(pr.label_ids),
                    "clientMutationId": "revup",
                    "labelableId": pr.id,
                })
            if pr.reviewer_ids or pr.reviewer_team_ids:
                reviewers.append({
                    "userIds": list(pr.reviewer_ids),
                    "teamIds": list(pr.reviewer_team_ids),
                    "clientMutationId": "revup",
                    "pullRequestId": pr.id,
                    "union": True,
                })
            if pr.assignee_ids:
                assignees.append({
                    "assigneeIds": list(pr.assignee_ids),
                    "clientMutationId": "revup",
                    "assignableId": pr.id,
                })
            if pr.is_draft is not None:
                if pr.is_draft:
                    convert_to_draft.append({
                        "clientMutationId": "revup",
                        "pullRequestId": pr.id,
                    })
                else:
                    convert_from_draft.append({
                        "clientMutationId": "revup",
                        "pullRequestId": pr.id,
                    })
            for c in pr.comments:
                if c.id:
                    edit_comments.append({
                        "body": c.text,
                        "clientMutationId": "revup",
                        "id": c.id,
                    })
                else:
                    comments.append({
                        "body": c.text,
                        "clientMutationId": "revup",
                        "subjectId": pr.id,
                    })

        update_group = QueryGroup(
            prefix="pr",
            scope="mutation",
            field_template="""
            {}: updatePullRequest(input: {}) {{
                clientMutationId
            }},""",
            var_types=["UpdatePullRequestInput!"],
        )
        for inp in inputs:
            update_group.add(inp)

        label_group = QueryGroup(
            prefix="label",
            scope="mutation",
            field_template="""
            {}: addLabelsToLabelable(input: {}) {{
                clientMutationId
            }},""",
            var_types=["AddLabelsToLabelableInput!"],
        )
        for inp in labels:
            label_group.add(inp)

        reviewer_group = QueryGroup(
            prefix="rev",
            scope="mutation",
            field_template="""
            {}: requestReviews(input: {}) {{
                clientMutationId
            }},""",
            var_types=["RequestReviewsInput!"],
        )
        for inp in reviewers:
            reviewer_group.add(inp)

        assignee_group = QueryGroup(
            prefix="asn",
            scope="mutation",
            field_template="""
            {}: addAssigneesToAssignable(input: {}) {{
                clientMutationId
            }},""",
            var_types=["AddAssigneesToAssignableInput!"],
        )
        for inp in assignees:
            assignee_group.add(inp)

        to_draft_group = QueryGroup(
            prefix="to_d",
            scope="mutation",
            field_template="""
            {}: convertPullRequestToDraft(input: {}) {{
                clientMutationId
            }},""",
            var_types=["ConvertPullRequestToDraftInput!"],
        )
        for inp in convert_to_draft:
            to_draft_group.add(inp)

        from_draft_group = QueryGroup(
            prefix="from_d",
            scope="mutation",
            field_template="""
            {}: markPullRequestReadyForReview(input: {}) {{
                clientMutationId
            }},""",
            var_types=["MarkPullRequestReadyForReviewInput!"],
        )
        for inp in convert_from_draft:
            from_draft_group.add(inp)

        comment_group = QueryGroup(
            prefix="com",
            scope="mutation",
            field_template="""
            {}: addComment(input: {}) {{
                clientMutationId
            }},""",
            var_types=["AddCommentInput!"],
        )
        for inp in comments:
            comment_group.add(inp)

        edit_comment_group = QueryGroup(
            prefix="edit_com",
            scope="mutation",
            field_template="""
            {}: updateIssueComment(input: {}) {{
                clientMutationId
            }},""",
            var_types=["UpdateIssueCommentInput!"],
        )
        for inp in edit_comments:
            edit_comment_group.add(inp)

        q = GraphqlQuery(operation="mutation")
        q.add_group(comment_group)
        q.add_group(update_group)
        q.add_group(reviewer_group)
        q.add_group(assignee_group)
        q.add_group(label_group)
        q.add_group(to_draft_group)
        q.add_group(from_draft_group)
        q.add_group(edit_comment_group)
        return q

    async def query_pr_by_number(self, owner: str, name: str, number: int) -> Tuple[str, str]:
        result = await self.endpoint.graphql(
            query="""\
query ($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      headRefName
      baseRefName
    }
  }
}""",
            owner=owner,
            name=name,
            number=number,
        )
        pr = result["data"]["repository"]["pullRequest"]
        return pr["headRefName"], pr["baseRefName"]
