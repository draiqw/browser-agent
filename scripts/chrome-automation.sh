#!/bin/zsh
# Отдельный Chrome для browser-use: свой профиль + CDP на 9222.
# ВСЕГДА headless: окна нет, в доке ничего не появляется, фокус не уводится.
# Режима с окном тут нет намеренно — headed-экземпляр альтабает владельца,
# и один раз оставшись жить, он переживал все последующие запуски скрипта.
# Chrome 136+ не отдаёт --remote-debugging-port на дефолтном профиле, поэтому нужен свой каталог.
DIR="$HOME/chrome-automation"
mkdir -p "$DIR"

if [[ "$1" == "--headed" ]]; then
  echo "режим с окном убран: он уводит фокус. Логиниться в профиле — руками:"
  echo "  open -na 'Google Chrome' --args --user-data-dir=$DIR --no-first-run"
  echo "и закрыть окно перед тем, как запускать автоматизацию."
  exit 2
fi

# Экземпляры на нашем профиле БЕЗ --headless. pgrep -f отдаёт и хелперы,
# поэтому оставляем только те, у кого в команде есть --remote-debugging-port,
# но нет --type= (то есть браузерный процесс, а не рендерер).
headed_pids() {
  for pid in ${(f)"$(pgrep -f -- "--user-data-dir=$DIR" 2>/dev/null)"}; do
    cmd=$(ps -o command= -p "$pid" 2>/dev/null)
    [[ "$cmd" == *"--type="* ]] && continue
    [[ "$cmd" == *"--remote-debugging-port=9222"* ]] || continue
    [[ "$cmd" == *"--headless"* ]] && continue
    echo "$pid"
  done
}

STALE=(${(f)"$(headed_pids)"})
if (( ${#STALE} )); then
  echo "нашёлся Chrome с окном на профиле автоматизации (${STALE}) — гашу"
  kill ${STALE} 2>/dev/null
  for i in {1..20}; do
    (( ${#${(f)"$(headed_pids)"}} )) || break
    sleep 0.25
  done
  kill -9 ${STALE} 2>/dev/null
  sleep 1
elif curl -s -o /dev/null -m 2 http://127.0.0.1:9222/json/version; then
  echo "уже работает на 9222 (headless)"
  exit 0
fi

"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --user-data-dir="$DIR" \
  --remote-debugging-port=9222 \
  --no-first-run --no-default-browser-check \
  --headless=new >/dev/null 2>&1 &
disown

for i in {1..40}; do
  curl -s -o /dev/null -m 1 http://127.0.0.1:9222/json/version && { echo "CDP на 9222 поднялся (headless)"; exit 0; }
  sleep 0.5
done
echo "не поднялся"; exit 1
