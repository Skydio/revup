# NAME

reset - reset current branch to upstream

# SYNOPSIS

**revup reset** [**--help**]

# DESCRIPTION

The `revup reset` command performs a hard reset of the current branch to match its upstream tracking branch. This is equivalent to running `git reset --hard @{u}`.

This command is useful when you want to discard all local changes and commits on the current branch and sync it exactly with the upstream version.

# OPTIONS

**--help**, **-h**
: Show help message and exit

# EXAMPLES

Reset the current branch to match upstream:

```
revup reset
```

# NOTES

- The command will fail if the current branch doesn't have an upstream tracking branch configured
- This operation discards all local changes and commits, so use with caution
- All uncommitted or unpushed changes will be lost permanently
- The working directory will be updated to match the upstream state

# SEE ALSO

**revup-upload**(1), **revup-restack**(1), **revup-amend**(1)
