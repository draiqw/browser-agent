#!/bin/zsh
# Отдельный Chrome для browser-use: свой профиль + CDP на 9222.
# По умолчанию headless — в доке ничего не появляется, окон не открывает.
# Запусти с --headed, когда надо руками залогиниться в этом профиле.
# Chrome 136+ не отдаёт --remote-debugging-port на дефолтном профиле, поэтому нужен свой каталог.
DIR="$HOME/chrome-automation"
mkdir -p "$DIR"

if curl -s -o /dev/null -m 2 http://127.0.0.1:9222/json/version; then
  echo "уже работает на 9222"
  exit 0
fi

MODE="--headless=new"
[[ "$1" == "--headed" ]] && MODE=""

"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --user-data-dir="$DIR" \
  --remote-debugging-port=9222 \
  --no-first-run --no-default-browser-check \
  ${MODE} >/dev/null 2>&1 &
disown

for i in {1..40}; do
  curl -s -o /dev/null -m 1 http://127.0.0.1:9222/json/version && { echo "CDP на 9222 поднялся${MODE:+ (headless)}"; exit 0; }
  sleep 0.5
done
echo "не поднялся"; exit 1
