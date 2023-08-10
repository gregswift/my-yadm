alias docker="podman"
alias git-rebase='git rebase -i $(git merge-base main $(git branch --show-current))'
alias jq="docker run --rm -i apteno/alpine-jq jq"
