import argparse
import configparser
import getpass
import logging
import os
import re
from argparse import _StoreAction, _StoreFalseAction, _StoreTrueAction
from typing import Any, Dict, List, Optional

from revup.types import RevupUsageException


class RevupArgParser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

    def add_argument(self, *args: Any, **kwargs: Any) -> argparse.Action:
        """
        For each boolean store_true action, add a corresponding "no" store_false action
        with the same target.
        """
        action = super().add_argument(*args, **kwargs)

        if isinstance(action, _StoreTrueAction):
            no_options = []
            for option_string in action.option_strings:
                if option_string.startswith("--"):
                    no_options.append("--no-" + option_string[2:])
                elif option_string.startswith("-"):
                    no_options.append("-n" + option_string[1:])

            if no_options:
                action = _StoreFalseAction(
                    no_options, action.dest, action.default, False, "autogenerated negation"
                )
                for op in no_options:
                    self._option_string_actions[op] = action
                self._actions.append(action)
        elif isinstance(action, _StoreFalseAction):
            # We assume all store false actions are the autogenerated one
            raise RuntimeError("Direct store_false actions are not allowed")

        return action

    def set_defaults_from_config(self, conf: configparser.ConfigParser) -> None:
        cmd = self.get_command()
        for option, action in self.get_actions().items():
            if conf.has_option(cmd, option):
                self.set_option_default(option, action, conf.get(cmd, option))

    def get_command(self) -> str:
        return self.prog.split()[-1].replace("-", "_")

    def get_actions(self) -> Dict[str, argparse.Action]:
        ret: Dict[str, argparse.Action] = {}
        for action in self._actions:
            if not isinstance(action, (_StoreTrueAction, _StoreAction)):
                # Ignore nonconfigurable actions (help, auto-generated negation)
                continue

            if len(action.option_strings) > 0 and action.option_strings[0].startswith("--"):
                option = action.option_strings[0][2:].replace("-", "_")
                ret[option] = action
        return ret

    def set_option_default(self, option: str, action: argparse.Action, value: str) -> None:
        if isinstance(action, _StoreTrueAction):
            override = value.lower()
            if override in ("true", "false"):
                action.default = override == "true"
            else:
                raise ValueError(
                    f'"{override}" not a valid override for boolean flag {option}, must'
                    ' be "true" or "false"'
                )
        elif isinstance(action, _StoreAction):
            if action.choices and value not in action.choices:
                raise ValueError(
                    f"Value {value} for {self.get_command()}.{option} is not"
                    f" one of the choices {action.choices}"
                )

            action.default = value
        else:
            raise RuntimeError("Unknown option type!: ", option, action)


class Config:
    # Object containing configuration values. Populated by read(), and can then
    # be modified by set_value()
    config: configparser.ConfigParser

    # Path to user global config file
    config_path: str

    # Path to config file in current repo
    repo_config_path: str

    # Whether the config contains values that need to be flushed to the file
    dirty: bool = False

    def __init__(self, config_path: str, repo_config_path: str = ""):
        self.config = configparser.ConfigParser()
        self.config_path = config_path
        self.repo_config_path = repo_config_path

    def read(self) -> None:
        if self.repo_config_path:
            self.config.read(self.repo_config_path)
        if self.config_path:
            self.config.read(self.config_path)

    def write(self) -> None:
        if not self.dirty:
            return

        # Ensure the file is created with secure permissions, due to containing credentials
        with os.fdopen(
            os.open(self.config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w"
        ) as f:
            self.config.write(f)
        self.dirty = False

    def set_value(self, section: str, key: str, value: Optional[str]) -> None:
        if value is None:
            if self.config.has_option(section, key):
                self.config.remove_option(section, key)
                self.dirty = True
            return

        if not self.config.has_section(section):
            self.config.add_section(section)
            self.dirty = True

        if not self.config.has_option(section, key) or self.config.get(section, key) != value:
            self.config.set(section, key, value)
            self.dirty = True

    def get_config(self) -> configparser.ConfigParser:
        return self.config


def config_main(conf: Config, args: argparse.Namespace, all_parsers: List[RevupArgParser]) -> int:
    split_key = args.flag[0].replace("-", "_").split(".")
    if len(split_key) == 1:
        command = "revup"
        key = split_key[0]
    elif len(split_key) == 2:
        command = split_key[0]
        key = split_key[1]
    else:
        raise RevupUsageException("Invalid flag argument (must be <key> or <command>.<key>)")

    all_commands = {p.get_command(): p for p in all_parsers}
    if command not in all_commands:
        raise RevupUsageException(
            f"Invalid command section {command}, choose from {list(all_commands.keys())}"
        )

    parser = all_commands[command]
    actions = parser.get_actions()

    if not args.delete:
        if key not in actions:
            raise RevupUsageException(
                f"Invalid option key {key}, choose from {list(actions.keys())}"
            )

    if args.delete:
        value = None
        if args.value:
            raise RevupUsageException("Can't provide a value when using --delete")
    elif args.value:
        value = args.value
        if command == "revup" and key == "github_oauth":
            logging.warning(
                "Prefer to omit the value on command line when entering sensitive info. "
                "You may want to clear your shell history."
            )
    else:
        value = getpass.getpass(f"Input value for {command}.{key}: ").strip()

    config_path = conf.repo_config_path if args.repo else conf.config_path
    this_config = Config(config_path)
    this_config.read()

    if value is not None:
        # Check whether this value is allowed by the parser by attempting to set it
        # (this may throw if the value is not allowed)
        parser.set_option_default(key, actions[key], value)

        if command == "revup" and key == "github_username":
            # From https://www.npmjs.com/package/github-username-regex :
            # Github username may only contain alphanumeric characters or hyphens.
            # Github username cannot have multiple consecutive hyphens.
            # Github username cannot begin or end with a hyphen.
            # Maximum is 39 characters.
            if not re.match(r"^[a-z\d](?:[a-z\d]|-(?=[a-z\d])){0,38}$", value, re.I):
                raise ValueError(f"{value} is not a valid GitHub username")
        elif command == "revup" and key == "github_oauth":
            if not re.match(r"^[a-z\d_]+$", value, re.I):
                raise ValueError("Input string is not a valid oauth")

    this_config.set_value(command, key, value)
    this_config.write()
    return 0
