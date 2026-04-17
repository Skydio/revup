function __revup_topics
    revup toolkit list-topics 2>/dev/null
end

function __revup_needs_command
    set -l cmd (commandline -opc)
    test (count $cmd) -eq 1
end

function __revup_using_command
    set -l cmd (commandline -opc)
    test (count $cmd) -gt 1; and test $cmd[2] = $argv[1]
end

# Subcommands
complete -c revup -n __revup_needs_command -f -a upload -d 'Upload commits as pull requests'
complete -c revup -n __revup_needs_command -f -a amend -d 'Modify commits in the current stack'
complete -c revup -n __revup_needs_command -f -a commit -d 'Insert a new commit'
complete -c revup -n __revup_needs_command -f -a restack -d 'Restack commits onto updated base'
complete -c revup -n __revup_needs_command -f -a cherry-pick -d 'Cherry-pick a branch or PR'
complete -c revup -n __revup_needs_command -f -a config -d 'Get or set configuration values'
complete -c revup -n __revup_needs_command -f -a toolkit -d 'Exercise various subfunctionalities'
complete -c revup -n __revup_needs_command -f -s h -l help -d 'Show help'
complete -c revup -n __revup_needs_command -f -s v -l verbose -d 'Enable verbose output'
complete -c revup -n __revup_needs_command -f -l version -d 'Show version'

# amend / commit
for cmd in amend commit
    complete -c revup -n "__revup_using_command $cmd" -f -s h -l help -d 'Show help'
    complete -c revup -n "__revup_using_command $cmd" -f -l no-edit -d 'Skip editor'
    complete -c revup -n "__revup_using_command $cmd" -f -s i -l insert -d 'Insert new commit'
    complete -c revup -n "__revup_using_command $cmd" -f -s d -l drop -d 'Drop commit'
    complete -c revup -n "__revup_using_command $cmd" -f -s a -l all -d 'Stage modified/deleted files'
    complete -c revup -n "__revup_using_command $cmd" -f -s b -l base-branch -d 'Override base branch'
    complete -c revup -n "__revup_using_command $cmd" -f -s e -l relative-branch -d 'Override relative branch'
    complete -c revup -n "__revup_using_command $cmd" -f -l no-parse-topics -d 'Disable topic parsing'
    complete -c revup -n "__revup_using_command $cmd" -f -l no-parse-refs -d 'Disable ref parsing'
    complete -c revup -n "__revup_using_command $cmd" -f -a '(__revup_topics)' -d 'Topic'
end

# upload
complete -c revup -n '__revup_using_command upload' -f -s h -l help -d 'Show help'
complete -c revup -n '__revup_using_command upload' -f -s r -l rebase -d 'Rebase before uploading'
complete -c revup -n '__revup_using_command upload' -f -s s -l skip-confirm -d 'Skip confirmation'
complete -c revup -n '__revup_using_command upload' -f -s d -l dry-run -d 'Dry run'
complete -c revup -n '__revup_using_command upload' -f -s t -l status -d 'Show status'
complete -c revup -n '__revup_using_command upload' -f -s b -l base-branch -d 'Override base branch'
complete -c revup -n '__revup_using_command upload' -f -s e -l relative-branch -d 'Override relative branch'
complete -c revup -n '__revup_using_command upload' -f -a '(__revup_topics)' -d 'Topic'

# restack
complete -c revup -n '__revup_using_command restack' -f -s h -l help -d 'Show help'
complete -c revup -n '__revup_using_command restack' -f -s t -l topicless-last -d 'Put topicless commits last'
complete -c revup -n '__revup_using_command restack' -f -s b -l base-branch -d 'Override base branch'
complete -c revup -n '__revup_using_command restack' -f -s e -l relative-branch -d 'Override relative branch'

# cherry-pick
complete -c revup -n '__revup_using_command cherry-pick' -f -s h -l help -d 'Show help'
complete -c revup -n '__revup_using_command cherry-pick' -f -s b -l base-branch -d 'Override base branch'

# config
complete -c revup -n '__revup_using_command config' -f -s h -l help -d 'Show help'
complete -c revup -n '__revup_using_command config' -f -s r -l repo -d 'Use repo config'
complete -c revup -n '__revup_using_command config' -f -s d -l delete -d 'Delete config value'
