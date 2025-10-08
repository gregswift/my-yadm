# What OS are we running?
if [[ $(uname) == "Darwin" ]]; then
  export PATH=${PATH}:${HOME}/.local/bin
else

fi
