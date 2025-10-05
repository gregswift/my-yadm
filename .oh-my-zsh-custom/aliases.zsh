alias docker="podman"
alias git-rebase='git-refresh && git rebase -i $(git merge-base $(git branch -r | grep -Po "HEAD -> \K.*$") $(git branch --show-current))'
alias git-refresh='git fetch --all -p && git rebase origin/main'
alias jq="docker run --rm -i apteno/alpine-jq jq"

git-rebase-branch () {
  git-refresh && git-rebase -i $(git merge-base ${1} $(git branch --show-current))
}
