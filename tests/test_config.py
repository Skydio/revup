from pathlib import Path

from revup.config import Config
from revup.revup import get_ancestor_config_paths


def write_config(path: Path, main_branch: str) -> None:
    path.write_text(f"[revup]\nmain_branch = {main_branch}\n")


class TestAncestorConfigPaths:
    def test_finds_config_above_repo_root(self, tmp_path: Path):
        tree = tmp_path / "src"
        repo = tree / "project" / "sub"
        repo.mkdir(parents=True)
        shared = tree / ".revupconfig"
        write_config(shared, "shared-branch")

        paths = get_ancestor_config_paths(str(repo), str(tmp_path / ".revupconfig"))

        assert paths == [str(shared)]

    def test_nearest_first_ordering(self, tmp_path: Path):
        tree = tmp_path / "src"
        repo = tree / "project"
        repo.mkdir(parents=True)
        near = tree / ".revupconfig"
        far = tmp_path / ".revupconfig"
        write_config(near, "near")
        write_config(far, "far")

        # Pass a user config path that doesn't collide with the ancestors.
        paths = get_ancestor_config_paths(str(repo), str(tmp_path / "home" / ".revupconfig"))

        assert paths == [str(near), str(far)]

    def test_excludes_user_config(self, tmp_path: Path):
        repo = tmp_path / "project"
        repo.mkdir(parents=True)
        user_config = tmp_path / ".revupconfig"
        write_config(user_config, "user")

        paths = get_ancestor_config_paths(str(repo), str(user_config))

        assert paths == []


class TestConfigPrecedence:
    def test_repo_root_overrides_ancestor(self, tmp_path: Path):
        tree = tmp_path / "src"
        repo = tree / "project"
        repo.mkdir(parents=True)
        ancestor = tree / ".revupconfig"
        repo_config = repo / ".revupconfig"
        write_config(ancestor, "ancestor-branch")
        write_config(repo_config, "repo-branch")

        conf = Config("", str(repo_config), [str(ancestor)])
        conf.read()

        assert conf.get_config().get("revup", "main_branch") == "repo-branch"

    def test_ancestor_applies_when_repo_has_no_config(self, tmp_path: Path):
        tree = tmp_path / "src"
        repo = tree / "project"
        repo.mkdir(parents=True)
        ancestor = tree / ".revupconfig"
        write_config(ancestor, "ancestor-branch")

        conf = Config("", str(repo / ".revupconfig"), [str(ancestor)])
        conf.read()

        assert conf.get_config().get("revup", "main_branch") == "ancestor-branch"

    def test_user_config_overrides_repo_and_ancestor(self, tmp_path: Path):
        tree = tmp_path / "src"
        repo = tree / "project"
        repo.mkdir(parents=True)
        ancestor = tree / ".revupconfig"
        repo_config = repo / ".revupconfig"
        user_config = tmp_path / "home.revupconfig"
        write_config(ancestor, "ancestor-branch")
        write_config(repo_config, "repo-branch")
        write_config(user_config, "user-branch")

        conf = Config(str(user_config), str(repo_config), [str(ancestor)])
        conf.read()

        assert conf.get_config().get("revup", "main_branch") == "user-branch"
