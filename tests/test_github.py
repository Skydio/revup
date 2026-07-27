"""Unit tests for the Github class with a mocked GraphQL endpoint."""

import asyncio
import time
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from revup.core_types import RevupForgeException, RevupRequestException
from revup.forge import ForgeRepoInfo, PrComment, PrInfo, PrUpdate
from revup.github.endpoint import GitHubEndpoint, _backoff_delay
from revup.github.github import _MAX_STALLED_RETRIES, Github, _merge_data
from revup.github.graphql import GraphqlResponse


def gql(data, errors=None):
    """Build the GraphqlResponse that endpoint.graphql now returns."""
    return GraphqlResponse.parse({"data": data, "errors": errors or []})


def gql_raw(raw):
    """Wrap a raw {data, errors} dict as the GraphqlResponse endpoint.graphql returns."""
    return GraphqlResponse.parse(raw)


def make_github(endpoint: GitHubEndpoint, fork_owner: str = "owner") -> Github:
    return Github(
        endpoint=endpoint,
        repo_info=ForgeRepoInfo(owner="owner", name="repo"),
        fork_info=ForgeRepoInfo(owner=fork_owner, name="repo"),
    )


def make_pr_node(
    pr_id: str = "PR_1",
    url: str = "https://github.com/owner/repo/pull/1",
    state: str = "OPEN",
    base_ref: str = "main",
    head_oid: str = "abc123",
    base_oid: str = "def456",
    is_draft: bool = False,
    reviewers: List[Dict] = None,
    team_reviewers: List[Dict] = None,
    latest_reviews: List[Dict] = None,
    assignees: List[Dict] = None,
    labels: List[Dict] = None,
    comments: List[Dict] = None,
    timeline_items: List[Dict] = None,
) -> Dict[str, Any]:
    review_requests = []
    for r in reviewers or []:
        review_requests.append({"requestedReviewer": r})
    for t in team_reviewers or []:
        review_requests.append({"requestedReviewer": t})

    return {
        "nodes": [
            {
                "id": pr_id,
                "state": state,
                "url": url,
                "baseRefName": base_ref,
                "body": "body",
                "title": "title",
                "isDraft": is_draft,
                "baseCommit": {"nodes": [{"commit": {"parents": {"nodes": [{"oid": base_oid}]}}}]},
                "headCommit": {"nodes": [{"commit": {"oid": head_oid}}]},
                "reviewRequests": {"nodes": review_requests},
                "timelineItems": {"nodes": timeline_items or []},
                "latestReviews": {"nodes": latest_reviews or []},
                "assignees": {"nodes": assignees or []},
                "labels": {"nodes": labels or []},
                "comments": {"nodes": comments or []},
            }
        ],
        "totalCount": 1,
    }


def make_user_node(login: str = "alice", node_id: str = "U_1") -> Dict[str, Any]:
    return {"nodes": [{"login": login, "id": node_id}], "totalCount": 1}


def make_label_node(name: str = "bug", node_id: str = "L_1") -> Dict[str, Any]:
    return {"id": node_id, "name": name}


def make_team_node(team_id: str = "T_1", members: List[str] = None) -> Dict[str, Any]:
    if members is None:
        members = ["alice"]
    return {
        "team": {
            "id": team_id,
            "members": {
                "nodes": [{"login": m} for m in members],
                "totalCount": len(members),
            },
        }
    }


class TestQueryEverything:
    def test_parses_reviewers_and_teams(self):
        """Verifies user reviewers, team reviewers, and latestReviews are all extracted."""
        endpoint = AsyncMock(spec=GitHubEndpoint)
        endpoint.graphql = AsyncMock(
            return_value=gql_raw(
                {
                    "data": {
                        "repository": {
                            "id": "R_1",
                            "pr_out0": make_pr_node(
                                reviewers=[{"login": "alice", "id": "U_1"}],
                                team_reviewers=[
                                    {
                                        "slug": "backend",
                                        "id": "T_1",
                                        "organization": {"login": "acme"},
                                    }
                                ],
                                latest_reviews=[
                                    {
                                        "author": {"login": "bob", "id": "U_2"},
                                        "viewerDidAuthor": False,
                                    },
                                    {
                                        "author": {"login": "me", "id": "U_3"},
                                        "viewerDidAuthor": True,
                                    },
                                ],
                            ),
                        },
                    }
                }
            )
        )
        gh = make_github(endpoint)

        _, prs, _, _, _, _, _ = asyncio.run(
            gh.query_everything(head_refs=["feat"], user_ids=[], labels=[], teams=[])
        )

        pr = prs[0]
        assert pr.reviewers == {"alice", "bob"}
        assert pr.reviewer_ids == {"U_1", "U_2"}
        assert pr.reviewer_teams == {"acme/backend"}
        assert pr.reviewer_team_ids == {"T_1"}

    def test_parses_removed_reviewers_from_timeline(self):
        """Removed review requests show up only if the user isn't currently a reviewer."""
        endpoint = AsyncMock(spec=GitHubEndpoint)
        endpoint.graphql = AsyncMock(
            return_value=gql_raw(
                {
                    "data": {
                        "repository": {
                            "id": "R_1",
                            "pr_out0": make_pr_node(
                                reviewers=[{"login": "alice", "id": "U_1"}],
                                timeline_items=[
                                    {"requestedReviewer": {"login": "alice", "id": "U_1"}},
                                    {"requestedReviewer": {"login": "carol", "id": "U_3"}},
                                    {"assignee": {"login": "dave", "id": "U_4"}},
                                ],
                            ),
                        },
                    }
                }
            )
        )
        gh = make_github(endpoint)

        _, prs, _, _, _, _, _ = asyncio.run(
            gh.query_everything(head_refs=["feat"], user_ids=[], labels=[], teams=[])
        )

        pr = prs[0]
        # alice is still a reviewer so not in removed set
        assert "alice" not in pr.removed_reviewers
        assert pr.removed_reviewers == {"carol"}
        assert pr.removed_reviewer_ids == {"U_3"}
        assert pr.removed_assignees == {"dave"}
        assert pr.removed_assignee_ids == {"U_4"}

    def test_parses_labels_assignees_comments(self):
        endpoint = AsyncMock(spec=GitHubEndpoint)
        endpoint.graphql = AsyncMock(
            return_value=gql_raw(
                {
                    "data": {
                        "repository": {
                            "id": "R_1",
                            "pr_out0": make_pr_node(
                                assignees=[{"login": "alice", "id": "U_1"}],
                                labels=[{"name": "urgent", "id": "L_1"}],
                                comments=[
                                    {"body": "hello", "id": "C_1"},
                                    {"body": "world", "id": "C_2"},
                                ],
                            ),
                        },
                    }
                }
            )
        )
        gh = make_github(endpoint)

        _, prs, _, _, _, _, _ = asyncio.run(
            gh.query_everything(head_refs=["feat"], user_ids=[], labels=[], teams=[])
        )

        pr = prs[0]
        assert pr.assignees == {"alice"}
        assert pr.labels == {"urgent"}
        assert pr.label_ids == {"L_1"}
        assert len(pr.comments) == 2
        assert pr.comments[0] == PrComment("hello", "C_1")

    def test_draft_state_preserved(self):
        endpoint = AsyncMock(spec=GitHubEndpoint)
        endpoint.graphql = AsyncMock(
            return_value=gql_raw(
                {
                    "data": {
                        "repository": {
                            "id": "R_1",
                            "pr_out0": make_pr_node(is_draft=True, state="OPEN"),
                        },
                    }
                }
            )
        )
        gh = make_github(endpoint)

        _, prs, _, _, _, _, _ = asyncio.run(
            gh.query_everything(head_refs=["feat"], user_ids=[], labels=[], teams=[])
        )

        assert prs[0].is_draft is True
        assert prs[0].state == "OPEN"

    def test_user_prefix_match_picks_shortest(self):
        """When multiple users match, picks the shortest login that starts with the query."""
        endpoint = AsyncMock(spec=GitHubEndpoint)
        endpoint.graphql = AsyncMock(
            return_value=gql_raw(
                {
                    "data": {
                        "repository": {
                            "id": "R_1",
                            "user_out0": {
                                "nodes": [
                                    {"login": "alice-long", "id": "U_long"},
                                    {"login": "alice", "id": "U_exact"},
                                    {"login": "alice-longer", "id": "U_longer"},
                                ],
                                "totalCount": 3,
                            },
                        },
                    }
                }
            )
        )
        gh = make_github(endpoint)

        _, _, names_to_ids, names_to_logins, _, _, _ = asyncio.run(
            gh.query_everything(head_refs=[], user_ids=["alice"], labels=[], teams=[])
        )

        assert names_to_ids["alice"] == "U_exact"
        assert names_to_logins["alice"] == "alice"

    def test_user_no_prefix_match_falls_back_to_first(self):
        """If no login starts with the query, falls back to first result."""
        endpoint = AsyncMock(spec=GitHubEndpoint)
        endpoint.graphql = AsyncMock(
            return_value=gql_raw(
                {
                    "data": {
                        "repository": {
                            "id": "R_1",
                            "user_out0": {
                                "nodes": [
                                    {"login": "bob", "id": "U_bob"},
                                    {"login": "carol", "id": "U_carol"},
                                ],
                                "totalCount": 2,
                            },
                        },
                    }
                }
            )
        )
        gh = make_github(endpoint)

        _, _, names_to_ids, names_to_logins, _, _, _ = asyncio.run(
            gh.query_everything(head_refs=[], user_ids=["al"], labels=[], teams=[])
        )

        # Falls back to first node's id, but login NOT added to names_to_logins
        assert names_to_ids["al"] == "U_bob"
        assert "al" not in names_to_logins

    def test_team_with_too_many_members_returns_none(self):
        """When totalCount > returned nodes, members set is None (can't enumerate all)."""
        endpoint = AsyncMock(spec=GitHubEndpoint)
        endpoint.graphql = AsyncMock(
            return_value=gql_raw(
                {
                    "data": {
                        "repository": {"id": "R_1"},
                        "team_out0": {
                            "team": {
                                "id": "T_1",
                                "members": {
                                    "nodes": [{"login": "alice"}],
                                    "totalCount": 150,
                                },
                            }
                        },
                    }
                }
            )
        )
        gh = make_github(endpoint)

        _, _, _, _, _, teams_to_ids, teams_to_members = asyncio.run(
            gh.query_everything(head_refs=[], user_ids=[], labels=[], teams=[("org", "big")])
        )

        assert teams_to_ids == {"org/big": "T_1"}
        assert teams_to_members["org/big"] is None

    def test_resource_limit_salvages_partial_and_retries_rest(self):
        """First response resolves pr_out0 but nulls pr_out1 with a resource-limit
        error. The retry must request ONLY pr_out1 (never re-request pr_out0)."""
        endpoint = AsyncMock(spec=GitHubEndpoint)
        requested_aliases = []

        async def mock_graphql(query, **kwargs):
            aliases = sorted(f"pr_out{k[2:]}" for k in kwargs if k.startswith("pr"))
            requested_aliases.append(aliases)
            if aliases == ["pr_out0", "pr_out1"]:
                # Partial: pr_out0 resolved, pr_out1 hit the resource limit.
                return gql(
                    {
                        "repository": {
                            "id": "R_1",
                            "pr_out0": make_pr_node("PR_b1"),
                            "pr_out1": None,
                        }
                    },
                    errors=[
                        {
                            "type": "RESOURCE_LIMITS_EXCEEDED",
                            "path": ["pr_out1", "pullRequest"],
                            "message": "Resource limits for this query exceeded.",
                        }
                    ],
                )
            # Retry of just pr_out1.
            return gql({"repository": {"pr_out1": make_pr_node("PR_b2")}})

        endpoint.graphql = mock_graphql
        gh = make_github(endpoint)

        repo_id, prs, _, _, _, _, _ = asyncio.run(
            gh.query_everything(head_refs=["b1", "b2"], user_ids=[], labels=[], teams=[])
        )

        assert repo_id == "R_1"
        assert prs[0].id == "PR_b1"
        assert prs[1].id == "PR_b2"
        # The retry requested only the failed alias, never re-requesting pr_out0.
        assert requested_aliases == [["pr_out0", "pr_out1"], ["pr_out1"]]

    def test_terminal_field_error_raises(self):
        """A non-retryable field error (NOT_FOUND) surfaces as a forge exception."""
        endpoint = AsyncMock(spec=GitHubEndpoint)
        endpoint.graphql = AsyncMock(
            return_value=gql(
                {"repository": {"id": "R_1", "pr_out0": None}},
                errors=[{"type": "NOT_FOUND", "path": ["pr_out0"], "message": "not found"}],
            )
        )
        gh = make_github(endpoint)

        with pytest.raises(RevupForgeException):
            asyncio.run(gh.query_everything(head_refs=["b1"], user_ids=[], labels=[], teams=[]))

    def test_empty_pr_result(self):
        endpoint = AsyncMock(spec=GitHubEndpoint)
        endpoint.graphql = AsyncMock(
            return_value=gql_raw(
                {
                    "data": {
                        "repository": {
                            "id": "R_1",
                            "pr_out0": {"nodes": [], "totalCount": 0},
                        }
                    }
                }
            )
        )
        gh = make_github(endpoint)

        _, prs, _, _, _, _, _ = asyncio.run(
            gh.query_everything(head_refs=["no-pr"], user_ids=[], labels=[], teams=[])
        )

        assert prs == [None]

    def test_multiple_prs_each_mapped_to_correct_branch(self):
        """Each head_ref maps to its corresponding PR result by index."""
        endpoint = AsyncMock(spec=GitHubEndpoint)
        endpoint.graphql = AsyncMock(
            return_value=gql_raw(
                {
                    "data": {
                        "repository": {
                            "id": "R_1",
                            "pr_out0": make_pr_node(pr_id="PR_A", base_ref="develop"),
                            "pr_out1": {"nodes": [], "totalCount": 0},
                            "pr_out2": make_pr_node(pr_id="PR_C", base_ref="main"),
                        },
                    }
                }
            )
        )
        gh = make_github(endpoint)

        _, prs, _, _, _, _, _ = asyncio.run(
            gh.query_everything(
                head_refs=["branch-a", "branch-b", "branch-c"],
                user_ids=[],
                labels=[],
                teams=[],
            )
        )

        assert prs[0].id == "PR_A"
        assert prs[0].headRef == "branch-a"
        assert prs[0].baseRef == "develop"
        assert prs[1] is None
        assert prs[2].id == "PR_C"
        assert prs[2].headRef == "branch-c"


class TestCreatePullRequests:
    def test_fork_mode_prefixes_head_ref(self):
        """When fork_info.owner differs from repo_info.owner, headRef gets owner: prefix."""
        endpoint = AsyncMock(spec=GitHubEndpoint)
        endpoint.graphql = AsyncMock(
            return_value=gql_raw(
                {
                    "data": {
                        "pr_out0": {"pullRequest": {"id": "PR_1", "url": "url1"}},
                    }
                }
            )
        )
        gh = make_github(endpoint, fork_owner="myfork")

        pr = PrInfo(
            baseRef="main", headRef="feat1", baseRefOid=None, headRefOid=None, body="b", title="t"
        )
        asyncio.run(gh.create_pull_requests("R_1", [pr]))

        _, kwargs = endpoint.graphql.call_args
        assert kwargs["pr0"]["headRefName"] == "myfork:feat1"
        assert kwargs["pr0"]["baseRefName"] == "main"

    def test_same_owner_no_prefix(self):
        endpoint = AsyncMock(spec=GitHubEndpoint)
        endpoint.graphql = AsyncMock(
            return_value=gql_raw(
                {"data": {"pr_out0": {"pullRequest": {"id": "PR_1", "url": "url1"}}}}
            )
        )
        gh = make_github(endpoint, fork_owner="owner")

        pr = PrInfo(
            baseRef="main", headRef="feat1", baseRefOid=None, headRefOid=None, body="b", title="t"
        )
        asyncio.run(gh.create_pull_requests("R_1", [pr]))

        _, kwargs = endpoint.graphql.call_args
        assert kwargs["pr0"]["headRefName"] == "feat1"

    def test_draft_flag_passed_through(self):
        endpoint = AsyncMock(spec=GitHubEndpoint)
        endpoint.graphql = AsyncMock(
            return_value=gql_raw({"data": {"pr_out0": {"pullRequest": {"id": "PR_1", "url": "u"}}}})
        )
        gh = make_github(endpoint)

        pr = PrInfo(
            baseRef="main",
            headRef="feat1",
            baseRefOid=None,
            headRefOid=None,
            body="b",
            title="t",
            is_draft=True,
        )
        asyncio.run(gh.create_pull_requests("R_1", [pr]))

        _, kwargs = endpoint.graphql.call_args
        assert kwargs["pr0"]["draft"] is True

    def test_already_exists_recovers_id_by_head_ref(self):
        """A create that comes back null (already exists) must recover the real PR id
        via a head-ref lookup, so downstream updates don't target an empty id."""
        endpoint = AsyncMock(spec=GitHubEndpoint)
        calls = []

        async def mock_graphql(query, **kwargs):
            calls.append(query)
            if "createPullRequest" in query:
                # Already exists: GitHub returns null for the created pullRequest.
                return gql(
                    {"pr_out0": {"pullRequest": None}},
                    errors=[
                        {
                            "type": "UNPROCESSABLE",
                            "path": ["pr_out0"],
                            "message": "A pull request already exists for owner:feat1.",
                        }
                    ],
                )
            # The follow-up lookup by head ref finds the existing PR.
            return gql(
                {"repository": {"id": "R_1", "pr_out0": make_pr_node("PR_existing", url="u_ex")}}
            )

        endpoint.graphql = mock_graphql
        gh = make_github(endpoint)

        pr = PrInfo(
            baseRef="main", headRef="feat1", baseRefOid=None, headRefOid=None, body="b", title="t"
        )
        asyncio.run(gh.create_pull_requests("R_1", [pr]))

        assert pr.id == "PR_existing"
        assert pr.url == "u_ex"
        assert len(calls) == 2  # the create, then the head-ref lookup

    def test_multiple_prs_batched_in_one_call(self):
        endpoint = AsyncMock(spec=GitHubEndpoint)
        endpoint.graphql = AsyncMock(
            return_value=gql_raw(
                {
                    "data": {
                        "pr_out0": {"pullRequest": {"id": "PR_1", "url": "u1"}},
                        "pr_out1": {"pullRequest": {"id": "PR_2", "url": "u2"}},
                        "pr_out2": {"pullRequest": {"id": "PR_3", "url": "u3"}},
                    }
                }
            )
        )
        gh = make_github(endpoint)

        prs = [
            PrInfo(
                baseRef="main",
                headRef=f"f{i}",
                baseRefOid=None,
                headRefOid=None,
                body="b",
                title=f"t{i}",
            )
            for i in range(3)
        ]
        asyncio.run(gh.create_pull_requests("R_1", prs))

        endpoint.graphql.assert_called_once()
        assert prs[0].id == "PR_1"
        assert prs[1].id == "PR_2"
        assert prs[2].id == "PR_3"


class TestUpdatePullRequests:
    def test_builds_all_mutation_types(self):
        """Labels, reviewers, assignees, draft conversion, comments all go in one mutation."""
        endpoint = AsyncMock(spec=GitHubEndpoint)
        endpoint.graphql = AsyncMock(return_value=gql_raw({"data": {}}))
        gh = make_github(endpoint)

        updates = [
            PrUpdate(
                id="PR_1",
                title="new title",
                body="new body",
                label_ids={"L_1"},
                reviewer_ids={"U_1"},
                reviewer_team_ids={"T_1"},
                assignee_ids={"U_2"},
                is_draft=True,
                comments=[PrComment("new comment"), PrComment("edit me", "C_1")],
            ),
        ]

        asyncio.run(gh.update_pull_requests(updates))

        _, kwargs = endpoint.graphql.call_args
        assert kwargs["pr0"]["title"] == "new title"
        assert kwargs["pr0"]["body"] == "new body"
        assert kwargs["label0"]["labelIds"] == ["L_1"]
        assert kwargs["rev0"]["userIds"] == ["U_1"]
        assert kwargs["rev0"]["teamIds"] == ["T_1"]
        assert kwargs["asn0"]["assigneeIds"] == ["U_2"]
        assert kwargs["to_d0"]["pullRequestId"] == "PR_1"
        assert kwargs["com0"]["body"] == "new comment"
        assert kwargs["com0"]["subjectId"] == "PR_1"
        assert kwargs["edit_com0"]["body"] == "edit me"
        assert kwargs["edit_com0"]["id"] == "C_1"

    def test_ready_for_review_mutation(self):
        """is_draft=False generates markPullRequestReadyForReview, not convertToDraft."""
        endpoint = AsyncMock(spec=GitHubEndpoint)
        endpoint.graphql = AsyncMock(return_value=gql_raw({"data": {}}))
        gh = make_github(endpoint)

        asyncio.run(gh.update_pull_requests([PrUpdate(id="PR_1", is_draft=False)]))

        _, kwargs = endpoint.graphql.call_args
        assert kwargs["from_d0"]["pullRequestId"] == "PR_1"
        assert "to_d0" not in kwargs

    def test_mutation_timeout_resubmits_only_unapplied(self):
        """On a whole-request timeout, fields that applied (non-null data) are kept and
        only the unapplied (null) fields are resubmitted — no duplicate side effects."""
        endpoint = AsyncMock(spec=GitHubEndpoint)
        seen = []

        async def mock_graphql(query, **kwargs):
            prs = sorted(k for k in kwargs if k.startswith("pr"))
            seen.append(prs)
            if prs == ["pr0", "pr1"]:
                # pr0 applied before the timeout; pr1 didn't. Timeout names no field.
                return gql(
                    {"pr_out0": {"clientMutationId": "revup"}, "pr_out1": None},
                    errors=[{"message": "We couldn't respond to your request in time"}],
                )
            return gql({"pr_out1": {"clientMutationId": "revup"}})

        endpoint.graphql = mock_graphql
        gh = make_github(endpoint)

        asyncio.run(
            gh.update_pull_requests(
                [PrUpdate(id="PR_1", title="a"), PrUpdate(id="PR_2", title="b")]
            )
        )
        # pr0 already applied, so only pr1 is resubmitted.
        assert seen == [["pr0", "pr1"], ["pr1"]]

    def test_terminal_mutation_error_raises(self):
        endpoint = AsyncMock(spec=GitHubEndpoint)
        endpoint.graphql = AsyncMock(
            return_value=gql(
                {"pr_out0": None},
                errors=[{"type": "FORBIDDEN", "path": ["pr_out0"], "message": "no permission"}],
            )
        )
        gh = make_github(endpoint)

        with pytest.raises(RevupForgeException):
            asyncio.run(gh.update_pull_requests([PrUpdate(id="PR_1", title="x")]))

    def test_already_exists_mutation_is_not_retried(self):
        """UNPROCESSABLE 'already exists' is treated as done: no raise, no re-send."""
        endpoint = AsyncMock(spec=GitHubEndpoint)
        call_count = 0

        async def mock_graphql(query, **kwargs):
            nonlocal call_count
            call_count += 1
            return gql(
                {"com_out0": None},
                errors=[
                    {
                        "type": "UNPROCESSABLE",
                        "path": ["com_out0"],
                        "message": "A pull request already exists",
                    }
                ],
            )

        endpoint.graphql = mock_graphql
        gh = make_github(endpoint)

        asyncio.run(gh.update_pull_requests([PrUpdate(id="PR_1", comments=[PrComment("hi")])]))
        assert call_count == 1

    def test_resource_limit_salvages_and_retries_only_failed(self):
        """A partial mutation re-sends only the resource-limited field, not the applied one."""
        endpoint = AsyncMock(spec=GitHubEndpoint)
        seen = []

        async def mock_graphql(query, **kwargs):
            prs = sorted(k for k in kwargs if k.startswith("pr"))
            seen.append(prs)
            if prs == ["pr0", "pr1"]:
                return gql(
                    {"pr_out0": {"clientMutationId": "revup"}, "pr_out1": None},
                    errors=[
                        {
                            "type": "RESOURCE_LIMITS_EXCEEDED",
                            "path": ["pr_out1"],
                            "message": "too big",
                        }
                    ],
                )
            return gql({"pr_out1": {"clientMutationId": "revup"}})

        endpoint.graphql = mock_graphql
        gh = make_github(endpoint)

        asyncio.run(
            gh.update_pull_requests(
                [PrUpdate(id="PR_1", title="a"), PrUpdate(id="PR_2", title="b")]
            )
        )
        # First the full pair, then a retry of only the failed pr1 (pr0 not re-sent).
        assert seen == [["pr0", "pr1"], ["pr1"]]

    def test_resource_limit_with_no_progress_splits_in_half(self):
        """When nothing completes and the request is too big, it halves and each half
        (down to a single field) succeeds separately."""
        endpoint = AsyncMock(spec=GitHubEndpoint)
        seen = []

        async def mock_graphql(query, **kwargs):
            prs = sorted(k for k in kwargs if k.startswith("pr"))
            seen.append(prs)
            if len(prs) > 1:
                # Nothing completes; every field hits the resource limit.
                return gql(
                    {f"pr_out{k[2:]}": None for k in prs},
                    errors=[
                        {
                            "type": "RESOURCE_LIMITS_EXCEEDED",
                            "path": [f"pr_out{k[2:]}"],
                            "message": "big",
                        }
                        for k in prs
                    ],
                )
            return gql({f"pr_out{prs[0][2:]}": {"clientMutationId": "revup"}})

        endpoint.graphql = mock_graphql
        gh = make_github(endpoint)

        asyncio.run(
            gh.update_pull_requests(
                [PrUpdate(id="PR_1", title="a"), PrUpdate(id="PR_2", title="b")]
            )
        )
        # Full pair fails wholesale, then each field is sent alone and succeeds.
        assert seen[0] == ["pr0", "pr1"]
        assert sorted(seen[1:]) == [["pr0"], ["pr1"]]

    def test_repeated_timeout_gives_up_after_stall_limit(self):
        """A lone field that keeps timing out (no progress) is retried a bounded number
        of times, then surfaced as a terminal error rather than looping forever."""
        endpoint = AsyncMock(spec=GitHubEndpoint)
        call_count = 0

        async def mock_graphql(query, **kwargs):
            nonlocal call_count
            call_count += 1
            return gql(
                {"pr_out0": None},
                errors=[{"message": "We couldn't respond to your request in time"}],
            )

        endpoint.graphql = mock_graphql
        gh = make_github(endpoint)

        with pytest.raises(RevupForgeException):
            asyncio.run(gh.update_pull_requests([PrUpdate(id="PR_1", title="a")]))
        # Initial attempt + _MAX_STALLED_RETRIES resubmits, then it gives up.
        assert call_count == 1 + _MAX_STALLED_RETRIES

    def test_multiple_prs_combined_into_single_mutation(self):
        """Multiple PrUpdates with different fields all batch into one graphql call."""
        endpoint = AsyncMock(spec=GitHubEndpoint)
        endpoint.graphql = AsyncMock(return_value=gql_raw({"data": {}}))
        gh = make_github(endpoint)

        updates = [
            PrUpdate(id="PR_1", title="a", reviewer_ids={"U_1"}),
            PrUpdate(id="PR_2", body="b", assignee_ids={"U_2"}),
        ]
        asyncio.run(gh.update_pull_requests(updates))

        endpoint.graphql.assert_called_once()
        _, kwargs = endpoint.graphql.call_args
        assert kwargs["pr0"]["title"] == "a"
        assert kwargs["pr1"]["body"] == "b"
        assert kwargs["rev0"]["pullRequestId"] == "PR_1"
        assert kwargs["asn0"]["assignableId"] == "PR_2"


class TestMergeData:
    def test_merges_nested_repository_and_top_level(self):
        merged = {"repository": {"id": "R_1", "pr_out0": "x"}}
        _merge_data(merged, {"repository": {"pr_out1": "y"}, "team_out0": "z"})
        assert merged["repository"] == {"id": "R_1", "pr_out0": "x", "pr_out1": "y"}
        assert merged["team_out0"] == "z"

    def test_merges_top_level_keys(self):
        merged: dict = {}
        _merge_data(merged, {"team_out0": "a"})
        _merge_data(merged, {"team_out1": "b"})
        assert merged == {"team_out0": "a", "team_out1": "b"}

    def test_none_src_is_noop(self):
        merged = {"repository": {"id": "R_1"}}
        _merge_data(merged, None)
        assert merged == {"repository": {"id": "R_1"}}


def _add_field(q, prefix, values, scope="repo", field_template="{}: f(a: {}) {{id}},"):
    q.add(
        prefix=prefix,
        scope=scope,
        field_template=field_template,
        var_types=["String!"],
        values=list(values),
    )


class TestGraphqlQuerySplit:
    def test_split_preserves_alias_indices(self):
        """Aliases are baked in at add time, so merged results don't collide."""
        from revup.github.graphql import GraphqlQuery

        q = GraphqlQuery(name="Test")
        q.add_fixed_var("owner", "String!", "o")
        q.add_fixed_var("name", "String!", "r")
        q.fixed_repo_fields = "id\n"

        for i in range(4):
            _add_field(q, "pr", [f"val{i}"])

        left, right = q.split()
        left_query, left_vars = left.build()
        right_query, right_vars = right.build()

        # Left gets items 0,1 with aliases pr_out0, pr_out1
        assert "pr_out0" in left_query
        assert "pr_out1" in left_query
        assert left_vars["pr0"] == "val0"
        assert left_vars["pr1"] == "val1"

        # Right gets items 2,3 with aliases pr_out2, pr_out3 (index-preserved)
        assert "pr_out2" in right_query
        assert "pr_out3" in right_query
        assert right_vars["pr2"] == "val2"
        assert right_vars["pr3"] == "val3"

    def test_split_multi_var_query(self):
        from revup.github.graphql import GraphqlQuery

        q = GraphqlQuery(name="Test")
        tmpl = "{}: org(login: {}) {{team(slug: {})}},"
        teams = [("org1", "slug1"), ("org2", "slug2"), ("org3", "slug3"), ("org4", "slug4")]
        for org, slug in teams:
            q.add(
                prefix="team",
                scope="top",
                field_template=tmpl,
                var_types=["String!", "String!"],
                values=[org, slug],
            )

        left, right = q.split()
        _, left_vars = left.build()
        _, right_vars = right.build()

        assert left_vars == {
            "team0_0": "org1",
            "team0_1": "slug1",
            "team1_0": "org2",
            "team1_1": "slug2",
        }
        assert right_vars == {
            "team2_0": "org3",
            "team2_1": "slug3",
            "team3_0": "org4",
            "team3_1": "slug4",
        }

    def test_single_item_cannot_split_further(self):
        from revup.github.graphql import GraphqlQuery

        q = GraphqlQuery()
        _add_field(q, "x", ["only"])

        left, right = q.split()
        assert left.total_items() == 1
        assert right.total_items() == 0

    def test_split_is_heterogeneous(self):
        """Split halves the flat list regardless of query type, so a half may mix types."""
        from revup.github.graphql import GraphqlQuery

        q = GraphqlQuery()
        q.add_fixed_var("owner", "String!", "o")
        q.add_fixed_var("name", "String!", "r")

        # Interleave two types so a per-type split couldn't separate them, but a
        # flat split can. Order added: pr0, user0, pr1, user1, pr2, user2.
        for i in range(3):
            _add_field(q, "pr", [f"pr{i}"])
            _add_field(q, "user", [f"user{i}"], field_template="{}: u(q: {}) {{id}},")

        left, right = q.split()
        assert left.total_items() == 3
        assert right.total_items() == 3

        _, left_vars = left.build()
        _, right_vars = right.build()

        # Left half is the first 3 added (pr0, user0, pr1) — heterogeneous.
        assert left_vars["pr0"] == "pr0"
        assert left_vars["user0"] == "user0"
        assert left_vars["pr1"] == "pr1"
        # Right half is the last 3 (user1, pr2, user2) — also heterogeneous.
        assert right_vars["user1"] == "user1"
        assert right_vars["pr2"] == "pr2"
        assert right_vars["user2"] == "user2"


def make_endpoint() -> GitHubEndpoint:
    return GitHubEndpoint(oauth_token="tok", github_url="github.com")


def scripted_post(*responses):
    """An async _post replacement yielding (status, headers, body) tuples in order."""
    it = iter(responses)

    async def _post(query, kwargs):
        return next(it)

    return _post


class TestEndpointGraphqlPolicy:
    """endpoint.graphql retry/error policy, mocking the _post network seam."""

    def test_200_with_partial_data_and_errors_returns_response(self):
        ep = make_endpoint()
        body = {
            "data": {"repository": {"pr_out0": {"id": "PR_1"}}},
            "errors": [{"type": "RESOURCE_LIMITS_EXCEEDED", "path": ["pr_out1"], "message": "x"}],
        }
        ep._post = scripted_post((200, {}, body))

        resp = asyncio.run(ep.graphql("query"))
        assert isinstance(resp, GraphqlResponse)
        assert resp.data["repository"]["pr_out0"] == {"id": "PR_1"}
        assert resp.errors[0].type == "RESOURCE_LIMITS_EXCEEDED"

    def test_200_request_error_no_data_raises_forge(self):
        ep = make_endpoint()
        body = {"errors": [{"message": "Field 'x' doesn't exist"}]}
        ep._post = scripted_post((200, {}, body))

        with pytest.raises(RevupForgeException):
            asyncio.run(ep.graphql("query"))

    def test_200_non_json_body_raises_request(self):
        ep = make_endpoint()
        ep._post = scripted_post((200, {}, None))

        with pytest.raises(RevupRequestException):
            asyncio.run(ep.graphql("query"))

    def test_transient_status_then_success_retries(self):
        ep = make_endpoint()
        ep._post = scripted_post((502, {}, None), (200, {}, {"data": {"ok": 1}}))

        with patch("asyncio.sleep", new=AsyncMock()) as sleep:
            resp = asyncio.run(ep.graphql("query"))
        assert resp.data == {"ok": 1}
        sleep.assert_awaited_once()

    def test_secondary_limit_403_is_retried(self):
        ep = make_endpoint()
        ep._post = scripted_post((403, {}, None), (200, {}, {"data": {"ok": 1}}))

        with patch("asyncio.sleep", new=AsyncMock()):
            resp = asyncio.run(ep.graphql("query"))
        assert resp.data == {"ok": 1}

    def test_non_retryable_status_raises_immediately(self):
        ep = make_endpoint()
        ep._post = scripted_post((401, {}, {"message": "bad creds"}))

        with patch("asyncio.sleep", new=AsyncMock()) as sleep:
            with pytest.raises(RevupRequestException) as exc:
                asyncio.run(ep.graphql("query"))
        assert exc.value.status == 401
        sleep.assert_not_awaited()  # 401 is not retried

    def test_retries_exhausted_raises(self):
        ep = make_endpoint()
        ep._post = scripted_post((503, {}, None), (503, {}, None))

        with patch("asyncio.sleep", new=AsyncMock()):
            with pytest.raises(RevupRequestException) as exc:
                asyncio.run(ep.graphql("query", max_retries=2))
        assert exc.value.status == 503


class TestEndpointBackoffDelay:
    def test_retry_after_takes_precedence(self):
        # Even with a huge exponential attempt, an explicit Retry-After wins.
        assert _backoff_delay({"retry-after": "7"}, attempt=5, base_delay=1.0) == 7.0

    def test_exhausted_budget_waits_until_reset(self):
        reset = int(time.time()) + 30
        delay = _backoff_delay(
            {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)},
            attempt=0,
            base_delay=1.0,
        )
        assert 25 <= delay <= 31

    def test_falls_back_to_exponential(self):
        assert _backoff_delay({}, attempt=0, base_delay=1.0) == 1.0
        assert _backoff_delay({}, attempt=3, base_delay=1.0) == 8.0

    def test_malformed_retry_after_falls_through(self):
        # A non-numeric Retry-After is ignored, falling back to exponential.
        assert _backoff_delay({"retry-after": "soon"}, attempt=1, base_delay=1.0) == 2.0

    def test_remaining_zero_without_reset_falls_through(self):
        assert _backoff_delay({"x-ratelimit-remaining": "0"}, attempt=2, base_delay=1.0) == 4.0
