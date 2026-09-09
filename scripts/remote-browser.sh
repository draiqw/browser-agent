#!/bin/zsh
# Ходить к Chrome, который живёт на ДРУГОЙ машине, как к локальному.
#
# Зачем. Аккаунты не надо никуда копировать: сессия одна, живёт в одном месте,
# не рассинхронизируется и не протухает. Для ChatGPT это единственный способ без
# оговорок — OpenAI привязывает сессию к устройству, и перенесённые куки он
# может попросить подтвердить заново. Переносить всё же надо — scripts/accounts.py.
#
# Как. SSH пробрасывает удалённый 9222 на локальный порт. Наружу CDP при этом
# не выставляется НИ РАЗУ: на той стороне он как слушал 127.0.0.1, так и слушает,
# трафик идёт внутри ssh. Это принципиально — открытый CDP означает полный
# доступ к браузеру и всем кукам для любого, кто дотянется до порта.
#
#   ./remote-browser.sh user@host          # туннель, локальный порт 9222
#   ./remote-browser.sh user@host 9333     # если 9222 занят своим браузером
#   ./remote-browser.sh --status           # что сейчас проброшено
#   ./remote-browser.sh --stop             # закрыть туннели

set -u
REMOTE_PORT=9222

case "${1:-}" in
  --status)
    pids=$(pgrep -f "ssh -N -L .*:127.0.0.1:$REMOTE_PORT" 2>/dev/null)
    if [[ -z "$pids" ]]; then echo "туннелей нет"; exit 1; fi
    for p in ${(f)pids}; do
      echo "pid $p: $(ps -o command= -p $p | sed 's/.*-L /-L /')"
    done
    exit 0
    ;;
  --stop)
    pids=$(pgrep -f "ssh -N -L .*:127.0.0.1:$REMOTE_PORT" 2>/dev/null)
    [[ -z "$pids" ]] && { echo "туннелей нет"; exit 0; }
    kill ${(f)pids} 2>/dev/null
    echo "закрыто: ${(f)pids}"
    exit 0
    ;;
  '' | -h | --help)
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac

HOST="$1"
LOCAL_PORT="${2:-9222}"

# Занятый локальный порт — почти всегда свой же браузер. Молча проброситься
# поверх нельзя: получится, что команды уходят не туда, куда думает человек.
if lsof -nP -iTCP:$LOCAL_PORT -sTCP:LISTEN >/dev/null 2>&1; then
  echo "локальный порт $LOCAL_PORT уже занят:"
  lsof -nP -iTCP:$LOCAL_PORT -sTCP:LISTEN | tail -n +2 | awk '{print "  " $1, "pid", $2}'
  echo "выбери другой: $0 $HOST 9333   (и потом BU_MCP_CDP_URL=http://127.0.0.1:9333)"
  exit 1
fi

echo "поднимаю Chrome на $HOST..."
ssh "$HOST" 'bash -lc "~/browser-use/scripts/chrome-automation.sh start"' || {
  echo "не удалось запустить Chrome на той стороне."
  echo "Проверь, что browser-agent развёрнут в ~/browser-use на $HOST."
  exit 1
}

echo "пробрасываю $HOST:$REMOTE_PORT -> 127.0.0.1:$LOCAL_PORT..."
ssh -N -f -L "$LOCAL_PORT:127.0.0.1:$REMOTE_PORT" "$HOST" || { echo "туннель не поднялся"; exit 1; }

for i in {1..20}; do
  if curl -s -m 2 "http://127.0.0.1:$LOCAL_PORT/json/version" >/dev/null; then
    ver=$(curl -s -m 2 "http://127.0.0.1:$LOCAL_PORT/json/version" | python3 -c 'import json,sys;print(json.load(sys.stdin)["Browser"])' 2>/dev/null)
    echo "готово: $ver на $HOST доступен как http://127.0.0.1:$LOCAL_PORT"
    echo
    echo "чтобы bu-mcp ходил туда, задай ему переменную:"
    echo "  BU_MCP_CDP_URL=http://127.0.0.1:$LOCAL_PORT"
    [[ "$LOCAL_PORT" == 9222 ]] && echo "  (9222 — значение по умолчанию, можно ничего не задавать)"
    exit 0
  fi
  sleep 0.5
done
echo "туннель есть, но CDP через него не отвечает."
echo "Скорее всего Chrome на $HOST не поднялся: ssh $HOST '~/browser-use/scripts/chrome-automation.sh status'"
exit 1
