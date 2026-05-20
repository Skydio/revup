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
from revup.github.graphql import GraphqlQuery, SingleQuery
from revup.types import RevupForgeException, RevupRequestException

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


def _is_resource_limit_error(e: Exception) -> bool:
    if isinstance(e, RevupForgeException):
        return "RESOURCE_LIMITS_EXCEEDED" in e.types
    if isinstance(e, RevupRequestException):
        return e.status in {502, 503}
    return False


def _add_pr_queries(q: GraphqlQuery, head_refs: List[str]) -> List[SingleQuery]:
    return [
        q.add(
            prefix="pr",
            scope="repo",
            field_template=(
                "{}: pullRequests (headRefName: {}, states: [OPEN, MERGED], first: 1, "
                "orderBy: {{direction: DESC, field:UPDATED_AT}}) {{...PrResult}},"
            ),
            var_types=["String!"],
            values=[ref],
            fragment=PR_FRAGMENT,
        )
        for ref in head_refs
    ]


def _add_user_queries(q: GraphqlQuery, user_ids: List[str]) -> List[SingleQuery]:
    return [
        q.add(
            prefix="user",
            scope="repo",
            field_template="{}: assignableUsers (query: {}, first: 25) {{...UserResult}},",
            var_types=["String!"],
            values=[uid],
            fragment=USER_FRAGMENT,
        )
        for uid in user_ids
    ]


def _add_label_queries(q: GraphqlQuery, labels: List[str]) -> List[SingleQuery]:
    return [
        q.add(
            prefix="label",
            scope="repo",
            field_template="{}: label (name: {}) {{...LabelResult}},",
            var_types=["String!"],
            values=[label],
            fragment=LABEL_FRAGMENT,
        )
        for label in labels
    ]


def _add_team_queries(q: GraphqlQuery, teams: List[Tuple[str, str]]) -> List[SingleQuery]:
    return [
        q.add(
            prefix="team",
            scope="top",
            field_template=(
                "{}: organization(login: {}) "
                "{{team(slug: {}) "
                "{{id, members(first: 100) {{nodes {{login}}, totalCount}}}}}},"
            ),
            var_types=["String!", "String!"],
            values=[org, slug],
        )
        for org, slug in teams
    ]


def _parse_prs(
    queries: List[SingleQuery], result: Any, head_refs: List[str]
) -> List[Optional[PrInfo]]:
    raw = [sq.extract(result) for sq in queries]
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
    queries: List[SingleQuery], result: Any, user_ids: List[str]
) -> Tuple[Dict[str, str], Dict[str, str]]:
    raw = [sq.extract(result) for sq in queries]
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


def _parse_labels(queries: List[SingleQuery], result: Any, labels: List[str]) -> Dict[str, str]:
    raw = [sq.extract(result) for sq in queries]
    labels_to_ids: Dict[str, str] = {}
    for i, label in enumerate(labels):
        this_node = raw[i]
        if this_node is not None:
            labels_to_ids[label] = this_node["id"]
        else:
            logging.warning("Couldn't find an existing label named {}".format(label))
    return labels_to_ids


def _parse_teams(
    queries: List[SingleQuery], result: Any, teams: List[Tuple[str, str]]
) -> Tuple[Dict[str, str], Dict[str, Optional[Set[str]]]]:
    raw = [sq.extract(result) for sq in queries]
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


async def _refresh_new_comment_ids(endpoint: GitHubEndpoint, prs: List[PrUpdate]) -> None:
    """Re-query comments for PRs with new (id=None) comments to avoid duplicates on retry."""
    prs_with_new = [pr for pr in prs if any(c.id is None for c in pr.comments)]
    if not prs_with_new:
        return

    query = GraphqlQuery()
    node_queries = [
        query.add(
            prefix="node",
            scope="top",
            field_template=(
                "{}: node(id: {}) {{ ... on PullRequest {{ comments(first: "
                + str(MAX_COMMENTS_TO_QUERY)
                + ") {{ nodes {{ body id }} }} }} }},"
            ),
            var_types=["ID!"],
            values=[pr.id],
        )
        for pr in prs_with_new
    ]
    query_str, variables = query.build()

    result = await endpoint.graphql(query_str, max_retries=1, **variables)

    raw = [sq.extract(result) for sq in node_queries]
    for pr, pr_data in zip(prs_with_new, raw):
        existing = pr_data.get("comments", {}).get("nodes", []) if pr_data else []
        existing_by_body = {c["body"]: c["id"] for c in existing}
        for comment in pr.comments:
            if comment.id is None and comment.text in existing_by_body:
                comment.id = existing_by_body[comment.text]
                logging.info("Comment already posted on PR, converting to edit")


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
    ) -> Tuple[
        GraphqlQuery,
        List[SingleQuery],
        List[SingleQuery],
        List[SingleQuery],
        List[SingleQuery],
    ]:
        q = GraphqlQuery(name="GetEverything")
        q.add_fixed_var("owner", "String!", self.repo_info.owner)
        q.add_fixed_var("name", "String!", self.repo_info.name)
        q.fixed_repo_fields = "id\n"

        pr_queries = _add_pr_queries(q, head_refs)
        user_queries = _add_user_queries(q, user_ids)
        label_queries = _add_label_queries(q, labels)
        team_queries = _add_team_queries(q, teams)

        return q, pr_queries, user_queries, label_queries, team_queries

    async def _execute_with_backoff(self, q: GraphqlQuery, max_retries: int = 3) -> Any:
        query_str, variables = q.build()
        try:
            return await self.endpoint.graphql(query_str, max_retries=max_retries, **variables)
        except (RevupForgeException, RevupRequestException) as e:
            if not _is_resource_limit_error(e) or q.total_items() <= 1:
                raise
        left, right = q.split()
        logging.warning(
            "Request too complex ({} items), splitting into {} + {}".format(
                q.total_items(), left.total_items(), right.total_items()
            )
        )
        left_result = await self._execute_with_backoff(left, max_retries=max_retries)
        right_result = await self._execute_with_backoff(right, max_retries=max_retries)
        return _merge_results(left_result, right_result)

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
        q, pr_queries, user_queries, label_queries, team_queries = self._make_query_everything(
            head_refs, user_ids, labels, teams
        )

        result = await self._execute_with_backoff(q)

        repo_id = result["data"]["repository"]["id"]
        prs = _parse_prs(pr_queries, result, head_refs)
        names_to_ids, names_to_logins = _parse_users(user_queries, result, user_ids)
        labels_to_ids = _parse_labels(label_queries, result, labels)
        teams_to_ids, teams_to_members = _parse_teams(team_queries, result, teams)

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
            inputs.append(
                {
                    "baseRefName": pr.baseRef,
                    "body": pr.body,
                    "clientMutationId": "revup",
                    "headRefName": headRef,
                    "repositoryId": repo_id,
                    "title": pr.title,
                    "draft": pr.is_draft,
                }
            )

        q = GraphqlQuery(operation="mutation")
        pr_queries = [
            q.add(
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
                values=[inp],
            )
            for inp in inputs
        ]

        pr_results = await self._execute_with_backoff(q)
        raw = [sq.extract(pr_results) for sq in pr_queries]
        for i, pr in enumerate(prs):
            result_node = raw[i]["pullRequest"]
            if result_node is not None:
                pr.id = result_node["id"]
                pr.url = result_node["url"]

    async def update_pull_requests(self, prs: List[PrUpdate]) -> None:
        q = self._build_update_mutation(prs)
        query_str, variables = q.build()
        try:
            await self.endpoint.graphql(query_str, max_retries=1, **variables)
        except (RevupForgeException, RevupRequestException) as e:
            if isinstance(e, RevupForgeException) and "timeout" in e.message:
                logging.warning(
                    "Github update request timed out! Most likely this is a false alarm and changes"
                    " actually succeeded. You may want to rerun this command to verify."
                )
                return
            if not _is_resource_limit_error(e) or len(prs) <= 1:
                raise
            mid = len(prs) // 2
            logging.warning(
                "Update request too complex ({} PRs), splitting into {} + {}".format(
                    len(prs), mid, len(prs) - mid
                )
            )
            await _refresh_new_comment_ids(self.endpoint, prs)
            await self.update_pull_requests(prs[:mid])
            await self.update_pull_requests(prs[mid:])

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
                labels.append(
                    {
                        "labelIds": list(pr.label_ids),
                        "clientMutationId": "revup",
                        "labelableId": pr.id,
                    }
                )
            if pr.reviewer_ids or pr.reviewer_team_ids:
                reviewers.append(
                    {
                        "userIds": list(pr.reviewer_ids),
                        "teamIds": list(pr.reviewer_team_ids),
                        "clientMutationId": "revup",
                        "pullRequestId": pr.id,
                        "union": True,
                    }
                )
            if pr.assignee_ids:
                assignees.append(
                    {
                        "assigneeIds": list(pr.assignee_ids),
                        "clientMutationId": "revup",
                        "assignableId": pr.id,
                    }
                )
            if pr.is_draft is not None:
                if pr.is_draft:
                    convert_to_draft.append(
                        {
                            "clientMutationId": "revup",
                            "pullRequestId": pr.id,
                        }
                    )
                else:
                    convert_from_draft.append(
                        {
                            "clientMutationId": "revup",
                            "pullRequestId": pr.id,
                        }
                    )
            for c in pr.comments:
                if c.id:
                    edit_comments.append(
                        {
                            "body": c.text,
                            "clientMutationId": "revup",
                            "id": c.id,
                        }
                    )
                else:
                    comments.append(
                        {
                            "body": c.text,
                            "clientMutationId": "revup",
                            "subjectId": pr.id,
                        }
                    )

        q = GraphqlQuery(operation="mutation")

        def add_all(prefix: str, mutation: str, var_type: str, items: List[Any]) -> None:
            for inp in items:
                q.add(
                    prefix=prefix,
                    scope="mutation",
                    field_template="""
            {}: """
                    + mutation
                    + """(input: {}) {{
                clientMutationId
            }},""",
                    var_types=[var_type],
                    values=[inp],
                )

        add_all("com", "addComment", "AddCommentInput!", comments)
        add_all("pr", "updatePullRequest", "UpdatePullRequestInput!", inputs)
        add_all("rev", "requestReviews", "RequestReviewsInput!", reviewers)
        add_all("asn", "addAssigneesToAssignable", "AddAssigneesToAssignableInput!", assignees)
        add_all("label", "addLabelsToLabelable", "AddLabelsToLabelableInput!", labels)
        add_all(
            "to_d", "convertPullRequestToDraft", "ConvertPullRequestToDraftInput!", convert_to_draft
        )
        add_all(
            "from_d",
            "markPullRequestReadyForReview",
            "MarkPullRequestReadyForReviewInput!",
            convert_from_draft,
        )
        add_all("edit_com", "updateIssueComment", "UpdateIssueCommentInput!", edit_comments)
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


def _merge_results(left: Any, right: Any) -> Any:
    """Merge two GraphQL result dicts by combining their data keys."""
    merged: Dict[str, Any] = {"data": {}}
    if "data" in left:
        merged["data"].update(left["data"])
    if "data" in right:
        for key, val in right["data"].items():
            if key == "repository" and "repository" in merged["data"]:
                merged["data"]["repository"].update(val)
            else:
                merged["data"][key] = val
    return merged
