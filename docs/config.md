# NAME

revup config - Edit revup configuration files.

# SYNOPSIS

`revup config [--help] [--repo] [--delete] <flag> <value>`

# DESCRIPTION

Revup stores some persistent configuration values in a python configparser
compatible format. A repo specific configuration is read from the root of
the current git repo in a ".revupconfig" file. A user configuration is read
from REVUP_CONFIG_PATH if available, otherwise from the default path of
~/.revupconfig. Any flag or argument to a revup command can
be configured. Revup loads options in this order:

- The program has built in defaults that are given in the manual.
- Repo configs take precedence over the above.
- User configs take precedence over the above.
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

**--repo, -r**
: If specified, configuration value will be written to the file in the current
repo. Otherwise, value will apply globally.

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
