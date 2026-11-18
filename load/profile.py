"""Профиль приложения под нагрузкой (py-spy) во время испытания тяжёлой операции.

    python load/profile.py --rate 0.05                # снять профиль на частоте предела тяжёлой операции
    python load/profile.py --analyze results/profile.raw   # только разобрать уже снятый профиль

Скрипт запускает k6 (сценарий heavy) на заданной частоте, через --warmup секунд снимает профиль процесса
сервиса (--seconds секунд в текстовом виде и столько же в виде флеймграфа) и разбирает его на горячие функции.
py-spy требует прав: если вы не root, команда запускается через sudo (будет запрошен пароль).

Результаты: results/profile.raw (свёрнутые стеки), results/profile.svg (флеймграф), results/profile_top.json.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_steps import ROOT, find_pids, ticks  # noqa: E402

FRAME = re.compile(r"^(?P<func>.+?) \((?P<file>.+?):(?P<line>\d+)\)$")


def parse_frame(frame):
    m = FRAME.match(frame.strip())
    if not m:
        return frame.strip(), "", 0
    return m["func"], m["file"], int(m["line"])


def is_service(path):
    p = path.replace("\\", "/")
    return bool(re.search(r"(^|/)app/[^/]+\.py$", p)) and "site-packages" not in p and "dist-packages" not in p


def analyze(raw_path, top=8):
    samples, total = [], 0
    for line in Path(raw_path).read_text().splitlines():
        stack, _, count = line.rpartition(" ")
        if not stack or not count.isdigit():
            continue
        samples.append(([parse_frame(f) for f in stack.split(";")], int(count)))
        total += int(count)
    self_c, cum_c, svc_c, pair_c = Counter(), Counter(), Counter(), Counter()
    for frames, n in samples:  # frames: от корня стека к листу
        leaf = frames[-1]
        key = lambda f: f"{f[0]} ({f[1]})"
        self_c[key(leaf)] += n
        for k in {key(f) for f in frames}:
            cum_c[k] += n
        service = [f for f in frames if is_service(f[1])]
        caller = service[-1] if service else None  # ближайшая к листу функция сервиса
        if caller:
            svc_c[f"{caller[0]} ({caller[1]}:{caller[2]})"] += n
            pair_c[(key(leaf), f"{caller[0]} ({caller[1]}:{caller[2]})")] += n
    share = lambda n: round(100 * n / total, 1) if total else 0.0
    lib_c = Counter()
    for frames, n in samples:
        lib_c[library(frames[-1][1])] += n
    best_caller = {}
    for (leaf, caller), n in pair_c.items():
        if leaf not in best_caller or n > best_caller[leaf][1]:
            best_caller[leaf] = (caller, n)
    return {
        "total_samples": total,
        "by_library": [{"library": k, "share_pct": share(n)} for k, n in lib_c.most_common()],
        "top_self": [{"function": k, "share_pct": share(n), "service_caller": best_caller.get(k, (None,))[0]}
                     for k, n in self_c.most_common(top)],
        "top_cumulative": [{"function": k, "share_pct": share(n)} for k, n in cum_c.most_common(top)],
        "service_functions": [{"function": k, "share_pct": share(n), "code": source_line(k)} for k, n in svc_c.most_common(top)],
    }


def source_line(path_and_line):
    """Текст строки кода по «файл:строка» (путь относительно корня репозитория)."""
    m = re.match(r"(.+):(\d+)\)?$", path_and_line.split("(")[-1])
    try:
        lines = (ROOT / m[1]).read_text().splitlines()
        return lines[int(m[2]) - 1].strip()
    except (OSError, IndexError, TypeError):
        return None


def library(path):
    p = path.replace("\\", "/")
    if is_service(p):
        return "сервис (app/)"
    for key, name in (("psycopg", "драйвер psycopg"), ("sqlalchemy", "SQLAlchemy"), ("threading", "стандартная библиотека (threading)"),
                      ("selectors", "стандартная библиотека (selectors)"), ("anyio", "anyio/starlette"), ("starlette", "anyio/starlette"),
                      ("fastapi", "anyio/starlette"), ("uvicorn", "uvicorn")):
        if key in p:
            return name
    return "прочее"


def print_top(res):
    print(f"снимков: {res['total_samples']}")
    print("\nпо библиотекам (где находится вершина стека):")
    for r in res["by_library"]:
        print(f"  {r['share_pct']:5.1f} %  {r['library']}")
    print("\nпо времени в самой функции (self):")
    for r in res["top_self"]:
        print(f"  {r['share_pct']:5.1f} %  {r['function']}" + (f"\n           ← из {r['service_caller']}" if r["service_caller"] else ""))
    print("\nпо функциям сервиса (доля снимков, где функция сервиса — ближайшая к вершине стека):")
    for r in res["service_functions"]:
        print(f"  {r['share_pct']:5.1f} %  {r['function']}" + (f"\n           {r['code']}" if r.get("code") else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", type=float, help="частота тяжёлой операции, запросов в секунду (предел пропускной способности)")
    ap.add_argument("--seconds", type=int, default=15, help="длительность снятия профиля, не короче 10")
    ap.add_argument("--warmup", type=int, default=8)
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--app-pattern", default="uvicorn,app.main")
    ap.add_argument("--analyze", help="разобрать готовый свёрнутый профиль и выйти")
    args = ap.parse_args()

    out = ROOT / "results"
    out.mkdir(exist_ok=True)
    if args.analyze:
        res = analyze(args.analyze)
        print_top(res)
        return
    if not args.rate:
        sys.exit("укажите --rate")
    spy = shutil.which("py-spy") or sys.exit("py-spy не найден: pip install py-spy")
    pattern = args.app_pattern.split(",")
    pids = find_pids("app", pattern) or sys.exit("не найден процесс сервиса (uvicorn app.main)")
    pid = max(pids, key=lambda p: ticks(p) or 0)  # при нескольких процессах — самый загруженный
    sudo = [] if os.geteuid() == 0 else ["sudo"]

    (out / "k6").mkdir(exist_ok=True)
    load_s = args.warmup + 2 * args.seconds + 10
    k6 = subprocess.Popen(
        ["k6", "run", "--quiet", "--summary-export", str(out / "k6" / "profile_heavy.json"),
         "-e", "SCENARIO=heavy", "-e", f"RATE={args.rate}", "-e", f"DURATION={load_s}s", "-e", f"BASE_URL={args.base}",
         str(ROOT / "load" / "k6" / "test.js")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"нагрузка: сводка, {args.rate} запр./с, {load_s} с; профиль процесса {pid} начнётся через {args.warmup} с")
    time.sleep(args.warmup)
    for fmt, target in (("raw", "profile.raw"), ("flamegraph", "profile.svg")):
        print(f"py-spy: {target} ({args.seconds} с)…", flush=True)
        subprocess.run(sudo + [spy, "record", "--pid", str(pid), "--duration", str(args.seconds), "--rate", "100",
                               "--format", fmt, "--output", str(out / target)], check=True)
    k6.wait()
    res = analyze(out / "profile.raw")
    res.update({"pid": pid, "rate": args.rate, "seconds": args.seconds,
                "py_spy": subprocess.run([spy, "--version"], capture_output=True, text=True).stdout.strip()})
    (out / "profile_top.json").write_text(json.dumps(res, ensure_ascii=False, indent=2))
    print_top(res)


if __name__ == "__main__":
    main()
