#!/usr/bin/env bash
# =====================================================================
#  Сколько ресурсов тратит торговый робот (без вмешательства в его работу).
#     bash robot-load.sh        — за последние 7 суток
#     DAYS=1 bash robot-load.sh — за сутки
#  Берёт итоги каждого запуска из журнала systemd («Consumed … CPU time,
#  … memory peak»), считает по задачам: число запусков, длительность,
#  процессорное время, пик памяти — и сравнивает с конфигурацией Selectel
#  (1 vCPU с гарантированной долей 20%, 1 ГБ памяти).
# =====================================================================
set -uo pipefail
DAYS=${DAYS:-7}
journalctl -o short-unix --no-pager --since "-${DAYS}d" -u 'trading-robot@*' 2>/dev/null > /tmp/robot-load.log
free -m | awk '/Mem:/{print "MEM", $2, $3, $7}' >> /tmp/robot-load.log
python3 - /tmp/robot-load.log "$DAYS" <<'PY'
import re, sys, statistics as st
lines = open(sys.argv[1]).read().splitlines(); days = sys.argv[2]
start, runs = {}, {}
def secs(v):   # "1min 2.345s" / "3.512s" / "812ms"
    t = 0.0
    for n, u in re.findall(r"([\d.]+)(min|ms|s|h)", v):
        t += float(n) * {"h": 3600, "min": 60, "s": 1, "ms": 0.001}[u]
    return t
def mb(v):
    m = re.match(r"([\d.]+)([KMG])", v); return float(m.group(1)) * {"K": 1/1024, "M": 1, "G": 1024}[m.group(2)] if m else None
mem = None
for ln in lines:
    if ln.startswith("MEM "): mem = list(map(int, ln.split()[1:])); continue
    m = re.match(r"([\d.]+) \S+ \S+: (trading-robot@([\w-]+)\.service): (.*)", ln)
    if m:
        t, unit, inst, msg = float(m.group(1)), m.group(2), m.group(3), m.group(4)
        c = re.search(r"Consumed (.+?) CPU time(?:, ([\d.]+[KMG]) memory peak)?", msg)
        if c:
            r = runs.setdefault(inst, [])
            r.append({"cpu": secs(c.group(1)), "mem": mb(c.group(2) or ""), "wall": t - start.pop(inst, t)})
        continue
    m = re.match(r"([\d.]+) \S+ systemd\[1\]: Starting .*\((\w[\w-]*)\)", ln)
    if m: start[m.group(2)] = float(m.group(1))
if not runs:
    print(f"В журнале за {days} сут нет итогов запусков робота (строк «Consumed …»)."); sys.exit()
NAMES = {"pass": "Проход стратегии", "collateral": "Ребаланс LQDT", "trader": "Данные помощника", "news": "Дайджест рынка", "check": "Проверка"}
print(f"Нагрузка торгового робота за {days} сут\n")
print(f"{'Задача':20s}{'запусков':>9s}{'длит., с':>10s}{'CPU, с':>9s}{'CPU, ядер':>11s}{'память, МБ':>12s}")
worst_core, worst_mem, worst_cpu = 0, 0, 0
for inst, r in sorted(runs.items(), key=lambda x: -len(x[1])):
    wall = [x["wall"] for x in r if x["wall"] > 0]; cpu = [x["cpu"] for x in r]; mm = [x["mem"] for x in r if x["mem"]]
    load = max((x["cpu"] / x["wall"] for x in r if x["wall"] > 0.5), default=0)
    worst_core = max(worst_core, load); worst_cpu = max(worst_cpu, max(cpu)); worst_mem = max(worst_mem, max(mm, default=0))
    print(f"{NAMES.get(inst, inst):20s}{len(r):9d}{(max(wall) if wall else 0):10.0f}{max(cpu):9.1f}{load:11.2f}{(max(mm) if mm else 0):12.0f}")
print("\nдлит. и CPU — худший запуск; «ядер» — процессорное время / длительность (1.0 = одно ядро целиком)")
if mem: print(f"Память сервера сейчас: всего {mem[0]} МБ, занято {mem[1]} МБ, доступно {mem[2]} МБ")
print("\nДля Selectel 1 vCPU (доля 20%) / 1 ГБ:")
print(f"  процессор: пик {worst_core:.2f} ядра; самый тяжёлый запуск ({worst_cpu:.0f} с CPU) при гарантированных 20% займёт до ~{worst_cpu/0.2:.0f} с — "
      + ("хватает" if worst_cpu/0.2 <= 120 else "медленно, лучше 2 vCPU или долю 50%"))
if mem:
    need = mem[1] + worst_mem
    print(f"  память: {'хватает' if need < 800 else 'впритык — лучше 2 ГБ'} (занято сейчас {mem[1]} МБ + пик робота {worst_mem:.0f} МБ ≈ {need:.0f} МБ из ~960)")
PY
