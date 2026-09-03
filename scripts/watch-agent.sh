#!/bin/zsh
# Живой лог действий браузерного агента: какой инструмент, куда кликает, что вводит.
# Берёт самый свежий непустой транскрипт сабагента текущего проекта и следит за ним.
# Использование: scripts/watch-agent.sh [каталог проекта в /private/tmp/claude-501]
DIR="${1:-/private/tmp/claude-501/$(pwd | tr "/." "--")}"
F=$(ls -t "$DIR"/*/tasks/*.output 2>/dev/null | while read f; do [[ -s "$f" ]] && { echo "$f"; break; }; done)
[[ -z "$F" ]] && { echo "транскриптов нет"; exit 1; }
echo "слежу за: $F"
tail -n +1 -f "$F" | python3 -u -c '
import json,sys
for line in sys.stdin:
    try: d=json.loads(line)
    except Exception: continue
    m=d.get("message",d)
    for c in (m.get("content") or []):
        if not isinstance(c,dict): continue
        if c.get("type")=="tool_use" and "bu-mcp" in c["name"]:
            a=c.get("input",{}) or {}
            keep={k:v for k,v in a.items() if k in("url","index","text","new_tab","css","query","direction","tab_id","keys","selector")}
            print("->", c["name"].replace("mcp__bu-mcp__",""), json.dumps(keep,ensure_ascii=False)[:160], flush=True)
        elif c.get("type")=="tool_result":
            body=c.get("content")
            if isinstance(body,list): body=" ".join(x.get("text","") for x in body if isinstance(x,dict))
            body=str(body or "")
            if "\"url\"" in body[:300] or "error" in body[:200].lower():
                print("   ", body[:200].replace("\n"," "), flush=True)
'
