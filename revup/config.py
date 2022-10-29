import argparse
import configparser
import getpass
import logging
import os
import re

from revup.types import RevupUsageException


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

    def set_value(self, section: str, key: str, value: str) -> None:
        if not self.config.has_section(section):
            self.config.add_section(section)
            self.dirty = True

        if not self.config.has_option(section, key) or self.config.get(section, key) != value:
            self.config.set(section, key, value)
            self.dirty = True

    def get_config(self) -> configparser.ConfigParser:
        return self.config


def config_main(conf: Config, args: argparse.Namespace) -> int:
    split_key = args.flag[0].replace("-", "_").split(".")
    if len(split_key) == 1:
        command = "revup"
        key = split_key[0]
    elif len(split_key) == 2:
        command = split_key[0]
        key = split_key[1]
    else:
        raise RevupUsageException("Invalid flag argument (must be <key> or <command>.<key>)")

    if not args.value:
        value = getpass.getpass(f"Input value for {command}.{key}: ").strip()
    else:
        value = args.value

        if command == "revup" and key == "github_oauth":
            logging.warning(
                "Prefer to omit the value on command line when entering sensitive info. "
                "You may want to clear your shell history."
            )

    config_path = conf.repo_config_path if args.repo else conf.config_path
    this_config = Config(config_path)
    this_config.read()

    if command == "revup" and key == "github_username":
        # From https://www.npmjs.com/package/github-username-regex :
        # Github username may only contain alphanumeric characters or hyphens.
        # Github username cannot have multiple consecutive hyphens.
        # Github username cannot begin or end with a hyphen.
        # Maximum is 39 characters.
        if not re.match(r"^[a-z\d](?:[a-z\d]|-(?=[a-z\d])){0,38}$", value, re.I):
            raise ValueError(f"{args.value} is not a valid GitHub username")
    elif command == "revup" and key == "github_oauth":
        if not re.match(r"^[a-z\d_]+$", value, re.I):
            raise ValueError("Input string is not a valid oauth")
    elif command == "config":
        raise RevupUsageException("Can't set defaults for the config command itself")

    this_config.set_value(command, key, value)
    this_config.write()
    return 0
