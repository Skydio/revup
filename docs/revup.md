# NAME

revup - Efficient git workflow and code review toolkit

# SYNOPSIS

`revup [<options>] <command> [<args>]`

# DESCRIPTION

Revup is a python command-line toolkit for speeding up your
git workflow. It provides commit-based development support and
full github integration.

# OPTIONS

**--verbose, -v**
: Prints out extra details for debugging. This will include the
full command-line of any subprocesses that are run along with
their full output, as well as the full input and output of any
graphql requests to github.

**--help, -h**
: Show this help page.

**--proxy**
: Proxy to use when making connections to GitHub

**--github-oauth**
: The oauth token that provides login credentials to github. Revup
requires full repository read/write permissions in order to create
and modify reviews. This is represented by the "repo" section of
https://github.com/settings/tokens/new.

**--github-username**
: The user's github username for login.

**--github-url**
:  URL to use for github. Defaults to "github.com" and would only
need changed if the user is using github enterprise.

**--remote-name**
: The name of the remote that corresponds to github. Branches on this
remote are also used for base branch detection. Defaults to "origin".

**--fork-name**
: If specified, the name of the remote that corresponds to a github fork
that should be used to push branches to. The pull request will be created
using the branch from this fork. If empty, remote-name is used for both
pushing and creating the pull request.

**--editor**
: The user's preferred editor, used for various message and file
editing. If not set, value is taken first from "git config core.editor"
then from the GIT_EDITOR env value, then from EDITOR.

**--keep-temp, -k**
: Occasionally, files will need to be stored to disk for various
purposes, including temporary git indexes, commit messages, and
files. Normally, these are stored in a temporary directory that
is deleted when the program finishes. This flag changes the temporary
file directory to `.revup/` in the root of the current git repository.
This allows leftover temporary files to be examined for debugging.
Since this is only for debugging, no attempt is made to lock files
in this directory even though they could conflict between processes.

**--git-path**
: Specifies a custom path for the git binary. If not set, the result of
"/usr/bin/which git" is used.

**--git-version**
: Specifies the minimum git version required for revup functionality.
Set to a known upstream version by default, but can be overridden with
a fully specified version string such as "2.36.0" or empty string to
disable the version check.

**--main-branch**
: Specifies the main branch name, usually "main" or "master". This
is used in base branch detection. Default is "main".

**--base-branch-globs**
: Specifies a newline separated list of branch names or glob style
expressions that match all possible base branches. Used to determine
which branches are supported by base branch detection. See manpage of
git-for-each-ref/fnmatch(3) for more info on glob syntax.

# REVUP COMMANDS

Revup is comprised of several sub-commands.

**revup amend**
: Modify a commit in the current stack using the contents of the
cache. Can also change the commit text.

**revup cherry-pick**
: Create a squashed commit that represents the changes made in the
given branch relative to its base branch, then cherry-pick it.

**revup upload**
: Upload and push the current stack of code reviews to github.

**revup restack**
: Reorder the current stack so that commits in a topic are consecutive.

**revup toolkit**
: Various low-level subfunctionalities intended for advanced users or scripts.

# CONFIGURATION
Revup stores some persistent configuration values in a python configparser
compatible format. Any flag or argument to a revup command can be configured.
Revup loads options the following way, in order of lowest to highest precedence:

- The program has built in defaults that are given in the manual.
- Repo config is loaded from ".revupconfig" in the root of the current repo.
- User config is loaded from REVUP_CONFIG_PATH if available, otherwise "~/.revupconfig".
- User's specified command-line flags.

Each command corresponds to a section in the config file, and each flag
corresponds to a key with "-" replaced with "_".
Boolean flags can be negated on the command line by prefixing "--no-" to the
long form, or "-n" to the short for if it exists.

**Example:**
The default value for `revup upload --skip-confirm` is `false`. The user
can override this by adding this section to .revupconfig.
```
[upload]
skip_confirm = True
```
If the user then wants to temporarily override their config, they can
run `revup upload --no-skip-confirm`.


# ISSUES

See https://github.com/Skydio/revup/issues for a list of known issues.
