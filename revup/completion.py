import argparse
import asyncio
import enum
import logging
import os
from pathlib import Path
from typing import List, Optional

from revup.config import Config


class ShellType(enum.Enum):
    BASH = "bash"
    ZSH = "zsh"
    FISH = "fish"
    TCSH = "tcsh"

    def __str__(self) -> str:
        return self.value

    def __repr__(self) -> str:
        return self.value

    @property
    def default_rc_file(self) -> str:
        return DEFAULT_RC_FILES[self]


DEFAULT_RC_FILES = {
    ShellType.BASH: "~/.bashrc",
    ShellType.ZSH: "~/.zshrc",
    ShellType.FISH: "~/.config/fish/config.fish",
    ShellType.TCSH: "~/.tcshrc",
}

SOURCE_LINE_TEMPLATES = {
    ShellType.BASH: 'eval "$(register-python-argcomplete revup)"',
    ShellType.ZSH: 'eval "$(register-python-argcomplete revup)"',
    ShellType.FISH: "register-python-argcomplete --shell fish revup | source",
    ShellType.TCSH: "eval `register-python-argcomplete --shell tcsh revup`",
}


def topic_completer(
    prefix: str, parsed_args: Optional[argparse.Namespace] = None, **_kwargs: object
) -> List[str]:
    try:
        from revup import git, shell, toolkit

        async def _get_names() -> List[str]:
            sh = shell.Shell(quiet=True)
            git_ctx = await git.make_git(sh, "", "", "origin", "main", "", False, "")
            topics = await toolkit.get_topics(git_ctx)
            return [t.name for t in topics.topics.values()]

        already = set()
        if parsed_args is not None:
            for attr in ("topics", "ref_or_topic"):
                val = getattr(parsed_args, attr, None)
                if isinstance(val, list):
                    already.update(val)
                elif isinstance(val, str):
                    already.add(val)

        return [n for n in asyncio.run(_get_names()) if n.startswith(prefix) and n not in already]
    except (OSError, RuntimeError):
        return []


def prompt_config_key(shell: ShellType) -> str:
    return f"prompt_completion_{shell.value}"


def install_completion(
    shell: ShellType, rc_file: Optional[str] = None, conf: Optional[Config] = None
) -> int:
    rc_path = Path(rc_file or shell.default_rc_file).expanduser()
    source_line = SOURCE_LINE_TEMPLATES[shell]

    if rc_path.is_file() and source_line in rc_path.read_text():
        logging.info(f"Completion already installed in {rc_path}")
    else:
        rc_path.parent.mkdir(parents=True, exist_ok=True)
        with rc_path.open("a") as f:
            f.write(f"\n{source_line}\n")
        logging.info(f"Installed revup completions in {rc_path}")

    if conf:
        user_conf = Config(conf.config_path)
        user_conf.read()
        user_conf.set_value("revup", prompt_config_key(shell), "false")
        user_conf.write()

    return 0


def detect_default_shell() -> Optional[ShellType]:
    shell_env = os.environ.get("SHELL", "")
    basename = Path(shell_env).name
    for s in ShellType:
        if s.value == basename:
            return s
    return None


def maybe_prompt_user_for_completions(conf: Config) -> None:
    shell = detect_default_shell()
    if shell is None:
        return

    key = prompt_config_key(shell)
    config = conf.get_config()
    if config.has_option("revup", key) and config.get("revup", key).lower() != "true":
        return

    logging.info(
        f"Tip: enable tab completions for {shell.value} by running:\n"
        f"  revup install-completion --shell {shell.value}\n"
        f"To suppress this message:\n"
        f"  revup config prompt-completion-{shell.value} false"
    )
