import logging
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from revup.forge import (
    MAX_COMMENTS_TO_QUERY,
    Forge,
    ForgeRepoInfo,
    PrComment,
    PrInfo,
    PrUpdate,
)
from revup.github.endpoint import GitHubEndpoint
from revup.types import RevupForgeException


def _get_args_dict(args: List[Any], prefix: str) -> Dict[str, Any]:
    return {f"{prefix}{n}": arg for n, arg in enumerate(args)}


def _get_args_declaration(args: Dict[str, Any], typ: str) -> List[str]:
    return [f"${var}: {typ}" for var in args]


def _get_result_args(num: int, prefix: str) -> List[str]:
    return [f"{prefix}{n}" for n in range(num)]


def _zip_and_flatten(l1: Iterable[str], l2: Iterable[str]) -> List[str]:
    ret: List[str] = []
    iter1 = iter(l1)
    iter2 = iter(l2)
    while True:
        try:
            ret.append(next(iter1))
            ret.append(next(iter2))
        except StopIteration:
            break
    return ret


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
        head_refs_args = _get_args_dict(head_refs, "pr")
        user_id_args = _get_args_dict(user_ids, "user")
        label_args = _get_args_dict(labels, "label")
        team_org_args = _get_args_dict([t[0] for t in teams], "team_org")
        team_slug_args = _get_args_dict([t[1] for t in teams], "team_slug")

        prs_out = _get_result_args(len(head_refs), "pr_out")
        user_id_out = _get_result_args(len(user_ids), "user_out")
        label_out = _get_result_args(len(labels), "label_out")
        team_out = _get_result_args(len(teams), "team_out")

        arg_str = ", ".join(
            _get_args_declaration(head_refs_args, "String!")
            + _get_args_declaration(user_id_args, "String!")
            + _get_args_declaration(label_args, "String!")
            + _get_args_declaration(team_org_args, "String!")
            + _get_args_declaration(team_slug_args, "String!")
        )

        # NOTE: There are possible limitations here because we depend on PRs being
        # returned in order of OPEN prs, followed by MERGED prs in the order that
        # they merged. github doesn't offer these options and it is excessively
        # expensive to always fetch multiple prs and order them on this side. For now
        # we hope that the most relevant PR will have the most recent update time.
        request_str = "".join(
            len(head_refs)
            * [
                "{}: pullRequests (headRefName: ${}, states: [OPEN, MERGED], first: 1, "
                "orderBy: {{direction: DESC, field:UPDATED_AT}}) {{"
                "...PrResult"
                "}},"
            ]
        )
        request_str = request_str.format(*_zip_and_flatten(prs_out, head_refs_args.keys()))

        user_str = "".join(
            len(user_ids) * ["{}: assignableUsers (query: ${}, first: 25) {{...UserResult}},"]
        )
        user_str = user_str.format(*_zip_and_flatten(user_id_out, user_id_args.keys()))

        label_str = "".join(len(labels) * ["{}: label (name: ${}) {{...LabelResult}},"])
        label_str = label_str.format(*_zip_and_flatten(label_out, label_args.keys()))

        team_str = ""
        for i in range(len(teams)):
            team_str += (
                f"{team_out[i]}: organization(login: ${list(team_org_args.keys())[i]}) "
                f"{{team(slug: ${list(team_slug_args.keys())[i]}) "
                f"{{id, members(first: 100) {{nodes {{login}}, totalCount}}}}}},"
            )

        multi_query_str = f"""
        query GetPrResults($owner: String!, $name: String!, {arg_str}) {{
            repository(name: $name, owner: $owner) {{
                id
                {request_str}{user_str}{label_str}
            }}
            {team_str}
        }}"""
        if user_str:
            multi_query_str += """
        fragment UserResult on UserConnection {
            nodes {
                login
                id
            }
            totalCount
        }"""
        if label_str:
            multi_query_str += """
        fragment LabelResult on Label {
            id
            name
        }"""
        if request_str:
            multi_query_str += f"""
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

        pr_result = await self.endpoint.graphql(
            multi_query_str,
            owner=self.repo_info.owner,
            name=self.repo_info.name,
            **head_refs_args,
            **user_id_args,
            **label_args,
            **team_org_args,
            **team_slug_args,
        )

        prs: List[Optional[PrInfo]] = []
        for i, branch_name in enumerate(head_refs):
            this_node = pr_result["data"]["repository"][prs_out[i]]
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
                        reviewer_teams.add(
                            f"{requested['organization']['login']}/{requested['slug']}"
                        )
                        reviewer_team_ids.add(requested["id"])
                    elif "login" in requested:
                        reviewers.add(requested["login"])
                        reviewer_ids.add(requested["id"])
                for revs in this_node["latestReviews"]["nodes"]:
                    # Ignore self reviews and bot reviews (without a login)
                    if not revs["viewerDidAuthor"] and "login" in revs["author"]:
                        reviewers.add(revs["author"]["login"])
                        reviewer_ids.add(revs["author"]["id"])
                for user in this_node["assignees"]["nodes"]:
                    assignees.add(user["login"])
                    assignee_ids.add(user["id"])

                # The plain headRef and baseRef fields return the latest commit id associated with
                # that branch name which may be newer than the PR itself if it was merged. We want
                # the ids of the commits actually last associated with the PR, which we query from
                # the commit list. This can also mean they are None if the PR has 0 commits.
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

        names_to_ids: Dict[str, str] = {}
        names_to_logins: Dict[str, str] = {}
        for i, user_id in enumerate(user_ids):
            this_node = pr_result["data"]["repository"][user_id_out[i]]
            if len(this_node["nodes"]) == 0:
                logging.warning("No matching user found for {}".format(user_id))
            else:
                if this_node["totalCount"] > len(this_node["nodes"]):
                    logging.warning(
                        "Too many matching users found for {}, try being more specific".format(
                            user_id
                        )
                    )
                shortest_name = this_node["nodes"][0]["login"]
                names_to_ids[user_id] = this_node["nodes"][0]["id"]
                found_match = False
                for user in this_node["nodes"]:
                    if len(user["login"]) <= len(shortest_name) and user["login"].startswith(
                        user_id
                    ):
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

        labels_to_ids: Dict[str, str] = {}
        for i, label in enumerate(labels):
            this_node = pr_result["data"]["repository"][label_out[i]]
            if this_node is not None:
                labels_to_ids[label] = this_node["id"]
            else:
                logging.warning("Couldn't find an existing label named {}".format(label))

        teams_to_ids: Dict[str, str] = {}
        teams_to_members: Dict[str, Optional[Set[str]]] = {}
        for i, (org, slug) in enumerate(teams):
            team_node = pr_result["data"][team_out[i]]
            if team_node is not None and team_node["team"] is not None:
                team_ref = f"{org}/{slug}"
                teams_to_ids[team_ref] = team_node["team"]["id"]
                members_node = team_node["team"]["members"]
                member_logins = {m["login"] for m in members_node["nodes"]}
                if members_node["totalCount"] > len(members_node["nodes"]):
                    # Team has more members than we fetched; we can't check membership reliably.
                    teams_to_members[team_ref] = None
                else:
                    teams_to_members[team_ref] = member_logins
            else:
                logging.warning("Couldn't find a team matching {}/{}".format(org, slug))

        return (
            pr_result["data"]["repository"]["id"],
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
        inputs_args = _get_args_dict(inputs, "pr")
        prs_out = _get_result_args(len(inputs), "pr_out")

        arg_str = ", ".join(_get_args_declaration(inputs_args, "CreatePullRequestInput!"))

        request_str = "".join(
            len(inputs)
            * [
                """
            {}: createPullRequest(input: ${}) {{
                pullRequest {{
                    id
                    url
                }}
            }},"""
            ]
        )
        request_str = request_str.format(*_zip_and_flatten(prs_out, inputs_args.keys()))

        mutation_str = f"""
        mutation ({arg_str}) {{
            {request_str}
        }}"""

        # Creating a pull request can fail if the branch is already merged.
        pr_results = await self.endpoint.graphql(mutation_str, require_success=False, **inputs_args)
        for i, pr in enumerate(prs):
            result = pr_results["data"][prs_out[i]]["pullRequest"]
            if result is not None:
                pr.id = result["id"]
                pr.url = result["url"]

    async def update_pull_requests(self, prs: List[PrUpdate]) -> None:
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

        inputs_args = _get_args_dict(inputs, "pr")
        prs_out = _get_result_args(len(inputs), "pr_out")

        labels_args = _get_args_dict(labels, "label")
        labels_out = _get_result_args(len(labels), "label_out")

        reviewers_args = _get_args_dict(reviewers, "rev")
        reviewers_out = _get_result_args(len(reviewers), "rev_out")

        assignees_args = _get_args_dict(assignees, "asn")
        assignees_out = _get_result_args(len(assignees), "asn_out")

        to_draft_args = _get_args_dict(convert_to_draft, "to_d")
        to_draft_out = _get_result_args(len(convert_to_draft), "to_d_out")

        from_draft_args = _get_args_dict(convert_from_draft, "from_d")
        from_draft_out = _get_result_args(len(convert_from_draft), "from_d_out")

        comments_args = _get_args_dict(comments, "com")
        comments_out = _get_result_args(len(comments), "com_out")

        edit_comments_args = _get_args_dict(edit_comments, "edit_com")
        edit_comments_out = _get_result_args(len(edit_comments), "edit_com_out")

        arg_str = ", ".join(
            _get_args_declaration(inputs_args, "UpdatePullRequestInput!")
            + _get_args_declaration(labels_args, "AddLabelsToLabelableInput!")
            + _get_args_declaration(reviewers_args, "RequestReviewsInput!")
            + _get_args_declaration(assignees_args, "AddAssigneesToAssignableInput!")
            + _get_args_declaration(to_draft_args, "ConvertPullRequestToDraftInput!")
            + _get_args_declaration(from_draft_args, "MarkPullRequestReadyForReviewInput!")
            + _get_args_declaration(comments_args, "AddCommentInput!")
            + _get_args_declaration(edit_comments_args, "UpdateIssueCommentInput!")
        )

        update_str = "".join(
            len(inputs)
            * [
                """
            {}: updatePullRequest(input: ${}) {{
                clientMutationId
            }},"""
            ]
        )
        update_str = update_str.format(*_zip_and_flatten(prs_out, inputs_args.keys()))

        request_reviewers_str = "".join(
            len(reviewers_args)
            * [
                """
            {}: requestReviews(input: ${}) {{
                clientMutationId
            }},"""
            ]
        )
        request_reviewers_str = request_reviewers_str.format(
            *_zip_and_flatten(reviewers_out, reviewers_args.keys())
        )
        assignees_str = "".join(
            len(assignees_args)
            * [
                """
            {}: addAssigneesToAssignable(input: ${}) {{
                clientMutationId
            }},"""
            ]
        )
        assignees_str = assignees_str.format(
            *_zip_and_flatten(assignees_out, assignees_args.keys())
        )

        add_labels_str = "".join(
            len(labels_args)
            * [
                """
            {}: addLabelsToLabelable(input: ${}) {{
                clientMutationId
            }},"""
            ]
        )
        add_labels_str = add_labels_str.format(*_zip_and_flatten(labels_out, labels_args.keys()))

        to_draft_str = "".join(
            len(convert_to_draft)
            * [
                """
            {}: convertPullRequestToDraft(input: ${}) {{
                clientMutationId
            }},"""
            ]
        )
        to_draft_str = to_draft_str.format(*_zip_and_flatten(to_draft_out, to_draft_args.keys()))

        from_draft_str = "".join(
            len(convert_from_draft)
            * [
                """
            {}: markPullRequestReadyForReview(input: ${}) {{
                clientMutationId
            }},"""
            ]
        )
        from_draft_str = from_draft_str.format(
            *_zip_and_flatten(from_draft_out, from_draft_args.keys())
        )

        add_comments_str = "".join(
            len(comments_args)
            * [
                """
            {}: addComment(input: ${}) {{
                clientMutationId
            }},"""
            ]
        )
        add_comments_str = add_comments_str.format(
            *_zip_and_flatten(comments_out, comments_args.keys())
        )

        edit_comments_str = "".join(
            len(edit_comments_args)
            * [
                """
            {}: updateIssueComment(input: ${}) {{
                clientMutationId
            }},"""
            ]
        )
        edit_comments_str = edit_comments_str.format(
            *_zip_and_flatten(edit_comments_out, edit_comments_args.keys())
        )

        # Add comment mutations first to ensure comments are at the top of the PR
        mutation_str = f"""
        mutation ({arg_str}) {{
            {add_comments_str}{update_str}{request_reviewers_str}{assignees_str}{add_labels_str}\
{to_draft_str}{from_draft_str}{edit_comments_str}
        }}"""

        try:
            await self.endpoint.graphql(
                mutation_str,
                **comments_args,
                **inputs_args,
                **reviewers_args,
                **assignees_args,
                **labels_args,
                **to_draft_args,
                **from_draft_args,
                **edit_comments_args,
            )
        except RevupForgeException as e:
            if "timeout" in e.message:
                logging.warning(
                    "Github update request timed out! Most likely this is a false alarm and changes"
                    " actually succeeded. You may want to rerun this command to verify."
                )
            else:
                raise e

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
