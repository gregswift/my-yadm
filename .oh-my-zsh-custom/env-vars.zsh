export EDITOR=nano

export NEW_RELIC_API_KEY=$(op item get "New Relic Sandbox" --fields credential)
export NEW_RELIC_ACCOUNT_ID=$(op item get "New Relic Sandbox" --fields "account ID")
