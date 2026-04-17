_revup() {
    local cur prev words cword
    _init_completion || return

    local subcommands="upload amend commit restack cherry-pick config toolkit"

    if [[ $cword -eq 1 ]]; then
        COMPREPLY=($(compgen -W "$subcommands -h --help -v --verbose --version" -- "$cur"))
        return
    fi

    local cmd="${words[1]}"

    case "$cmd" in
        amend|commit)
            if [[ "$cur" == -* ]]; then
                local opts="-h --help --no-edit --insert -i --drop -d --all -a --base-branch -b --relative-branch -e --no-parse-topics --no-parse-refs"
                COMPREPLY=($(compgen -W "$opts" -- "$cur"))
            else
                local topics
                topics=$(revup toolkit list-topics 2>/dev/null)
                COMPREPLY=($(compgen -W "$topics" -- "$cur"))
            fi
            ;;
        upload)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "-h --help --rebase -r --skip-confirm -s --dry-run -d --status -t --base-branch -b --relative-branch -e" -- "$cur"))
            else
                local topics
                topics=$(revup toolkit list-topics 2>/dev/null)
                COMPREPLY=($(compgen -W "$topics" -- "$cur"))
            fi
            ;;
        restack)
            COMPREPLY=($(compgen -W "-h --help --topicless-last -t --base-branch -b --relative-branch -e" -- "$cur"))
            ;;
        cherry-pick)
            COMPREPLY=($(compgen -W "-h --help --base-branch -b" -- "$cur"))
            ;;
        config)
            COMPREPLY=($(compgen -W "-h --help --repo -r --delete -d" -- "$cur"))
            ;;
        toolkit)
            if [[ $cword -eq 2 ]]; then
                COMPREPLY=($(compgen -W "detect-branch cherry-pick diff-target fork-point closest-branch list-topics" -- "$cur"))
            fi
            ;;
    esac
}

complete -F _revup revup
