import argparse

import pytest
from git_env import GitTestEnvironment, async_test

from revup import cherry_pick, download
from revup.core_types import RevupUsageException


def make_args(target, base_branch="main"):
    return argparse.Namespace(
        pr_or_branch=[target],
        base_branch=base_branch,
        remote_name="origin",
        forge_url="github.com",
    )


async def setup_repo(env):
    """A main with a feature branch holding two topic commits, and HEAD on a separate branch."""
    await env.commit("root", {"root.txt": "r"})
    await env.git_ctx.git("branch", "origin/main", "HEAD")
    await env.git_ctx.git("checkout", "-b", "feature")
    await env.commit("feat\n\nbody text\n\nTopic: theirs\nRelative: other", {"f.txt": "1"})
    await env.commit("feat 2", {"f.txt": "2", "g.txt": "g"})
    await env.git_ctx.git("checkout", "-b", "work", "main")
    await env.commit("mine\n\nTopic: mine", {"m.txt": "m"})


class TestRewriteDownloadMsg:
    def test_strips_tags_and_adds_metadata(self):
        msg = download.rewrite_download_msg("title\n\nbody\n\nTopic: t\nReviewers: bob", "br")
        assert msg == (
            f"title\n\nbody\n\n{download.DOWNLOAD_WARNING}\n\n{download.DOWNLOAD_TAG}: br\n"
        )

    def test_drops_old_download_line_and_collapses_blank_lines(self):
        msg = download.rewrite_download_msg("t\n\nbody\nTopic: x\n\nmore\nRevup-Download: old", "n")
        assert msg == f"t\n\nbody\n\nmore\n\n{download.DOWNLOAD_WARNING}\n\nRevup-Download: n\n"


class TestDownload:
    @async_test
    async def test_branch_is_squashed_with_metadata(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)

            assert await download.main(make_args("feature"), env.git_ctx, None) == 0

            msg = await env.get_commit_message()
            assert msg.startswith("feat\n\nbody text\n\n")
            assert "Topic:" not in msg and "Relative:" not in msg
            assert msg.rstrip().endswith("Revup-Download: feature")
            assert await env.get_file_at_commit("f.txt") == "2"
            assert await env.get_file_at_commit("m.txt") == "m"
            # One squashed commit on top of "mine"
            assert await env.get_log_subjects() == ["feat", "mine", "root"]

    @async_test
    async def test_already_downloaded_is_refused(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await download.main(make_args("feature"), env.git_ctx, None)
            head = await env.get_commit_hash()

            with pytest.raises(RevupUsageException, match="already downloaded"):
                await download.main(make_args("feature"), env.git_ctx, None)
            assert await env.get_commit_hash() == head

    @async_test
    async def test_pr_number_resolves_through_remote(self, monkeypatch):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.git_ctx.git("remote", "add", "origin", "git@github.com:acme/widgets.git")
            seen = []

            async def fake_resolve(args, git_ctx, conf, url):
                seen.append(url)
                return "feature", "main"

            monkeypatch.setattr(download, "resolve_pr_url", fake_resolve)

            assert await download.main(make_args("42", None), env.git_ctx, None) == 0
            assert seen == ["https://github.com/acme/widgets/pull/42"]
            assert "Revup-Download: feature" in await env.get_commit_message()

    @async_test
    async def test_pr_number_without_remote_fails(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            with pytest.raises(RevupUsageException, match="full URL"):
                await download.main(make_args("42"), env.git_ctx, None)

    @async_test
    async def test_origin_prefixed_branch_counts_as_already_downloaded(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await download.main(make_args("feature"), env.git_ctx, None)
            head = await env.get_commit_hash()

            with pytest.raises(RevupUsageException, match="already downloaded"):
                await download.main(make_args("origin/feature"), env.git_ctx, None)
            assert await env.get_commit_hash() == head
            assert not await env.has_staged_changes()

    @async_test
    async def test_origin_prefix_is_not_kept_in_tag(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await download.main(make_args("origin/feature"), env.git_ctx, None)
            assert (await env.get_commit_message()).rstrip().endswith("Revup-Download: feature")

    @async_test
    async def test_pr_number_with_https_remote(self, monkeypatch):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.git_ctx.git("remote", "add", "origin", "https://github.com/acme/widgets.git")
            seen = []

            async def fake_resolve(args, git_ctx, conf, url):
                seen.append(url)
                return "feature", "main"

            monkeypatch.setattr(download, "resolve_pr_url", fake_resolve)

            await download.main(make_args("7", None), env.git_ctx, None)
            assert seen == ["https://github.com/acme/widgets/pull/7"]

    @async_test
    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/other/widgets/pull/1",
            "https://github.com/acme/gadgets/pull/1",
            "https://ghe.example.com/acme/widgets/pull/1",
        ],
    )
    async def test_pr_from_other_repo_is_refused(self, monkeypatch, url):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.git_ctx.git("remote", "add", "origin", "git@github.com:acme/widgets.git")
            head = await env.get_commit_hash()

            async def fail_resolve(*args):
                raise AssertionError("must not query the forge")

            monkeypatch.setattr(download, "resolve_pr_url", fail_resolve)

            with pytest.raises(RevupUsageException, match="isn't supported"):
                await download.main(make_args(url), env.git_ctx, None)
            assert await env.get_commit_hash() == head

    @async_test
    async def test_same_repo_pr_url_is_accepted(self, monkeypatch):
        async with GitTestEnvironment() as env:
            await setup_repo(env)
            await env.git_ctx.git("remote", "add", "origin", "git@github.com:Acme/Widgets.git")

            async def fake_resolve(args, git_ctx, conf, url):
                return "feature", "main"

            monkeypatch.setattr(download, "resolve_pr_url", fake_resolve)

            url = "https://github.com/acme/widgets/pull/3"
            assert await download.main(make_args(url, None), env.git_ctx, None) == 0

    @async_test
    async def test_no_base_branch_is_a_usage_error(self):
        async with GitTestEnvironment() as env:
            await env.commit("root", {"root.txt": "r"})
            await env.git_ctx.git("checkout", "-b", "feature")
            await env.commit("feat", {"f.txt": "1"})
            await env.git_ctx.git("checkout", "-b", "work", "main")
            with pytest.raises(RevupUsageException):
                await download.main(make_args("feature"), env.git_ctx, None)


class TestCherryPickUnchanged:
    @async_test
    async def test_cherry_pick_keeps_message_without_rewrite(self):
        async with GitTestEnvironment() as env:
            await setup_repo(env)

            assert await cherry_pick.squash_and_cherry_pick(env.git_ctx, "feature", "main") == 0

            msg = await env.get_commit_message()
            assert "Topic: theirs" in msg and "Revup-Download" not in msg
            assert await env.get_file_at_commit("f.txt") == "2"
