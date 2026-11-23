#!/bin/zsh
# Save a local encrypted credential or start the existing exam worker.
set +x
set -eu
umask 077
cd -- "${0:a:h}"
vault=".local/yandex-tasks.enc"
cipher=(-aes-256-cbc -salt -pbkdf2 -iter 600000 -md sha256)
tty_state=""
tmp=""
cleanup() {
  if [[ -n "$tty_state" ]]; then stty "$tty_state" </dev/tty; fi
  if [[ -n "$tmp" && -f "$tmp" ]]; then rm -f -- "$tmp"; fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

read_secret() {
  tty_state=$(stty -g </dev/tty)
  stty -echo </dev/tty
  print -n -- "$1: " >/dev/tty
  IFS= read -r REPLY </dev/tty
  stty "$tty_state" </dev/tty
  tty_state=""
  print >/dev/tty
}

case "${1:-}" in
  save-token)
    mkdir -p -- .local
    tmp=$(mktemp .local/.token.XXXXXXXX)
    read_secret 'Вставь только OAuth-токен Яндекс Заданий и нажми Enter (символы скрыты)'
    token=$REPLY
    if [[ -z "$token" || "$token" == *[[:space:]]* || "$token" == *://* || "$token" == *access_token=* ]]; then
      print -u2 -- 'Нужен сам токен из кабинета Яндекса, без адреса страницы.'
      exit 1
    fi
    read_secret 'Придумай пароль для зашифрованного файла'
    password=$REPLY
    read_secret 'Повтори пароль'
    if [[ -z "$password" || "$password" != "$REPLY" ]]; then
      print -u2 -- 'Пароли должны совпадать и не могут быть пустыми.'
      exit 1
    fi
    print -rn -- "$token" | /usr/bin/openssl enc "${cipher[@]}" -out "$tmp" -pass fd:3 3< <(print -r -- "$password")
    unset REPLY token password
    mv -f -- "$tmp" "$vault"
    print -- "Токен сохранён в зашифрованном файле: $PWD/$vault"
    ;;
  once|run|main-qc|main-qc-watch|main-accept-watch)
    if [[ ! -s "$vault" ]]; then
      print -u2 -- 'Сначала выполни: zsh exam.zsh save-token'
      exit 1
    fi
    read_secret 'Введи пароль зашифрованного файла'
    YANDEX_TASKS_TOKEN=$(/usr/bin/openssl enc -d "${cipher[@]}" -in "$vault" -pass fd:3 3< <(print -r -- "$REPLY"))
    unset REPLY
    export YANDEX_TASKS_TOKEN
    if [[ "$1" == main-qc ]]; then
      exec /usr/bin/python3 main_qc.py check --apply
    elif [[ "$1" == main-qc-watch ]]; then
      exec /usr/bin/python3 main_qc.py check --apply --watch
    elif [[ "$1" == main-accept-watch ]]; then
      exec /usr/bin/python3 main_qc.py check --apply --watch --accept-passing
    fi
    if [[ "$1" == once ]]; then
      exec /usr/bin/python3 sync_exam.py --apply
    fi
    exec /usr/bin/python3 sync_exam.py --apply --watch 60
    ;;
  *)
    print -u2 -- 'Команды: save-token | once | run | main-qc | main-qc-watch | main-accept-watch'
    exit 2
    ;;
esac
