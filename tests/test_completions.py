import tempfile
from pathlib import Path

from revup.completion import SOURCE_LINE_TEMPLATES, ShellType, install_completion


class TestInstallCompletion:
    def test_installs_source_line_in_rc_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc_path = Path(tmp) / "test_bashrc"
            ret = install_completion(ShellType.BASH, rc_file=str(rc_path))
            assert ret == 0
            assert SOURCE_LINE_TEMPLATES[ShellType.BASH] in rc_path.read_text()

    def test_idempotent_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc_path = Path(tmp) / "test_bashrc"
            install_completion(ShellType.BASH, rc_file=str(rc_path))
            install_completion(ShellType.BASH, rc_file=str(rc_path))
            assert rc_path.read_text().count(SOURCE_LINE_TEMPLATES[ShellType.BASH]) == 1

    def test_appends_to_existing_rc_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc_path = Path(tmp) / "existing_bashrc"
            rc_path.write_text("export FOO=bar\n")
            install_completion(ShellType.BASH, rc_file=str(rc_path))
            contents = rc_path.read_text()
            assert "export FOO=bar" in contents
            assert SOURCE_LINE_TEMPLATES[ShellType.BASH] in contents

    def test_creates_parent_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc_path = Path(tmp) / "subdir" / "nested" / "config.fish"
            ret = install_completion(ShellType.FISH, rc_file=str(rc_path))
            assert ret == 0
            assert rc_path.is_file()

    def test_all_shells_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            for s in ShellType:
                rc_path = Path(tmp) / f"rc_{s.value}"
                ret = install_completion(s, rc_file=str(rc_path))
                assert ret == 0
                assert SOURCE_LINE_TEMPLATES[s] in rc_path.read_text()
