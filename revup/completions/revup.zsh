#compdef revup

_revup_topics() {
    local -a topics
    topics=(${(f)"$(revup toolkit list-topics 2>/dev/null)"})
    _describe 'topic' topics
}

_revup() {
    local -a subcommands
    subcommands=(
        'upload:Upload commits as pull requests'
        'amend:Modify commits in the current stack'
        'commit:Insert a new commit (alias for amend --insert)'
        'restack:Restack commits onto updated base'
        'cherry-pick:Cherry-pick a branch or PR'
        'config:Get or set configuration values'
        'toolkit:Exercise various subfunctionalities'
    )

    _arguments -C \
        '(-h --help)'{-h,--help}'[Show help]' \
        '(-v --verbose)'{-v,--verbose}'[Enable verbose output]' \
        '--version[Show version]' \
        '1:command:->command' \
        '*::arg:->args'

    case $state in
        command)
            _describe 'command' subcommands
            ;;
        args)
            case $words[1] in
                amend|commit)
                    _arguments \
                        '(-h --help)'{-h,--help}'[Show help]' \
                        '--no-edit[Skip editor]' \
                        '(-i --insert)'{-i,--insert}'[Insert new commit]' \
                        '(-d --drop)'{-d,--drop}'[Drop commit]' \
                        '(-a --all)'{-a,--all}'[Stage modified/deleted files]' \
                        '(-b --base-branch)'{-b,--base-branch}'[Override base branch]:branch' \
                        '(-e --relative-branch)'{-e,--relative-branch}'[Override relative branch]:branch' \
                        '--no-parse-topics[Disable topic parsing]' \
                        '--no-parse-refs[Disable ref parsing]' \
                        '::ref_or_topic:_revup_topics'
                    ;;
                upload)
                    _arguments \
                        '(-h --help)'{-h,--help}'[Show help]' \
                        '(-r --rebase)'{-r,--rebase}'[Rebase before uploading]' \
                        '(-s --skip-confirm)'{-s,--skip-confirm}'[Skip confirmation]' \
                        '(-d --dry-run)'{-d,--dry-run}'[Dry run]' \
                        '(-t --status)'{-t,--status}'[Show status]' \
                        '(-b --base-branch)'{-b,--base-branch}'[Override base branch]:branch' \
                        '(-e --relative-branch)'{-e,--relative-branch}'[Override relative branch]:branch' \
                        '*::topics:_revup_topics'
                    ;;
                restack)
                    _arguments \
                        '(-h --help)'{-h,--help}'[Show help]' \
                        '(-t --topicless-last)'{-t,--topicless-last}'[Put topicless commits last]' \
                        '(-b --base-branch)'{-b,--base-branch}'[Override base branch]:branch' \
                        '(-e --relative-branch)'{-e,--relative-branch}'[Override relative branch]:branch'
                    ;;
                cherry-pick)
                    _arguments \
                        '(-h --help)'{-h,--help}'[Show help]' \
                        '(-b --base-branch)'{-b,--base-branch}'[Override base branch]:branch' \
                        ':branch_or_pr_url:'
                    ;;
                config)
                    _arguments \
                        '(-h --help)'{-h,--help}'[Show help]' \
                        '(-r --repo)'{-r,--repo}'[Use repo config]' \
                        '(-d --delete)'{-d,--delete}'[Delete config value]' \
                        ':flag:' \
                        '::value:'
                    ;;
                toolkit)
                    local -a toolkit_cmds
                    toolkit_cmds=(
                        'detect-branch:Detect base branch'
                        'cherry-pick:Cherry-pick commit to new parent'
                        'diff-target:Make virtual diff target'
                        'fork-point:Find fork point'
                        'closest-branch:Find nearest base branch'
                        'list-topics:List all topics'
                    )
                    _describe 'toolkit command' toolkit_cmds
                    ;;
            esac
            ;;
    esac
}

_revup "$@"
