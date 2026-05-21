"""Unit tests for the Github class with a mocked GraphQL endpoint."""

import asyncio
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pytest

from revup.forge import ForgeRepoInfo, PrComment, PrInfo, PrUpdate
from revup.github.endpoint import GitHubEndpoint
from revup.github.github import Github, _merge_results
from revup.types import RevupForgeException


def run(coro):
    return asyncio.run(coro)


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
            return_value={
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
                                {"author": {"login": "bob", "id": "U_2"}, "viewerDidAuthor": False},
                                {"author": {"login": "me", "id": "U_3"}, "viewerDidAuthor": True},
                            ],
                        ),
                    },
                }
            }
        )
        gh = make_github(endpoint)

        _, prs, _, _, _, _, _ = run(
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
            return_value={
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
        gh = make_github(endpoint)

        _, prs, _, _, _, _, _ = run(
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
            return_value={
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
        gh = make_github(endpoint)

        _, prs, _, _, _, _, _ = run(
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
            return_value={
                "data": {
                    "repository": {
                        "id": "R_1",
                        "pr_out0": make_pr_node(is_draft=True, state="OPEN"),
                    },
                }
            }
        )
        gh = make_github(endpoint)

        _, prs, _, _, _, _, _ = run(
            gh.query_everything(head_refs=["feat"], user_ids=[], labels=[], teams=[])
        )

        assert prs[0].is_draft is True
        assert prs[0].state == "OPEN"

    def test_user_prefix_match_picks_shortest(self):
        """When multiple users match, picks the shortest login that starts with the query."""
        endpoint = AsyncMock(spec=GitHubEndpoint)
        endpoint.graphql = AsyncMock(
            return_value={
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
        gh = make_github(endpoint)

        _, _, names_to_ids, names_to_logins, _, _, _ = run(
            gh.query_everything(head_refs=[], user_ids=["alice"], labels=[], teams=[])
        )

        assert names_to_ids["alice"] == "U_exact"
        assert names_to_logins["alice"] == "alice"

    def test_user_no_prefix_match_falls_back_to_first(self):
        """If no login starts with the query, falls back to first result."""
        endpoint = AsyncMock(spec=GitHubEndpoint)
        endpoint.graphql = AsyncMock(
            return_value={
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
        gh = make_github(endpoint)

        _, _, names_to_ids, names_to_logins, _, _, _ = run(
            gh.query_everything(head_refs=[], user_ids=["al"], labels=[], teams=[])
        )

        # Falls back to first node's id, but login NOT added to names_to_logins
        assert names_to_ids["al"] == "U_bob"
        assert "al" not in names_to_logins

    def test_team_with_too_many_members_returns_none(self):
        """When totalCount > returned nodes, members set is None (can't enumerate all)."""
        endpoint = AsyncMock(spec=GitHubEndpoint)
        endpoint.graphql = AsyncMock(
            return_value={
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
        gh = make_github(endpoint)

        _, _, _, _, _, teams_to_ids, teams_to_members = run(
            gh.query_everything(head_refs=[], user_ids=[], labels=[], teams=[("org", "big")])
        )

        assert teams_to_ids == {"org/big": "T_1"}
        assert teams_to_members["org/big"] is None

    def test_resource_limit_triggers_split(self):
        endpoint = AsyncMock(spec=GitHubEndpoint)

        call_count = 0

        async def mock_graphql(query, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RevupForgeException(
                    [{"type": "RESOURCE_LIMITS_EXCEEDED", "message": "too complex"}]
                )
            repo_data: Dict[str, Any] = {"id": "R_1"}
            for k, v in kwargs.items():
                if k.startswith("pr"):
                    alias = f"pr_out{k[2:]}"
                    repo_data[alias] = make_pr_node(f"PR_{v}")
            return {"data": {"repository": repo_data}}

        endpoint.graphql = mock_graphql
        gh = make_github(endpoint)

        repo_id, prs, _, _, _, _, _ = run(
            gh.query_everything(head_refs=["b1", "b2"], user_ids=[], labels=[], teams=[])
        )

        assert call_count == 3
        assert repo_id == "R_1"
        assert len(prs) == 2
        assert prs[0].id == "PR_b1"
        assert prs[1].id == "PR_b2"

    def test_non_resource_error_raises(self):
        endpoint = AsyncMock(spec=GitHubEndpoint)
        endpoint.graphql = AsyncMock(
            side_effect=RevupForgeException([{"type": "NOT_FOUND", "message": "not found"}])
        )
        gh = make_github(endpoint)

        with pytest.raises(RevupForgeException):
            run(gh.query_everything(head_refs=["b1"], user_ids=[], labels=[], teams=[]))

    def test_empty_pr_result(self):
        endpoint = AsyncMock(spec=GitHubEndpoint)
        endpoint.graphql = AsyncMock(
            return_value={
                "data": {
                    "repository": {
                        "id": "R_1",
                        "pr_out0": {"nodes": [], "totalCount": 0},
                    }
                }
            }
        )
        gh = make_github(endpoint)

        _, prs, _, _, _, _, _ = run(
            gh.query_everything(head_refs=["no-pr"], user_ids=[], labels=[], teams=[])
        )

        assert prs == [None]

    def test_multiple_prs_each_mapped_to_correct_branch(self):
        """Each head_ref maps to its corresponding PR result by index."""
        endpoint = AsyncMock(spec=GitHubEndpoint)
        endpoint.graphql = AsyncMock(
            return_value={
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
        gh = make_github(endpoint)

        _, prs, _, _, _, _, _ = run(
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
            return_value={
                "data": {
                    "pr_out0": {"pullRequest": {"id": "PR_1", "url": "url1"}},
                }
            }
        )
        gh = make_github(endpoint, fork_owner="myfork")

        pr = PrInfo(
            baseRef="main", headRef="feat1", baseRefOid=None, headRefOid=None, body="b", title="t"
        )
        run(gh.create_pull_requests("R_1", [pr]))

        _, kwargs = endpoint.graphql.call_args
        assert kwargs["pr0"]["headRefName"] == "myfork:feat1"
        assert kwargs["pr0"]["baseRefName"] == "main"

    def test_same_owner_no_prefix(self):
        endpoint = AsyncMock(spec=GitHubEndpoint)
        endpoint.graphql = AsyncMock(
            return_value={"data": {"pr_out0": {"pullRequest": {"id": "PR_1", "url": "url1"}}}}
        )
        gh = make_github(endpoint, fork_owner="owner")

        pr = PrInfo(
            baseRef="main", headRef="feat1", baseRefOid=None, headRefOid=None, body="b", title="t"
        )
        run(gh.create_pull_requests("R_1", [pr]))

        _, kwargs = endpoint.graphql.call_args
        assert kwargs["pr0"]["headRefName"] == "feat1"

    def test_draft_flag_passed_through(self):
        endpoint = AsyncMock(spec=GitHubEndpoint)
        endpoint.graphql = AsyncMock(
            return_value={"data": {"pr_out0": {"pullRequest": {"id": "PR_1", "url": "u"}}}}
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
        run(gh.create_pull_requests("R_1", [pr]))

        _, kwargs = endpoint.graphql.call_args
        assert kwargs["pr0"]["draft"] is True

    def test_null_pullRequest_leaves_pr_unchanged(self):
        """If GitHub returns null for the pullRequest, id/url stay at defaults."""
        endpoint = AsyncMock(spec=GitHubEndpoint)
        endpoint.graphql = AsyncMock(return_value={"data": {"pr_out0": {"pullRequest": None}}})
        gh = make_github(endpoint)

        pr = PrInfo(
            baseRef="main", headRef="feat1", baseRefOid=None, headRefOid=None, body="b", title="t"
        )
        run(gh.create_pull_requests("R_1", [pr]))

        assert pr.id == ""
        assert pr.url == ""

    def test_multiple_prs_batched_in_one_call(self):
        endpoint = AsyncMock(spec=GitHubEndpoint)
        endpoint.graphql = AsyncMock(
            return_value={
                "data": {
                    "pr_out0": {"pullRequest": {"id": "PR_1", "url": "u1"}},
                    "pr_out1": {"pullRequest": {"id": "PR_2", "url": "u2"}},
                    "pr_out2": {"pullRequest": {"id": "PR_3", "url": "u3"}},
                }
            }
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
        run(gh.create_pull_requests("R_1", prs))

        endpoint.graphql.assert_called_once()
        assert prs[0].id == "PR_1"
        assert prs[1].id == "PR_2"
        assert prs[2].id == "PR_3"


class TestUpdatePullRequests:
    def test_builds_all_mutation_types(self):
        """Labels, reviewers, assignees, draft conversion, comments all go in one mutation."""
        endpoint = AsyncMock(spec=GitHubEndpoint)
        endpoint.graphql = AsyncMock(return_value={"data": {}})
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

        run(gh.update_pull_requests(updates))

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
        endpoint.graphql = AsyncMock(return_value={"data": {}})
        gh = make_github(endpoint)

        run(gh.update_pull_requests([PrUpdate(id="PR_1", is_draft=False)]))

        _, kwargs = endpoint.graphql.call_args
        assert kwargs["from_d0"]["pullRequestId"] == "PR_1"
        assert "to_d0" not in kwargs

    def test_timeout_does_not_raise(self):
        endpoint = AsyncMock(spec=GitHubEndpoint)
        endpoint.graphql = AsyncMock(
            side_effect=RevupForgeException([{"type": "TIMEOUT", "message": "timeout occurred"}])
        )
        gh = make_github(endpoint)

        run(gh.update_pull_requests([PrUpdate(id="PR_1", title="x")]))

    def test_non_timeout_error_raises(self):
        endpoint = AsyncMock(spec=GitHubEndpoint)
        endpoint.graphql = AsyncMock(
            side_effect=RevupForgeException([{"type": "INTERNAL", "message": "server error"}])
        )
        gh = make_github(endpoint)

        with pytest.raises(RevupForgeException):
            run(gh.update_pull_requests([PrUpdate(id="PR_1", title="x")]))

    def test_resource_limit_splits(self):
        endpoint = AsyncMock(spec=GitHubEndpoint)
        call_count = 0

        async def mock_graphql(query, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RevupForgeException(
                    [{"type": "RESOURCE_LIMITS_EXCEEDED", "message": "too big"}]
                )
            return {"data": {}}

        endpoint.graphql = mock_graphql
        gh = make_github(endpoint)

        updates = [
            PrUpdate(id="PR_1", title="a"),
            PrUpdate(id="PR_2", title="b"),
        ]
        run(gh.update_pull_requests(updates))

        assert call_count == 3

    def test_multiple_prs_combined_into_single_mutation(self):
        """Multiple PrUpdates with different fields all batch into one graphql call."""
        endpoint = AsyncMock(spec=GitHubEndpoint)
        endpoint.graphql = AsyncMock(return_value={"data": {}})
        gh = make_github(endpoint)

        updates = [
            PrUpdate(id="PR_1", title="a", reviewer_ids={"U_1"}),
            PrUpdate(id="PR_2", body="b", assignee_ids={"U_2"}),
        ]
        run(gh.update_pull_requests(updates))

        endpoint.graphql.assert_called_once()
        _, kwargs = endpoint.graphql.call_args
        assert kwargs["pr0"]["title"] == "a"
        assert kwargs["pr1"]["body"] == "b"
        assert kwargs["rev0"]["pullRequestId"] == "PR_1"
        assert kwargs["asn0"]["assignableId"] == "PR_2"


class TestMergeResults:
    def test_merges_repository_and_top_level(self):
        left = {"data": {"repository": {"id": "R_1", "pr_out0": "x"}}}
        right = {"data": {"repository": {"pr_out1": "y"}, "team_out0": "z"}}
        merged = _merge_results(left, right)
        assert merged["data"]["repository"] == {"id": "R_1", "pr_out0": "x", "pr_out1": "y"}
        assert merged["data"]["team_out0"] == "z"

    def test_right_top_level_keys_preserved(self):
        left = {"data": {"team_out0": "a"}}
        right = {"data": {"team_out1": "b"}}
        merged = _merge_results(left, right)
        assert merged["data"]["team_out0"] == "a"
        assert merged["data"]["team_out1"] == "b"


class TestGraphqlQuerySplit:
    def test_split_preserves_all_items_via_offset(self):
        """After split, aliases keep original indices so merged results don't collide."""
        from revup.github.graphql import GraphqlQuery, QueryGroup

        q = GraphqlQuery(name="Test")
        q.add_fixed_var("owner", "String!", "o")
        q.add_fixed_var("name", "String!", "r")
        q.fixed_repo_fields = "id\n"

        group = QueryGroup(
            prefix="pr",
            scope="repo",
            field_template="{}: field(arg: {}) {{id}},",
            var_types=["String!"],
        )
        for i in range(4):
            group.add(f"val{i}")
        q.add_group(group)

        left, right = q.split()
        left_query, left_vars = left.build()
        right_query, right_vars = right.build()

        # Left gets items 0,1 with aliases pr_out0, pr_out1
        assert "pr_out0" in left_query
        assert "pr_out1" in left_query
        assert left_vars["pr0"] == "val0"
        assert left_vars["pr1"] == "val1"

        # Right gets items 2,3 with aliases pr_out2, pr_out3 (offset-preserved)
        assert "pr_out2" in right_query
        assert "pr_out3" in right_query
        assert right_vars["pr2"] == "val2"
        assert right_vars["pr3"] == "val3"

    def test_split_multi_var_group(self):
        from revup.github.graphql import GraphqlQuery, QueryGroup

        q = GraphqlQuery(name="Test")

        group = QueryGroup(
            prefix="team",
            scope="top",
            field_template="{}: org(login: {}) {{team(slug: {})}},",
            var_types=["String!", "String!"],
        )
        group.add("org1", "slug1")
        group.add("org2", "slug2")
        group.add("org3", "slug3")
        group.add("org4", "slug4")
        q.add_group(group)

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
        from revup.github.graphql import GraphqlQuery, QueryGroup

        q = GraphqlQuery()
        group = QueryGroup(
            prefix="x",
            scope="repo",
            field_template="{}: f(a: {}) {{id}},",
            var_types=["String!"],
        )
        group.add("only")
        q.add_group(group)

        left, right = q.split()
        assert left.total_items() == 1
        assert right.total_items() == 0

    def test_split_multiple_groups(self):
        """Each group is independently halved during split."""
        from revup.github.graphql import GraphqlQuery, QueryGroup

        q = GraphqlQuery()
        q.add_fixed_var("owner", "String!", "o")
        q.add_fixed_var("name", "String!", "r")

        g1 = QueryGroup(
            prefix="pr", scope="repo", field_template="{}: f(a: {}) {{id}},", var_types=["String!"]
        )
        g1.add("a")
        g1.add("b")
        g1.add("c")
        g1.add("d")
        q.add_group(g1)

        g2 = QueryGroup(
            prefix="user",
            scope="repo",
            field_template="{}: u(q: {}) {{id}},",
            var_types=["String!"],
        )
        g2.add("x")
        g2.add("y")
        q.add_group(g2)

        left, right = q.split()

        assert left.total_items() == 3  # 2 from g1 + 1 from g2
        assert right.total_items() == 3  # 2 from g1 + 1 from g2

        _, left_vars = left.build()
        _, right_vars = right.build()

        assert left_vars["pr0"] == "a"
        assert left_vars["pr1"] == "b"
        assert left_vars["user0"] == "x"
        assert right_vars["pr2"] == "c"
        assert right_vars["pr3"] == "d"
        assert right_vars["user1"] == "y"
