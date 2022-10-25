import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Set, Tuple

from revup import github
from revup.git import GitHubRepoInfo
from revup.types import GitCommitHash

MAX_COMMENTS_TO_QUERY = 3


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
    baseRefOid: GitCommitHash
    headRefOid: GitCommitHash
    body: str
    title: str
    id: str = ""
    url: str = ""
    state: str = ""
    reviewers: Set[str] = field(default_factory=set)
    reviewer_ids: Set[str] = field(default_factory=set)
    assignees: Set[str] = field(default_factory=set)
    assignee_ids: Set[str] = field(default_factory=set)
    labels: Set[str] = field(default_factory=set)
    label_ids: Set[str] = field(default_factory=set)
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


async def query_everything(
    github_ep: github.GitHubEndpoint,
    repo_info: GitHubRepoInfo,
    head_refs: List[str],
    user_ids: List[str],
    labels: List[str],
) -> Tuple[str, List[Optional[PrInfo]], Dict[str, str], Dict[str, str], Dict[str, str]]:
    """
    This function does all necessary graphql querying in one request. This dramatically reduces the
    amount of time spent on querying.

    Returns a tuple of:
    - Repository node id
    - List of pull requests, one for each ref in head_refs. None if a pr wasn't found for that ref
    - Dict of user_ids as given to graphql node ids
    - Dict of user_ids as given to their full login name
    - Dict of labels to their graphql node ids
    """
    head_refs_args = get_args_dict(head_refs, "pr")
    user_id_args = get_args_dict(user_ids, "user")
    label_args = get_args_dict(labels, "label")

    prs_out = get_result_args(len(head_refs), "pr_out")
    user_id_out = get_result_args(len(user_ids), "user_out")
    label_out = get_result_args(len(labels), "label_out")

    arg_str = ", ".join(
        get_args_declaration(head_refs_args, "String!")
        + get_args_declaration(user_id_args, "String!")
        + get_args_declaration(label_args, "String!")
    )

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

    user_str = "".join(
        len(user_ids) * ["{}: assignableUsers (query: ${}, first: 25) {{...UserResult}},"]
    )
    user_str = user_str.format(*zip_and_flatten(user_id_out, user_id_args.keys()))

    label_str = "".join(len(labels) * ["{}: label (name: ${}) {{...LabelResult}},"])
    label_str = label_str.format(*zip_and_flatten(label_out, label_args.keys()))

    multi_query_str = f"""
        query GetPrResults($owner: String!, $name: String!, {arg_str}) {{
            repository(name: $name, owner: $owner) {{
                id
                {request_str}{user_str}{label_str}
            }}
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
                headRefOid
                body
                title
                isDraft
                updatedAt
                commits (first: 1) {{
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
                reviewRequests (first: 25) {{
                    nodes {{
                        requestedReviewer {{
                            ... on User {{
                                login
                                id
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
        multi_query_str,
        owner=repo_info.owner,
        name=repo_info.name,
        **head_refs_args,
        **user_id_args,
        **label_args,
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
            assignees = set()
            assignee_ids = set()
            for label in this_node["labels"]["nodes"]:
                pr_labels.add(label["name"])
                pr_label_ids.add(label["id"])
            for revs in this_node["reviewRequests"]["nodes"]:
                if not revs["requestedReviewer"]:
                    continue
                elif "login" in revs["requestedReviewer"]:
                    reviewers.add(revs["requestedReviewer"]["login"])
                    reviewer_ids.add(revs["requestedReviewer"]["id"])
            for revs in this_node["latestReviews"]["nodes"]:
                if not revs["viewerDidAuthor"]:
                    reviewers.add(revs["author"]["login"])
                    reviewer_ids.add(revs["author"]["id"])
            for user in this_node["assignees"]["nodes"]:
                assignees.add(user["login"])
                assignee_ids.add(user["id"])
            headRefOid = this_node["headRefOid"]
            # Github's "baseRefOid" in the api field returns the ToT commit for the base ref
            # which isn't what we want, since that commit may not exist locally. Instead
            # we get the parent of the first commit, which is the base ref it was actually
            # uploaded against.
            baseRefOid = (
                headRefOid
                if not this_node["commits"]["nodes"]
                else this_node["commits"]["nodes"][0]["commit"]["parents"]["nodes"][0]["oid"]
            )

            comments = []
            for c in this_node["comments"]["nodes"]:
                comments.append(PrComment(c["body"], c["id"]))

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
                    assignees=assignees,
                    assignee_ids=assignee_ids,
                    labels=pr_labels,
                    label_ids=pr_label_ids,
                    is_draft=this_node["isDraft"],
                    state=this_node["state"],
                    comments=comments,
                )
            )
        else:
            prs.append(None)

    names_to_ids = {}
    names_to_logins = {}
    for i, user_id in enumerate(user_ids):
        this_node = pr_result["data"]["repository"][user_id_out[i]]
        if len(this_node["nodes"]) == 0:
            logging.warning("No matching user found for {}".format(user_id))
        elif this_node["totalCount"] > len(this_node["nodes"]):
            logging.warning("Too many matching users found for {}".format(user_id))
        else:
            shortest_name = this_node["nodes"][0]["login"]
            names_to_ids[user_id] = this_node["nodes"][0]["id"]
            for user in this_node["nodes"]:
                if len(user["login"]) <= len(shortest_name):
                    shortest_name = user["login"]
                    names_to_ids[user_id] = user["id"]
                    names_to_logins[user_id] = user["login"]

    labels_to_ids = {}
    for i, label in enumerate(labels):
        this_node = pr_result["data"]["repository"][label_out[i]]
        if this_node is not None:
            labels_to_ids[label] = this_node["id"]
        else:
            logging.warning("Couldn't find an existing label named {}".format(label))

    return (
        pr_result["data"]["repository"]["id"],
        prs,
        names_to_ids,
        names_to_logins,
        labels_to_ids,
    )


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
    inputs = []
    for pr in prs:
        headRef = (
            pr.headRef if fork_info.owner == repo_info.owner else f"{fork_info.owner}:{pr.headRef}"
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


async def update_pull_requests(github_ep: github.GitHubEndpoint, prs: List[PrUpdate]) -> None:
    """
    Update the given pull request contents, and also add reviewers and labels.
    """
    inputs = []
    labels = []
    reviewers = []
    assignees = []
    convert_to_draft = []
    convert_from_draft = []
    comments = []
    edit_comments = []
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
            labels.append(
                {
                    "labelIds": list(pr.label_ids),
                    "clientMutationId": "revup",
                    "labelableId": pr.id,
                }
            )

        if pr.reviewer_ids:
            reviewers.append(
                {
                    "userIds": list(pr.reviewer_ids),
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

    comments_args = get_args_dict(comments, "com")
    comments_out = get_result_args(len(comments), "com_out")

    edit_comments_args = get_args_dict(edit_comments, "edit_com")
    edit_comments_out = get_result_args(len(edit_comments), "edit_com_out")

    arg_str = ", ".join(
        get_args_declaration(inputs_args, "UpdatePullRequestInput!")
        + get_args_declaration(labels_args, "AddLabelsToLabelableInput!")
        + get_args_declaration(reviewers_args, "RequestReviewsInput!")
        + get_args_declaration(assignees_args, "AddAssigneesToAssignableInput!")
        + get_args_declaration(to_draft_args, "ConvertPullRequestToDraftInput!")
        + get_args_declaration(from_draft_args, "MarkPullRequestReadyForReviewInput!")
        + get_args_declaration(comments_args, "AddCommentInput!")
        + get_args_declaration(edit_comments_args, "UpdateIssueCommentInput!")
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

    add_comments_str = "".join(
        len(comments_args)
        * [
            """
            {}: addComment(input: ${}) {{
                clientMutationId
            }},"""
        ]
    )
    add_comments_str = add_comments_str.format(*zip_and_flatten(comments_out, comments_args.keys()))

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

    # Have any add comment mutations first in order to ensure that comments are at the top of the PR
    mutation_str = f"""
        mutation ({arg_str}) {{
            {add_comments_str}{update_str}{request_reviewers_str}{assignees_str}{add_labels_str}{to_draft_str}{from_draft_str}{edit_comments_str}
        }}"""

    await github_ep.graphql(
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
