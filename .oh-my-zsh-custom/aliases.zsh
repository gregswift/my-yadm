alias docker="podman"
alias git-rebase='git rebase -i $(git merge-base main $(git branch --show-current))'
alias jq="docker run --rm -i apteno/alpine-jq jq"

# AWS creds for terraform
alias tfaws='eval $(aws configure export-credentials --format env --profile iat)'
# Clear AWS creds
alias unsetaws='unset AWS_ACCESS_KEY_ID; unset AWS_SECRET_ACCESS_KEY; unset AWS_SESSION_TOKEN; unset AWS_CREDENTIAL_EXPIRATION'
