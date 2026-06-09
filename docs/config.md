# NAME

revup config - Edit revup configuration files.

# SYNOPSIS

`revup config [--help] [--repo] [--delete] <flag> <value>`

# DESCRIPTION

Revup stores some persistent configuration values in a python configparser
compatible format. Any flag or argument to a revup command can be configured.
Revup loads options in this order:

- The program has built in defaults that are given in the manual.
- Global configs (~/.revupconfig) take precedence over the above. REVUP_CONFIG_PATH can override this path
- Repo configs (.revupconfig at the repo root) take precedence over the above.
- Repo-local configs (.git/.revupconfig) take precedence over the above.
- Command line flags specified by the user take highest precedence.

# OPTIONS

**`<flag>`**
: The name of the flag to be configured. Flags for revup subcommands can be
specified with "command.flag". Dashes will be replaced with underscores in the
underlying file.

**`<value>`**
: The desired value of the flag. Booleans are specified as "true" and "false".
If no value is specified, the user will be prompted to input the value in a
secure prompt. This is the preferred way to set certain sensitive fields, and
revup will warn if attempting to specify them directly.

**--help, -h**
: Show this help page.

**--global, -g**
: If specified, configuration value will be written to .revupconfig in the user's
home directory (overridable with REVUP_CONFIG_PATH). This is the default if repo
and repo-local are not specified.

**--repo, -r**
: If specified, configuration value will be written to .revupconfig at the root
of the current repo.

**--repo-local, -l**
: If specified, configuration value will be written to .revupconfig inside the
current repo's .git directory (common to all worktrees).

**--delete, -d**
: Delete the value with the given flag key.

# EXAMPLES

The default value for `revup upload --skip-confirm` is `false`. The user
can override this by running

: $ `revup config upload.skip_confirm true`

which adds this section to .revupconfig.
```
[upload]
skip_confirm = True
```
If the user then wants to temporarily override their config, they can
run `revup upload --no-skip-confirm`.
