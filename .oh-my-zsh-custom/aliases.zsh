alias docker="podman"
alias git-rebase='git-refresh && git rebase -i $(git merge-base $(git branch -r | grep -Po "HEAD -> \K.*$") $(git branch --show-current))'
alias git-refresh='git fetch --all -p && git rebase $(git branch -r | grep -Po "HEAD -> \K.*$")'
alias jq="docker run --rm -i apteno/alpine-jq jq"

git-rebase-branch () {
  git fetch --all -p
  git rebase ${1}
  git-rebase -i $(git merge-base ${1} $(git branch --show-current))
}
