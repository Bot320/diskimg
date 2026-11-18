[[ $- != *i* ]] && return
PS1='\[\e[1;32m\]\u@rescue\[\e[0m\]:\w\$ '
alias ls='ls --color=auto'
