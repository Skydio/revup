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
from revup.github.graphql import ErrorClass, GraphqlError, GraphqlQuery, GraphqlResponse
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


# How many times to resend a subquery that makes no progress (lone too-big field
# or a repeated timeout) before treating its errors as terminal.
_MAX_STALLED_RETRIES = 2


def _merge_data(into: Dict[str, Any], src: Any) -> None:
    """Merge GraphQL `data` dict `src` into `into`, combining nested repository fields."""
    if not src:
        return
    for key, val in src.items():
        if key == "repository" and isinstance(into.get("repository"), dict) and val:
            into["repository"].update(val)
        else:
            into[key] = val


class GithubQuery(GraphqlQuery):
    """A GraphqlQuery with GitHub-specific field builders and result parsers."""

    def add_pr_queries(self, head_refs: List[str]) -> None:
        for ref in head_refs:
            self.add(
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

    def add_user_queries(self, user_ids: List[str]) -> None:
        for uid in user_ids:
            self.add(
                prefix="user",
                scope="repo",
                field_template="{}: assignableUsers (query: {}, first: 25) {{...UserResult}},",
                var_types=["String!"],
                values=[uid],
                fragment=USER_FRAGMENT,
            )

    def add_label_queries(self, labels: List[str]) -> None:
        for label in labels:
            self.add(
                prefix="label",
                scope="repo",
                field_template="{}: label (name: {}) {{...LabelResult}},",
                var_types=["String!"],
                values=[label],
                fragment=LABEL_FRAGMENT,
            )

    def add_team_queries(self, teams: List[Tuple[str, str]]) -> None:
        for org, slug in teams:
            self.add(
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

    def parse_prs(self, result: Any, head_refs: List[str]) -> List[Optional[PrInfo]]:
        raw = self.extract(result, "pr")
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
                        reviewer_teams.add(
                            f"{requested['organization']['login']}/{requested['slug']}"
                        )
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

    def parse_users(
        self, result: Any, user_ids: List[str]
    ) -> Tuple[Dict[str, str], Dict[str, str]]:
        raw = self.extract(result, "user")
        names_to_ids: Dict[str, str] = {}
        names_to_logins: Dict[str, str] = {}
        for i, user_id in enumerate(user_ids):
            this_node = raw[i]
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
        return names_to_ids, names_to_logins

    def parse_labels(self, result: Any, labels: List[str]) -> Dict[str, str]:
        raw = self.extract(result, "label")
        labels_to_ids: Dict[str, str] = {}
        for i, label in enumerate(labels):
            this_node = raw[i]
            if this_node is not None:
                labels_to_ids[label] = this_node["id"]
            else:
                logging.warning("Couldn't find an existing label named {}".format(label))
        return labels_to_ids

    def parse_teams(
        self, result: Any, teams: List[Tuple[str, str]]
    ) -> Tuple[Dict[str, str], Dict[str, Optional[Set[str]]]]:
        raw = self.extract(result, "team")
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
    ) -> GithubQuery:
        q = GithubQuery(name="GetEverything")
        q.add_fixed_var("owner", "String!", self.repo_info.owner)
        q.add_fixed_var("name", "String!", self.repo_info.name)
        q.fixed_repo_fields = "id\n"

        q.add_pr_queries(head_refs)
        q.add_user_queries(user_ids)
        q.add_label_queries(labels)
        q.add_team_queries(teams)

        return q

    async def _run_once(self, q: GraphqlQuery) -> GraphqlResponse:
        query_str, variables = q.build()
        return await self.endpoint.graphql(query_str, **variables)

    async def _execute(self, q: GraphqlQuery) -> Dict[str, Any]:
        """Run a query/mutation, salvaging partial results and re-transacting the rest.

        Returns the merged `data` for every field that ultimately succeeded. Fields
        that GitHub couldn't compute (resource limits) are re-run — split smaller if a
        field still fails alone. Fields whose effect already exists are treated as
        done. Any other (terminal) error is raised.

        Only never-computed fields are ever re-sent, so mutations with real side
        effects (created PRs, posted comments) are never re-executed.
        """
        merged: Dict[str, Any] = {}
        terminal: List[Any] = []
        # Queue of (subquery, attempts_without_progress). Start with the whole thing.
        pending: List[Tuple[GraphqlQuery, int]] = [(q, 0)]
        while pending:
            sub, stalls = pending.pop()
            if sub.total_items() == 0:
                continue
            resp = await self._run_once(sub)
            _merge_data(merged, resp.data)

            # A field with a non-null result completed (for a mutation, its side
            # effect applied); a null field did not. This is the source of truth for
            # what to resubmit, so a completed mutation is never re-sent.
            unfulfilled = sub.unfulfilled_aliases({"data": merged})
            errors_by_alias: Dict[str, GraphqlError] = {}
            timed_out = False
            for err in resp.errors:
                if err.error_class is ErrorClass.INFORMATIONAL:
                    continue  # e.g. a deprecation warning; response still succeeded
                if err.alias:
                    errors_by_alias[err.alias] = err
                elif err.is_timeout:
                    timed_out = True  # a whole-request timeout names no field
                else:
                    terminal.append(err.raw)  # request-level error with nothing to salvage

            resubmit: Set[str] = set()
            too_big = False  # a resource limit means the request must shrink, not just retry
            for alias in unfulfilled:
                field_err = errors_by_alias.get(alias)
                if field_err is None:
                    # No per-field error: resubmit only if the whole request timed out
                    # before reaching it, else it's a legitimate null (e.g. missing label).
                    if timed_out:
                        resubmit.add(alias)
                elif field_err.error_class is ErrorClass.ALREADY_DONE:
                    continue  # effect already exists; nothing to redo
                elif field_err.error_class is ErrorClass.RETRYABLE:
                    resubmit.add(alias)
                    too_big = True
                elif field_err.is_timeout:
                    resubmit.add(alias)
                else:
                    terminal.append(field_err.raw)

            if not resubmit:
                continue
            retry = sub.subset(resubmit)
            made_progress = retry.total_items() < sub.total_items()
            if too_big and not made_progress and retry.total_items() > 1:
                # No progress and too big: halve to shrink the request.
                left, right = retry.split()
                logging.warning(
                    "Request too large, splitting {} fields into {} + {}".format(
                        retry.total_items(), left.total_items(), right.total_items()
                    )
                )
                pending.extend([(left, 0), (right, 0)])
            elif made_progress:
                # Some fields completed; resubmit the remainder fresh.
                pending.append((retry, 0))
            elif stalls < _MAX_STALLED_RETRIES:
                # No progress (a lone too-big field, or a repeated timeout): retry a
                # bounded number of times before giving up.
                pending.append((retry, stalls + 1))
            else:
                # Give up. Surface a terminal error for each field that never
                # completed — synthesizing one for timeouts, which name no field —
                # so a stalled request never silently drops work.
                for alias in resubmit:
                    stalled = errors_by_alias.get(alias)
                    terminal.append(
                        stalled.raw
                        if stalled is not None
                        else {"message": "Request repeatedly failed to complete: {}".format(alias)}
                    )

        if terminal:
            raise RevupForgeException(terminal)
        return {"data": merged}

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
        q = self._make_query_everything(head_refs, user_ids, labels, teams)

        result = await self._execute(q)

        repo_id = result["data"]["repository"]["id"]
        prs = q.parse_prs(result, head_refs)
        names_to_ids, names_to_logins = q.parse_users(result, user_ids)
        labels_to_ids = q.parse_labels(result, labels)
        teams_to_ids, teams_to_members = q.parse_teams(result, teams)

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
            headRef = pr.headRef if not self.is_fork else f"{self.fork_info.owner}:{pr.headRef}"
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
        for inp in inputs:
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

        pr_results = await self._execute(q)
        raw = q.extract(pr_results, "pr")
        for i, pr in enumerate(prs):
            result_node = raw[i]["pullRequest"] if raw[i] is not None else None
            if result_node is not None:
                pr.id = result_node["id"]
                pr.url = result_node["url"]

        # A create that came back "already exists" (from a prior partial run) leaves
        # id/url unset. The PR does exist, so look it up by head ref — otherwise a
        # downstream update would target an empty id and fail with NOT_FOUND.
        missing = [pr for pr in prs if not pr.id]
        if missing:
            await self._populate_existing_pr_ids(missing)

    async def _populate_existing_pr_ids(self, prs: List[PrInfo]) -> None:
        q = GithubQuery(name="FindExisting")
        q.add_fixed_var("owner", "String!", self.repo_info.owner)
        q.add_fixed_var("name", "String!", self.repo_info.name)
        q.fixed_repo_fields = "id\n"
        q.add_pr_queries([pr.headRef for pr in prs])

        result = await self._execute(q)
        for pr, node in zip(prs, q.extract(result, "pr")):
            nodes = node["nodes"] if node else []
            if nodes:
                pr.id = nodes[0]["id"]
                pr.url = nodes[0]["url"]

    async def update_pull_requests(self, prs: List[PrUpdate]) -> None:
        # _execute salvages partial success and re-transacts only never-applied
        # fields, so a mutation is never re-sent once its effect exists — no need
        # to reconcile already-posted comments.
        await self._execute(self._build_update_mutation(prs))

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
        pr = result.data["repository"]["pullRequest"]
        return pr["headRefName"], pr["baseRefName"]
