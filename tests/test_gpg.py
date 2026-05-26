from git_env import GitTestEnvironment, async_test

from revup import git


async def _set_gpg_config(env, value):
    await env.sh.sh(env._git_path, "config", "commit.gpgSign", value)


async def _make_git_in_env(env, gpg_sign=None):
    return await git.make_git(
        env.sh,
        git_path=env._git_path,
        remote_name="origin",
        main_branch="main",
        editor="true",
        gpg_sign=gpg_sign,
    )


class TestMakeGitGpgSign:
    @async_test
    async def test_reads_from_git_config_true(self):
        async with GitTestEnvironment() as env:
            await _set_gpg_config(env, "true")
            git_ctx = await _make_git_in_env(env)
            assert git_ctx.gpg_sign is True

    @async_test
    async def test_reads_from_git_config_false(self):
        async with GitTestEnvironment() as env:
            await _set_gpg_config(env, "false")
            git_ctx = await _make_git_in_env(env)
            assert git_ctx.gpg_sign is False

    @async_test
    async def test_cli_override_true_when_config_unset(self):
        async with GitTestEnvironment() as env:
            git_ctx = await _make_git_in_env(env, gpg_sign=True)
            assert git_ctx.gpg_sign is True

    @async_test
    async def test_cli_override_false_wins_over_config(self):
        async with GitTestEnvironment() as env:
            await _set_gpg_config(env, "true")
            git_ctx = await _make_git_in_env(env, gpg_sign=False)
            assert git_ctx.gpg_sign is False


class TestCommitTreeSigning:
    async def _get_real_commit_header(self, env):
        commit_hash = await env.commit("base", {"a.txt": "hello"})
        raw = await env.git_ctx.rev_list(commit_hash, max_revs=1, header=True)
        return git.parse_rev_list(raw)[0]

    async def _capture_commit_tree_args(self, env, gpg_sign, mocker):
        commit_info = await self._get_real_commit_header(env)
        env.git_ctx.gpg_sign = gpg_sign

        async def fake_git_stdout(*args, **kwargs):
            return "fakecommithash"

        spy = mocker.patch.object(env.git_ctx, "git_stdout", side_effect=fake_git_stdout)

        result = await env.git_ctx.commit_tree(commit_info)
        assert result == "fakecommithash"
        return spy.call_args.args

    @async_test
    async def test_dash_s_present_when_gpg_sign_enabled(self, mocker):
        async with GitTestEnvironment() as env:
            args = await self._capture_commit_tree_args(env, True, mocker)
            assert "commit-tree" in args
            assert "-S" in args

    @async_test
    async def test_dash_s_absent_when_gpg_sign_disabled(self, mocker):
        async with GitTestEnvironment() as env:
            args = await self._capture_commit_tree_args(env, False, mocker)
            assert "commit-tree" in args
            assert "-S" not in args
