#!/bin/zsh
# Отдельный Chrome для browser-use: свой профиль + CDP на 9222.
# Chrome 136+ не отдаёт --remote-debugging-port на дефолтном профиле, поэтому нужен свой каталог.
#
# Режим по умолчанию — С ОКНОМ, но окно скрытое и фокус не забирает.
# Так пришлось сделать не из вкусовщины: headless=new не проходит Cloudflare
# на chatgpt.com и подобных — проверка виснет на "Verifying..." навсегда.
# Настоящий Chrome с настоящим профилем её проходит.
#
# Фокус защищён не отсутствием окна, а способом запуска: бинарник поднимается
# НАПРЯМУЮ, минуя LaunchServices. Через `open -a` нельзя — тот видит, что Chrome
# из этого бандла уже работает (у владельца открыт свой), и активирует ЕГО окно.
# Плюс флаги против троттлинга, чтобы скрытое окно не засыпало.
#
# Ровно одно место, где окно показывается специально, — подкоманда `login`:
# туда пользователь идёт сам, руками, чтобы залогиниться в аккаунт.
#
# История: раньше здесь был headless без вариантов, потому что однажды
# поднятый headed-экземпляр забирал фокус и ПЕРЕЖИВАЛ все последующие запуски
# (скрипт видел живой 9222 и молча выходил). Лечится это не запретом окна, а
# тем, что ниже: перед выходом сверяется РЕЖИМ живого процесса, и не тот
# режим — повод перезапустить, а не повод промолчать.

APP="/Applications/Google Chrome.app"
CHROME="$APP/Contents/MacOS/Google Chrome"

# --- профили -----------------------------------------------------------------
# Профиль — это связка «каталог Chrome + свой порт CDP». Разные профили держат
# разные наборы логинов и не видят кук друг друга, поэтому рабочий и личный
# аккаунт можно развести, не разлогиниваясь по очереди.
#
# Имя берётся из --profile/-p или из BU_PROFILE, по умолчанию `default`.
# У `default` каталог остаётся прежним (~/chrome-automation) и порт 9222 —
# чтобы всё, что уже настроено на 9222, продолжало работать без правок.
#
# Порт закрепляется за именем НАВСЕГДА и хранится в реестре: если раздавать
# порты по порядку запуска, один и тот же профиль каждый раз оказывался бы на
# новом порту, и BU_MCP_CDP_URL пришлось бы переписывать после каждого старта.
PROFILE="${BU_PROFILE:-default}"
args=()
while (( $# )); do
  case "$1" in
    --profile|-p) PROFILE="${2:-}"; shift 2 ;;
    --profile=*)  PROFILE="${1#*=}"; shift ;;
    *) args+=("$1"); shift ;;
  esac
done
set -- "${args[@]}"

if [[ ! "$PROFILE" =~ '^[A-Za-z0-9_-]+$' ]]; then
  echo "имя профиля может содержать только буквы, цифры, дефис и подчёркивание: '$PROFILE'"
  exit 2
fi

REGISTRY="$HOME/.config/bu-mcp/profiles"
mkdir -p "${REGISTRY:h}"
[[ -f "$REGISTRY" ]] || printf 'default\t9222\n' > "$REGISTRY"

profile_dir() {
  [[ "$1" == default ]] && { echo "$HOME/chrome-automation"; return }
  echo "$HOME/chrome-automation-$1"
}

profile_port() {
  local name="$1" line port used
  line=$(grep -E "^$name	" "$REGISTRY" 2>/dev/null | head -1)
  if [[ -n "$line" ]]; then echo "${line#*	}"; return; fi
  # Первый свободный порт начиная с 9222: и не занятый в реестре, и не слушаемый.
  port=9222
  while :; do
    used=$(cut -f2 "$REGISTRY" 2>/dev/null | grep -cx "$port")
    if (( used == 0 )) && ! lsof -nP -iTCP:$port -sTCP:LISTEN >/dev/null 2>&1; then break; fi
    (( port++ ))
  done
  printf '%s\t%s\n' "$name" "$port" >> "$REGISTRY"
  echo "$port"
}

DIR=$(profile_dir "$PROFILE")
PORT=$(profile_port "$PROFILE")
mkdir -p "$DIR"

# Браузерный процесс на нашем профиле (не рендерер и не хелпер: у тех есть --type=).
browser_pids() {
  for pid in ${(f)"$(pgrep -f -- "--user-data-dir=$DIR" 2>/dev/null)"}; do
    cmd=$(ps -o command= -p "$pid" 2>/dev/null)
    [[ "$cmd" == *"--type="* ]] && continue
    [[ "$cmd" == *"--remote-debugging-port=$PORT"* ]] || continue
    echo "$pid"
  done
}

# headless | windowed | пусто, если никто не работает.
running_mode() {
  for pid in ${(f)"$(browser_pids)"}; do
    cmd=$(ps -o command= -p "$pid" 2>/dev/null)
    [[ "$cmd" == *"--headless"* ]] && { echo headless; return }
    echo windowed; return
  done
}

cdp_up() { curl -s -o /dev/null -m 2 "http://127.0.0.1:$PORT/json/version" }

stop_chrome() {
  local pids=(${(f)"$(browser_pids)"})
  (( ${#pids} )) || return 0
  kill ${pids} 2>/dev/null
  for i in {1..20}; do
    (( ${#${(f)"$(browser_pids)"}} )) || return 0
    sleep 0.25
  done
  kill -9 ${pids} 2>/dev/null
  sleep 1
}

wait_cdp() {
  for i in {1..40}; do
    cdp_up && return 0
    sleep 0.5
  done
  return 1
}

common_args=(
  --user-data-dir="$DIR"
  --remote-debugging-port=$PORT
  --no-first-run --no-default-browser-check
)
# Скрытое окно macOS считает перекрытым, и Chrome начинает душить рендерер и
# таймеры — страница «живёт», но JS в ней еле шевелится. Эти три флага снимают
# именно это, ничего больше.
awake_args=(
  --disable-backgrounding-occluded-windows
  --disable-renderer-backgrounding
  --disable-background-timer-throttling
)
# Окно маленькое и в углу. Убрать его за пределы экрана НЕЛЬЗЯ: Chrome на macOS
# прижимает такое окно обратно на экран (проверено: --window-position=-32000
# даёт bounds left=0). Поэтому окно просто прячется ниже, через hide_app.
window_args=(--window-position=0,0 --window-size=900,700)

# Кто сейчас впереди (pid). Нужно, чтобы вернуть фокус туда же после запуска.
frontmost_app() {
  osascript -e 'tell application "System Events" to unix id of first process whose frontmost is true' 2>/dev/null
}

restore_front() {
  [[ -n "$1" ]] || return 0
  osascript -e "tell application \"System Events\" to set frontmost of (first process whose unix id is $1) to true" 2>/dev/null
}

# Спрятать приложение целиком: окно есть и рендерит, но на экране его нет.
# Одного этого мало — создание вкладки по CDP приложение раскрывает, — поэтому
# страховка от увода фокуса стоит ещё и в browser_use (preserve_frontmost).
# Спрятать НАШ Chrome и вернуть фокус тому, кто был впереди.
#
# Порядок здесь единственно верный: СПЕРВА спрятать, ПОТОМ вернуть фокус.
# Скрытие приложения само переназначает активное окно — macOS поднимает
# следующее, и оно почти никогда не то, что нужно. Если прятать после возврата,
# возврат тут же и затирается (проверено: фокус уезжал на Finder).
#
# Только по pid: «Google Chrome» называется и основной браузер владельца,
# по имени мы прятали бы именно его.
hide_app() {
  local pid=${1:-} back=${2:-} i vis
  [[ -n "$pid" ]] || return 0

  # Скрыть с дожатием. Одного раза не хватает: CDP отвечает раньше, чем Chrome
  # успевает создать окно, и приложение, спрятанное до этого момента, тут же
  # показывается обратно вместе с новым окном. Поэтому повторяем, пока
  # System Events не подтвердит, что оно действительно скрыто.
  for i in {1..15}; do
    osascript >/dev/null 2>&1 <<EOS
tell application "System Events"
  set visible of (first process whose unix id is $pid) to false
end tell
EOS
    sleep 0.2
    vis=$(osascript -e "tell application \"System Events\" to visible of (first process whose unix id is $pid)" 2>/dev/null)
    [[ "$vis" == "false" ]] && break
  done
  [[ "$vis" == "false" ]] || echo "предупреждение: окно спрятать не удалось (visible=$vis)" >&2

  [[ -n "$back" ]] || return 0
  osascript >/dev/null 2>&1 <<EOS
tell application "System Events"
  set frontmost of (first process whose unix id is $back) to true
end tell
EOS
}

start_hidden() {
  # Запускаем ДВОИЧНЫЙ файл напрямую, а не через `open`. `open -a` идёт через
  # LaunchServices, а тот считает, что приложение уже работает (у владельца
  # открыт свой Chrome из того же бандла), и активирует ЕГО. То есть команда,
  # которая должна была тихо поднять наш экземпляр, выносила вперёд чужое окно.
  # Прямой запуск бинарника в LaunchServices не заходит и чужой Chrome не трогает.
  local was=$(frontmost_app)
  "$CHROME" ${common_args} ${awake_args} ${window_args} >/dev/null 2>&1 &
  disown

  # Сторож на время запуска. Chrome при старте выходит вперёд, и это была
  # последняя остававшаяся кража фокуса: ~0.6 с, пока скрипт ждёт готовности CDP.
  #
  # Сторож — ОДИН процесс osascript с циклом внутри, а не цикл в шелле, который
  # дёргает osascript на каждом витке. Разница принципиальная: запуск osascript
  # стоит ~150 мс, то есть шелловый сторож медленнее той кражи, которую ловит
  # (проверено — он её не поймал ни разу).
  #
  # Свой Chrome отличаем от чужого по списку pid'ов, снятых ДО запуска: всё, что
  # называется Google Chrome и в этом списке не значится, — наше. По имени одному
  # ориентироваться нельзя, так зовётся и основной браузер владельца.
  if [[ -n "$was" ]]; then
    local before="$(pgrep -x 'Google Chrome' 2>/dev/null | tr '\n' ',')"
    osascript >/dev/null 2>&1 <<EOS &
set known to ",${before}"
tell application "System Events"
  repeat 200 times
    try
      set f to first process whose frontmost is true
      if name of f is "Google Chrome" then
        if known does not contain ("," & (unix id of f as string) & ",") then
          set frontmost of (first process whose unix id is $was) to true
        end if
      end if
    end try
    delay 0.05
  end repeat
end tell
EOS
    disown
  fi

  wait_cdp
  local rc=$?
  hide_app "$(browser_pids | head -1)" "$was"
  return $rc
}

start_headless() {
  "$CHROME" ${common_args} --headless=new >/dev/null 2>&1 &
  disown
}

want=windowed
[[ "${BU_HEADLESS:-0}" == (1|true|yes|on) ]] && want=headless

case "${1:-start}" in
  list)
    printf '%-14s %-6s %-9s %s\n' ПРОФИЛЬ ПОРТ СОСТОЯНИЕ КАТАЛОГ
    while IFS=$'\t' read -r name port; do
      [[ -n "$name" ]] || continue
      local_dir=$(profile_dir "$name")
      if curl -s -o /dev/null -m 1 "http://127.0.0.1:$port/json/version"; then st=работает; else st=остановлен; fi
      printf '%-14s %-6s %-9s %s\n' "$name" "$port" "$st" "$local_dir"
    done < "$REGISTRY"
    exit 0
    ;;

  install)
    # Запуск при входе в систему. Смысл не в удобстве: холодный старт Chrome
    # отбирает фокус примерно на 0.3 с, и убрать эти 0.3 с совсем не выходит —
    # столько же стоит один виток любого наблюдателя. Зато можно вынести старт
    # туда, где он никому не мешает: в момент логина, а не в середину работы.
    plist="$HOME/Library/LaunchAgents/com.bu-mcp.chrome-$PROFILE.plist"
    mkdir -p "${plist:h}"
    cat > "$plist" <<EOS
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.bu-mcp.chrome-$PROFILE</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string><string>-lc</string>
    <string>$0 --profile $PROFILE start</string>
  </array>
  <key>RunAtLoad</key><true/>
  <!-- Периодическая перепроверка вместо KeepAlive: скрипт завершается сразу,
       и KeepAlive крутил бы его в бесконечном цикле. А так раз в 5 минут он
       смотрит, жив ли браузер, и поднимает заново, если тот умер. -->
  <key>StartInterval</key><integer>300</integer>
  <key>StandardErrorPath</key><string>/tmp/bu-chrome-$PROFILE.err</string>
</dict>
</plist>
EOS
    launchctl unload "$plist" 2>/dev/null
    launchctl load "$plist" && echo "профиль '$PROFILE' будет подниматься при входе в систему" || {
      echo "launchctl load не сработал: $plist"; exit 1; }
    exit 0
    ;;

  uninstall)
    plist="$HOME/Library/LaunchAgents/com.bu-mcp.chrome-$PROFILE.plist"
    [[ -f "$plist" ]] || { echo "автозапуск для '$PROFILE' не установлен"; exit 0; }
    launchctl unload "$plist" 2>/dev/null
    rm -f "$plist"
    echo "автозапуск для '$PROFILE' убран"
    exit 0
    ;;

  show)
    # Показать окно, чтобы человек сделал руками то, что агент не может:
    # интерактивную капчу, подтверждение входа, 2FA. Автоматически такие вещи
    # не решаются и решаться не должны — правильная развязка в том, чтобы агент
    # остановился и позвал владельца, а не пытался обойти проверку.
    pid=$(browser_pids | head -1)
    [[ -n "$pid" ]] || { echo "профиль '$PROFILE' не запущен"; exit 1; }
    osascript >/dev/null 2>&1 <<EOS
tell application "System Events"
  set visible of (first process whose unix id is $pid) to true
  set frontmost of (first process whose unix id is $pid) to true
end tell
EOS
    echo "окно профиля '$PROFILE' на экране. Сделай, что нужно, потом: $0 --profile $PROFILE hide"
    exit 0
    ;;

  hide)
    pid=$(browser_pids | head -1)
    [[ -n "$pid" ]] || { echo "профиль '$PROFILE' не запущен"; exit 1; }
    hide_app "$pid" "$(frontmost_app)"
    echo "окно профиля '$PROFILE' убрано, работа продолжается"
    exit 0
    ;;

  status)
    have=$(running_mode)
    if [[ -z "$have" ]]; then echo "профиль '$PROFILE' (порт $PORT): не запущен"; exit 1; fi
    cdp_up && echo "профиль '$PROFILE': работает, режим $have, CDP на $PORT" || echo "профиль '$PROFILE': процесс есть ($have), но CDP на $PORT не отвечает"
    exit 0
    ;;

  stop)
    stop_chrome
    echo "остановлен"
    exit 0
    ;;

  login)
    # Единственное место, где окно показывается специально. Профиль тот же,
    # то есть куки, которые тут наберутся, потом видит автоматизация.
    stop_chrome
    # Запоминаем фокус ДО того, как показали окно. Спросить после — значит
    # получить в ответ сам Chrome и вернуть фокус тому, кого только что спрятали.
    login_back=$(frontmost_app)
    echo "Открываю профиль автоматизации с видимым окном."
    echo "Залогинься в нужные аккаунты (ChatGPT, Google, что нужно) и вернись сюда."
    "$CHROME" ${common_args} ${awake_args} >/dev/null 2>&1 &
    disown
    wait_cdp >/dev/null || echo "внимание: CDP на $PORT не поднялся, но окно должно быть открыто"
    echo
    printf 'Когда закончишь — нажми Enter, окно спрячется и браузер уйдёт в рабочий режим: '
    read -r _
    # Вернуть фокус туда, где человек был до открытия окна, — обычно терминал.
    # Без адреса возврата macOS после скрытия поднимает что попало (ловился Finder).
    hide_app "$(browser_pids | head -1)" "$login_back"
    echo "готово: логины сохранены в профиле, браузер работает на $PORT"
    exit 0
    ;;

  start|"") ;;

  --headed)
    echo "флага --headed больше нет: окно теперь и так режим по умолчанию (только скрытое)."
    echo "Чтобы залогиниться руками: $0 login"
    exit 2
    ;;

  *)
    echo "использование: $0 [start|login|stop|status|list|show|hide|install|uninstall] [--profile ИМЯ]"
    exit 2
    ;;
esac

have=$(running_mode)
if [[ -n "$have" ]]; then
  if [[ "$have" == "$want" ]] && cdp_up; then
    echo "уже работает на $PORT (режим $have)"
    exit 0
  fi
  # Либо режим не тот, либо процесс есть, а CDP мёртв. Оба случая — перезапуск.
  # Раньше здесь был молчаливый exit 0, и через него чужой режим жил вечно.
  echo "живой экземпляр в режиме '${have}', нужен '${want}' — перезапускаю"
  stop_chrome
fi

if [[ "$want" == headless ]]; then start_headless && wait_cdp; else start_hidden; fi

if cdp_up; then
  echo "CDP на $PORT поднялся (режим $want)"
  exit 0
fi
echo "не поднялся"; exit 1
