import argparse
import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import (
    Any,
    AsyncGenerator,
    Dict,
    Iterable,
    List,
    NamedTuple,
    Optional,
    Set,
    Tuple,
)

from revup import config, git, github, logs
from revup.git import GitHubRepoInfo
from revup.types import (
    GitCommitHash,
    RevupGithubException,
    RevupRequestException,
    RevupUsageException,
)

MAX_COMMENTS_TO_QUERY = 3

# GitHub returns this GraphQL error type when a single request consumes too many
# server-side resources. Unlike the documented 500k node limit (a static up-front
# rejection), this is a runtime budget hit partway through execution: the response
# contains partial data for the sub-operations that did run plus this error for the
# ones that didn't. There is no published formula or threshold, so we can't predict
# it; instead we send a batch, and if we get this error we split the batch in half
# and retry until it fits.
RESOURCE_LIMITS_EXCEEDED = "RESOURCE_LIMITS_EXCEEDED"


@dataclass
class PrComment:
    text: str = ""
    id: Optional[str] = None


@dataclass
class PrInfo:
    """
    Represents a Github pull request.
    """

    baseRef: str
    headRef: str
    baseRefOid: Optional[GitCommitHash]
    headRefOid: Optional[GitCommitHash]
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
    """
    Represents a Github pull request update with the same fields as
    a pull request, except some are optional.
    """

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


def get_args_dict(args: List[Any], prefix: str) -> Dict[str, Any]:
    """
    Return a dictionary of argument names to argument values, for use in graphql.
    """
    return {f"{prefix}{n}": arg for n, arg in enumerate(args)}


def get_args_declaration(args: Dict[str, Any], typ: str) -> List[str]:
    """
    Return a list of args with their type declaration.
    """
    return [f"${var}: {typ}" for var in args]


def get_result_args(num: int, prefix: str) -> List[str]:
    """
    Return a list of result variable names.
    """
    return [f"{prefix}{n}" for n in range(num)]


def zip_and_flatten(l1: Iterable[str], l2: Iterable[str]) -> List[str]:
    """
    Return a list of l1 and l2 interleaved.
    """
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


async def _query_repo_users_labels(
    github_ep: github.GitHubEndpoint,
    repo_info: GitHubRepoInfo,
    user_ids: List[str],
    labels: List[str],
    teams: List[Tuple[str, str]],
) -> Tuple[
    str,
    Dict[str, str],
    Dict[str, str],
    Dict[str, str],
    Dict[str, str],
    Dict[str, Optional[Set[str]]],
]:
    """
    Query the repository node id along with user, label, and team lookups in a single request.

    None of these scale with the number of PRs, so they're fetched once rather than per PR batch.

    Returns a tuple of:
    - Repository node id
    - Dict of user_ids as given to graphql node ids
    - Dict of user_ids as given to their full login name
    - Dict of labels to their graphql node ids
    - Dict of "org/slug" team refs to graphql node ids
    - Dict of "org/slug" team refs to their member logins. None if the team has more members
      than we fetched (meaning membership is unknown / incomplete).
    """
    user_id_args = get_args_dict(user_ids, "user")
    label_args = get_args_dict(labels, "label")
    team_org_args = get_args_dict([t[0] for t in teams], "team_org")
    team_slug_args = get_args_dict([t[1] for t in teams], "team_slug")

    user_id_out = get_result_args(len(user_ids), "user_out")
    label_out = get_result_args(len(labels), "label_out")
    team_out = get_result_args(len(teams), "team_out")

    arg_str = ", ".join(
        get_args_declaration(user_id_args, "String!")
        + get_args_declaration(label_args, "String!")
        + get_args_declaration(team_org_args, "String!")
        + get_args_declaration(team_slug_args, "String!")
    )
    if arg_str:
        arg_str = ", " + arg_str

    user_str = "".join(
        len(user_ids) * ["{}: assignableUsers (query: ${}, first: 25) {{...UserResult}},"]
    )
    user_str = user_str.format(*zip_and_flatten(user_id_out, user_id_args.keys()))

    label_str = "".join(len(labels) * ["{}: label (name: ${}) {{...LabelResult}},"])
    label_str = label_str.format(*zip_and_flatten(label_out, label_args.keys()))

    team_str = ""
    for i in range(len(teams)):
        team_str += (
            f"{team_out[i]}: organization(login: ${list(team_org_args.keys())[i]}) "
            f"{{team(slug: ${list(team_slug_args.keys())[i]}) "
            f"{{id, members(first: 100) {{nodes {{login}}, totalCount}}}}}},"
        )

    query_str = f"""
        query ($owner: String!, $name: String!{arg_str}) {{
            repository(name: $name, owner: $owner) {{
                id
                {user_str}{label_str}
            }}
            {team_str}
        }}"""
    if user_str:
        query_str += """
        fragment UserResult on UserConnection {
            nodes {
                login
                id
            }
            totalCount
        }"""
    if label_str:
        query_str += """
        fragment LabelResult on Label {
            id
            name
        }"""

    result = await github_ep.graphql(
        query_str,
        owner=repo_info.owner,
        name=repo_info.name,
        **user_id_args,
        **label_args,
        **team_org_args,
        **team_slug_args,
    )

    repo_id = result["data"]["repository"]["id"]

    names_to_ids: Dict[str, str] = {}
    names_to_logins: Dict[str, str] = {}
    for i, user_id in enumerate(user_ids):
        this_node = result["data"]["repository"][user_id_out[i]]
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

    labels_to_ids: Dict[str, str] = {}
    for i, label in enumerate(labels):
        this_node = result["data"]["repository"][label_out[i]]
        if this_node is not None:
            labels_to_ids[label] = this_node["id"]
        else:
            logging.warning("Couldn't find an existing label named {}".format(label))

    teams_to_ids: Dict[str, str] = {}
    teams_to_members: Dict[str, Optional[Set[str]]] = {}
    for i, (org, slug) in enumerate(teams):
        team_node = result["data"][team_out[i]]
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

    return repo_id, names_to_ids, names_to_logins, labels_to_ids, teams_to_ids, teams_to_members


async def _query_prs_batch(
    github_ep: github.GitHubEndpoint,
    repo_info: GitHubRepoInfo,
    head_refs: List[str],
) -> List[Optional[PrInfo]]:
    head_refs_args = get_args_dict(head_refs, "pr")
    prs_out = get_result_args(len(head_refs), "pr_out")

    arg_str = ", ".join(get_args_declaration(head_refs_args, "String!"))

    # NOTE: There are possible limitations here because we depend on PRs being returned in order of
    # OPEN prs, followed by MERGED prs in the order that they merged. github doesn't offer these
    # options and it is excessively expensive to always fetch multiple prs and order them on this
    # side. For now we hope that the most relevant PR will have the most recent update time.
    request_str = "".join(
        len(head_refs)
        * [
            "{}: pullRequests (headRefName: ${}, states: [OPEN, MERGED], first: 1, "
            "orderBy: {{direction: DESC, field:UPDATED_AT}}) {{"
            "...PrResult"
            "}},"
        ]
    )
    request_str = request_str.format(*zip_and_flatten(prs_out, head_refs_args.keys()))

    query_str = f"""
        query ($owner: String!, $name: String!, {arg_str}) {{
            repository(name: $name, owner: $owner) {{
                {request_str}
            }}
        }}
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

    pr_result = await github_ep.graphql(
        query_str,
        owner=repo_info.owner,
        name=repo_info.name,
        **head_refs_args,
    )

    prs: List[Optional[PrInfo]] = []
    for i, branch_name in enumerate(head_refs):
        this_node = pr_result["data"]["repository"][prs_out[i]]
        if len(this_node["nodes"]) == 1:
            this_node = this_node["nodes"][0]
            pr_labels = set()
            pr_label_ids = set()
            reviewers = set()
            reviewer_ids = set()
            reviewer_teams = set()
            reviewer_team_ids = set()
            assignees = set()
            assignee_ids = set()
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

    return prs


async def _query_prs_with_splitting(
    github_ep: github.GitHubEndpoint,
    repo_info: GitHubRepoInfo,
    head_refs: List[str],
) -> List[Optional[PrInfo]]:
    """
    Query PRs for head_refs, halving the batch and retrying on RESOURCE_LIMITS_EXCEEDED.

    The query path is read-only, so splitting and concatenating results is always safe.
    Raises if a single ref still exceeds the limit (can't split further).
    """
    try:
        return await _query_prs_batch(github_ep, repo_info, head_refs)
    except RevupGithubException as e:
        if RESOURCE_LIMITS_EXCEEDED not in e.types or len(head_refs) <= 1:
            raise
        logging.warning(
            "GitHub GraphQL RESOURCE_LIMITS_EXCEEDED querying %d PRs, splitting batch in half",
            len(head_refs),
        )
        mid = len(head_refs) // 2
        prs = await _query_prs_with_splitting(github_ep, repo_info, head_refs[:mid])
        prs.extend(await _query_prs_with_splitting(github_ep, repo_info, head_refs[mid:]))
        return prs


async def query_everything(
    github_ep: github.GitHubEndpoint,
    repo_info: GitHubRepoInfo,
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
    Query all necessary data from GitHub, batching PR lookups to stay within
    GitHub's GraphQL resource limits.

    Returns a tuple of:
    - Repository node id
    - List of pull requests, one for each ref in head_refs. None if a pr wasn't found for that ref
    - Dict of user_ids as given to graphql node ids
    - Dict of user_ids as given to their full login name
    - Dict of labels to their graphql node ids
    - Dict of "org/slug" team refs to graphql node ids
    - Dict of "org/slug" team refs to their member logins. None if the team has more members
      than we fetched (meaning membership is unknown / incomplete).
    """
    (
        repo_id,
        names_to_ids,
        names_to_logins,
        labels_to_ids,
        teams_to_ids,
        teams_to_members,
    ) = await _query_repo_users_labels(github_ep, repo_info, user_ids, labels, teams)

    batch_size = github_ep.batch_size
    prs: List[Optional[PrInfo]] = []
    for i in range(0, len(head_refs), batch_size):
        prs.extend(
            await _query_prs_with_splitting(github_ep, repo_info, head_refs[i : i + batch_size])
        )

    return (
        repo_id,
        prs,
        names_to_ids,
        names_to_logins,
        labels_to_ids,
        teams_to_ids,
        teams_to_members,
    )


async def _create_pull_requests_batch(
    github_ep: github.GitHubEndpoint,
    repo_id: str,
    repo_info: GitHubRepoInfo,
    fork_info: GitHubRepoInfo,
    prs: List[PrInfo],
) -> None:
    inputs = []
    for pr in prs:
        headRef = (
            pr.headRef if fork_info.owner == repo_info.owner else f"{fork_info.owner}:{pr.headRef}"
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
    inputs_args = get_args_dict(inputs, "pr")
    prs_out = get_result_args(len(inputs), "pr_out")

    arg_str = ", ".join(get_args_declaration(inputs_args, "CreatePullRequestInput!"))

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
    request_str = request_str.format(*zip_and_flatten(prs_out, inputs_args.keys()))

    mutation_str = f"""
        mutation ({arg_str}) {{
            {request_str}
        }}"""

    # Creating a pull request can fail if the branch is already merged.
    pr_results = await github_ep.graphql(mutation_str, require_success=False, **inputs_args)
    for i, pr in enumerate(prs):
        result = pr_results["data"][prs_out[i]]["pullRequest"]
        if result is not None:
            pr.id = result["id"]
            pr.url = result["url"]


async def create_pull_requests(
    github_ep: github.GitHubEndpoint,
    repo_id: str,
    repo_info: GitHubRepoInfo,
    fork_info: GitHubRepoInfo,
    prs: List[PrInfo],
) -> None:
    """
    Create all pull requests given in prs and modify them to add the new pr node id and URL.
    """
    batch_size = github_ep.batch_size
    for i in range(0, len(prs), batch_size):
        await _create_pull_requests_batch(
            github_ep, repo_id, repo_info, fork_info, prs[i : i + batch_size]
        )


TRANSIENT_STATUSES = frozenset({500, 502, 503, 504})


async def _refresh_new_comment_ids(github_ep: github.GitHubEndpoint, prs: List[PrUpdate]) -> None:
    """Re-query comments for PRs with new (id=None) comments.

    If a comment with matching body text already exists on the PR, set its
    id in-place so the next mutation attempt uses updateIssueComment instead
    of addComment, avoiding duplicates.
    """
    prs_with_new = [pr for pr in prs if any(c.id is None for c in pr.comments)]
    if not prs_with_new:
        return

    node_args = get_args_dict([pr.id for pr in prs_with_new], "node")
    node_outs = get_result_args(len(prs_with_new), "node_out")
    arg_str = ", ".join(get_args_declaration(node_args, "ID!"))

    query_str = "".join(
        len(prs_with_new)
        * [
            "{}: node(id: ${}) {{ ... on PullRequest {{ comments(first: "
            + str(MAX_COMMENTS_TO_QUERY)
            + ") {{ nodes {{ body id }} }} }} }},"
        ]
    )
    query_str = query_str.format(*zip_and_flatten(node_outs, node_args.keys()))
    query = f"query ({arg_str}) {{ {query_str} }}"

    result = await github_ep.graphql(query, max_retries=1, **node_args)

    for pr, out in zip(prs_with_new, node_outs):
        pr_data = result["data"][out]
        existing = pr_data.get("comments", {}).get("nodes", []) if pr_data else []
        existing_by_body = {c["body"]: c["id"] for c in existing}
        for comment in pr.comments:
            if comment.id is None and comment.text in existing_by_body:
                comment.id = existing_by_body[comment.text]
                logging.info("Comment already posted on PR, converting to edit")


async def _update_pull_requests_batch(
    github_ep: github.GitHubEndpoint, prs: List[PrUpdate]
) -> None:
    # Build non-comment parts once (all idempotent, safe to retry as-is).
    inputs = []
    labels = []
    reviewers = []
    assignees = []
    convert_to_draft = []
    convert_from_draft = []
    for pr in prs:
        update_dict = {
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

    inputs_args = get_args_dict(inputs, "pr")
    prs_out = get_result_args(len(inputs), "pr_out")

    labels_args = get_args_dict(labels, "label")
    labels_out = get_result_args(len(labels), "label_out")

    reviewers_args = get_args_dict(reviewers, "rev")
    reviewers_out = get_result_args(len(reviewers), "rev_out")

    assignees_args = get_args_dict(assignees, "asn")
    assignees_out = get_result_args(len(assignees), "asn_out")

    to_draft_args = get_args_dict(convert_to_draft, "to_d")
    to_draft_out = get_result_args(len(convert_to_draft), "to_d_out")

    from_draft_args = get_args_dict(convert_from_draft, "from_d")
    from_draft_out = get_result_args(len(convert_from_draft), "from_d_out")

    update_str = "".join(
        len(inputs)
        * [
            """
            {}: updatePullRequest(input: ${}) {{
                clientMutationId
            }},"""
        ]
    )
    update_str = update_str.format(*zip_and_flatten(prs_out, inputs_args.keys()))

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
        *zip_and_flatten(reviewers_out, reviewers_args.keys())
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
    assignees_str = assignees_str.format(*zip_and_flatten(assignees_out, assignees_args.keys()))

    add_labels_str = "".join(
        len(labels_args)
        * [
            """
            {}: addLabelsToLabelable(input: ${}) {{
                clientMutationId
            }},"""
        ]
    )
    add_labels_str = add_labels_str.format(*zip_and_flatten(labels_out, labels_args.keys()))

    to_draft_str = "".join(
        len(convert_to_draft)
        * [
            """
            {}: convertPullRequestToDraft(input: ${}) {{
                clientMutationId
            }},"""
        ]
    )
    to_draft_str = to_draft_str.format(*zip_and_flatten(to_draft_out, to_draft_args.keys()))

    from_draft_str = "".join(
        len(convert_from_draft)
        * [
            """
            {}: markPullRequestReadyForReview(input: ${}) {{
                clientMutationId
            }},"""
        ]
    )
    from_draft_str = from_draft_str.format(*zip_and_flatten(from_draft_out, from_draft_args.keys()))

    idempotent_str = (
        f"{update_str}{request_reviewers_str}{assignees_str}{add_labels_str}"
        f"{to_draft_str}{from_draft_str}"
    )
    idempotent_decl = (
        get_args_declaration(inputs_args, "UpdatePullRequestInput!")
        + get_args_declaration(labels_args, "AddLabelsToLabelableInput!")
        + get_args_declaration(reviewers_args, "RequestReviewsInput!")
        + get_args_declaration(assignees_args, "AddAssigneesToAssignableInput!")
        + get_args_declaration(to_draft_args, "ConvertPullRequestToDraftInput!")
        + get_args_declaration(from_draft_args, "MarkPullRequestReadyForReviewInput!")
    )
    idempotent_kwargs = {
        **inputs_args,
        **reviewers_args,
        **assignees_args,
        **labels_args,
        **to_draft_args,
        **from_draft_args,
    }

    # Retry loop with idempotent addComment handling: between attempts,
    # re-query PR comments so already-posted ones become edits, not adds.
    max_retries = 3
    base_delay = 1.0
    for attempt in range(max_retries):
        # Build comment parts from current pr.comments state.
        # After _refresh_new_comment_ids, previously-new comments that were
        # already posted will have their id set, routing them to
        # updateIssueComment instead of addComment.
        comments = []
        edit_comments = []
        for pr in prs:
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

        comments_args = get_args_dict(comments, "com")
        comments_out = get_result_args(len(comments), "com_out")

        edit_comments_args = get_args_dict(edit_comments, "edit_com")
        edit_comments_out = get_result_args(len(edit_comments), "edit_com_out")

        arg_str = ", ".join(
            idempotent_decl
            + get_args_declaration(comments_args, "AddCommentInput!")
            + get_args_declaration(edit_comments_args, "UpdateIssueCommentInput!")
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
            *zip_and_flatten(comments_out, comments_args.keys())
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
            *zip_and_flatten(edit_comments_out, edit_comments_args.keys())
        )

        # addComment first so new comments appear at the top of the PR
        mutation_str = f"""
        mutation ({arg_str}) {{
            {add_comments_str}{idempotent_str}{edit_comments_str}
        }}"""

        try:
            await github_ep.graphql(
                mutation_str,
                max_retries=1,
                **comments_args,
                **idempotent_kwargs,
                **edit_comments_args,
            )
            return
        except RevupRequestException as e:
            if e.status not in TRANSIENT_STATUSES or attempt >= max_retries - 1:
                raise
            delay = base_delay * (2**attempt)
            logging.warning(
                "GitHub returned %d, retrying in %ss (attempt %d/%d)",
                e.status,
                delay,
                attempt + 1,
                max_retries,
            )
        except RevupGithubException as e:
            if "timeout" not in e.message or attempt >= max_retries - 1:
                raise
            delay = base_delay * (2**attempt)
            logging.warning(
                "GitHub GraphQL error (timeout), retrying in %ss (attempt %d/%d)",
                delay,
                attempt + 1,
                max_retries,
            )

        # Before retrying, check which new comments were already posted
        # and update their IDs so the next attempt edits instead of adds.
        await asyncio.gather(
            _refresh_new_comment_ids(github_ep, prs),
            asyncio.sleep(delay),
        )


async def _update_with_splitting(github_ep: github.GitHubEndpoint, prs: List[PrUpdate]) -> None:
    """
    Update prs, halving the batch and retrying on RESOURCE_LIMITS_EXCEEDED.

    RESOURCE_LIMITS_EXCEEDED is a *partial* success: GitHub applies some sub-mutations
    (including addComments) before running out of budget. Resending the same request as-is
    would re-post those comments, so we first run _refresh_new_comment_ids to convert
    already-posted comments into edits, then split the batch and retry the halves.
    Raises if a single PR's update still exceeds the limit (can't split PRs further).
    """
    try:
        await _update_pull_requests_batch(github_ep, prs)
    except RevupGithubException as e:
        if RESOURCE_LIMITS_EXCEEDED not in e.types or len(prs) <= 1:
            raise
        logging.warning(
            "GitHub GraphQL RESOURCE_LIMITS_EXCEEDED updating %d PRs, splitting batch in half",
            len(prs),
        )
        # Some sub-mutations already applied; convert posted comments to edits so the
        # retry doesn't duplicate them.
        await _refresh_new_comment_ids(github_ep, prs)
        mid = len(prs) // 2
        await _update_with_splitting(github_ep, prs[:mid])
        await _update_with_splitting(github_ep, prs[mid:])


async def update_pull_requests(github_ep: github.GitHubEndpoint, prs: List[PrUpdate]) -> None:
    """
    Update the given pull request contents, and also add reviewers and labels.
    """
    batch_size = github_ep.batch_size
    for i in range(0, len(prs), batch_size):
        await _update_with_splitting(github_ep, prs[i : i + batch_size])


RE_PR_URL = re.compile(
    r"^https://(?P<github_url>[^/]+)/(?P<owner>[^/]+)/(?P<name>[^/]+)/pull/(?P<number>[0-9]+)/?$"
)


GitHubPullRequestParams = NamedTuple(
    "GitHubPullRequestParams",
    [
        ("github_url", str),
        ("owner", str),
        ("name", str),
        ("number", int),
    ],
)


def parse_pull_request_url(pull_request: str) -> GitHubPullRequestParams:
    m = RE_PR_URL.match(pull_request)
    if not m:
        raise RuntimeError("Did not understand PR argument.  PR must be URL")

    github_url = m.group("github_url")
    owner = m.group("owner")
    name = m.group("name")
    number = int(m.group("number"))
    return GitHubPullRequestParams(github_url=github_url, owner=owner, name=name, number=number)


@asynccontextmanager
async def github_connection(
    git_ctx: git.Git, args: argparse.Namespace, conf: config.Config
) -> AsyncGenerator[Tuple, None]:
    from revup import github_real

    repo_info = await git_ctx.get_github_repo_info(
        github_url=args.github_url, remote_name=args.remote_name
    )

    if not repo_info.owner or not repo_info.name:
        raise RevupUsageException(
            f'Configured remote "{args.remote_name}" does not '
            "point to the a github repository! "
            "You can set it manually by running "
            f"`git remote set-url {args.remote_name} git@github.com:{{OWNER}}/{{PROJECT}}` "
            f"or change the configured remote in {conf.config_path}/"
        )

    fork_info = repo_info
    if args.fork_name and args.fork_name != args.remote_name:
        fork_info = await git_ctx.get_github_repo_info(
            github_url=args.github_url, remote_name=args.fork_name
        )

    if not fork_info.owner or not fork_info.name:
        raise RevupUsageException(
            f'Configured remote fork "{args.fork_info}" does not '
            "point to the a github repository! "
            "You can set it manually by running "
            f"`git remote set-url {args.fork_info} git@github.com:{{OWNER}}/{{PROJECT}}` "
            f"or change the configured remote in {conf.config_path}."
        )

    if repo_info.name != fork_info.name:
        raise RevupUsageException(
            f'Configured remote fork "{args.fork_info}" is not '
            f"the same repo as the remote {args.remote_info}."
        )

    if not args.github_oauth:
        # Try environment variables first
        args.github_oauth = os.environ.get("GITHUB_TOKEN")
        if args.github_oauth:
            logs.redact({args.github_oauth: "<GITHUB_OAUTH>"})
            logging.debug("Used GitHub token from environment variable")
        else:
            # Fall back to git credential helper
            args.github_oauth = await git_ctx.credential(
                protocol="https",
                host=args.github_url,
                path=f"{fork_info.owner}/{fork_info.name}.git",
            )
            if args.github_oauth:
                logs.redact({args.github_oauth: "<GITHUB_OAUTH>"})
                logging.debug("Used credential from git-credential")

    if not args.github_oauth:
        raise RevupUsageException(
            "No Github OAuth token found! "
            "Set the GITHUB_TOKEN environment variable, "
            "login with 'gh auth login', "
            "or make one at https://github.com/settings/tokens/new "
            "(revup needs full repo permissions) "
            "then set it with `revup config github_oauth`."
        )

    github_ep = github_real.RealGitHubEndpoint(
        oauth_token=args.github_oauth,
        proxy=args.proxy,
        github_url=args.github_url,
        batch_size=args.github_batch_size,
    )
    try:
        yield github_ep, repo_info, fork_info
    finally:
        await github_ep.close()


async def query_pr_by_number(
    github_ep: github.GitHubEndpoint,
    owner: str,
    name: str,
    number: int,
) -> Tuple[str, str]:
    """
    Query a pull request by number and return (headRefName, baseRefName).
    """
    result = await github_ep.graphql(
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
