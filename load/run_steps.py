"""Ступенчатое испытание: запускает k6 по ступеням и параллельно снимает потребление ресурсов.

    python load/run_steps.py --scenario mixed --rates 1,2,4,8,16,32 --duration 30s
    python load/run_steps.py --scenario heavy --rates 1,2,4 --duration 60s

Перед каждым испытанием база наполняется заново (--reseed делает это сам; нужны переменные из .env).
Между ступенями скрипт ждёт, пока загрузка процессора приложения вернётся к простою.
Испытание останавливается на первой ступени, не выдержавшей порог (p95 < 500 мс, ошибки < 1 %);
с ключом --all проходятся все ступени.

Результаты:
  results/steps_<scenario>.json      параметры, окружение, строки ступеней
  results/resources_<scenario>.csv   ряд потребления ресурсов раз в секунду (столбец step — текущая ступень)
  results/k6/<scenario>_<rate>.json  сводка k6 по ступени, .txt — вывод k6
"""
import argparse
import csv
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
CLK = os.sysconf("SC_CLK_TCK")
NCPU = os.cpu_count() or 1
P95_LIMIT_MS, ERR_LIMIT = 500.0, 0.01


# ---------- чтение /proc ----------
def read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def find_pids(kind, app_pattern):
    """kind: app — процессы сервиса (uvicorn app.main и их дочерние), db — процессы postgres, k6 — генератор нагрузки."""
    info = {}
    for name in os.listdir("/proc"):
        if name.isdigit():
            stat = read(f"/proc/{name}/stat")
            if stat:
                rest = stat.rsplit(")", 1)
                info[int(name)] = (int(rest[1].split()[1]), stat.split("(", 1)[1].rsplit(")", 1)[0])  # ppid, comm
    if kind in ("db", "k6"):
        want = "postgres" if kind == "db" else "k6"
        return [p for p, (_, comm) in info.items() if comm == want]
    roots = []
    for p, (_, comm) in info.items():
        if comm == "k6":
            continue
        cmd = (read(f"/proc/{p}/cmdline") or "").replace("\0", " ")
        if all(x in cmd for x in app_pattern) and "run_steps" not in cmd and "profile.py" not in cmd:
            roots.append(p)
    pids, grew = set(roots), True
    while grew:  # рабочие процессы (--workers N) запускаются дочерними и в командной строке слова uvicorn не имеют
        grew = False
        for p, (ppid, _) in info.items():
            if ppid in pids and p not in pids:
                pids.add(p)
                grew = True
    return sorted(pids)


def ticks(pid):
    stat = read(f"/proc/{pid}/stat")
    if not stat:
        return None
    parts = stat.rsplit(")", 1)[1].split()
    return int(parts[11]) + int(parts[12])  # utime + stime


def rss_mb(pid, field):
    status = read(f"/proc/{pid}/status") or ""
    m = re.search(rf"^{field}:\s+(\d+) kB", status, re.M)
    return int(m.group(1)) / 1024 if m else 0.0


def machine_busy():
    f = (read("/proc/stat") or "").splitlines()[0].split()[1:]
    vals = list(map(int, f))
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return sum(vals), idle


# ---------- сборщик ресурсов ----------
class Sampler(threading.Thread):
    def __init__(self, db_url, app_pattern, interval=1.0):
        super().__init__(daemon=True)
        self.db_url, self.app_pattern, self.interval = db_url, app_pattern, interval
        self.rows, self.step, self.stop_flag = [], "idle", threading.Event()
        self.t0 = time.time()

    def run(self):
        try:
            conn = psycopg.connect(self.db_url, autocommit=True)
        except Exception as e:  # noqa: BLE001
            print("предупреждение: нет подключения к БД для pg_stat_activity:", e, file=sys.stderr)
            conn = None
        prev = {}
        prev_total, prev_idle = machine_busy()
        prev_t = time.time()
        while not self.stop_flag.wait(self.interval):
            now = time.time()
            dt = now - prev_t
            groups = {k: find_pids(k, self.app_pattern) for k in ("app", "db", "k6")}
            cpu = {}
            for kind, pids in groups.items():
                total = 0.0
                for pid in pids:
                    t = ticks(pid)
                    if t is None:
                        continue
                    total += (t - prev.get((kind, pid), t)) / CLK / dt * 100
                    prev[(kind, pid)] = t
                cpu[kind] = max(total, 0.0)
            tot, idle = machine_busy()
            busy = 1 - (idle - prev_idle) / max(tot - prev_total, 1)
            prev_total, prev_idle, prev_t = tot, idle, now
            conns_total = conns_active = None
            if conn:
                try:
                    conns_total, conns_active = conn.execute(
                        "SELECT count(*), count(*) FILTER (WHERE state = 'active') FROM pg_stat_activity "
                        "WHERE datname = current_database() AND pid <> pg_backend_pid()").fetchone()
                except Exception:  # noqa: BLE001
                    pass
            self.rows.append({
                "t_sec": round(now - self.t0, 1), "step": self.step,
                "app_cpu_pct": round(cpu["app"], 1), "db_cpu_pct": round(cpu["db"], 1), "k6_cpu_pct": round(cpu["k6"], 1),
                "machine_cpu_pct": round(busy * NCPU * 100, 1),
                "app_rss_mb": round(sum(rss_mb(p, "VmRSS") for p in groups["app"]), 1),
                "db_rss_mb": round(sum(rss_mb(p, "RssAnon") for p in groups["db"]), 1),
                "conns_total": conns_total, "conns_active": conns_active,
            })

    def stop(self):
        self.stop_flag.set()
        self.join()

    def write_csv(self, path):
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(self.rows[0].keys()))
            w.writeheader()
            w.writerows(self.rows)

    def wait_idle(self, timeout=300):
        """Ждёт, пока процессор приложения и генератор нагрузки вернутся к простою (три секунды подряд)."""
        start = time.time()
        self.step = "idle"
        while time.time() - start < timeout:
            tail = self.rows[-3:]
            if len(tail) == 3 and all(r["app_cpu_pct"] < 5 and r["k6_cpu_pct"] < 5 for r in tail):
                return True
            time.sleep(1)
        return False


# ---------- вспомогательное ----------
def seconds(text):
    m = re.fullmatch(r"(\d+)(s|m)", text)
    return int(m.group(1)) * (60 if m.group(2) == "m" else 1)


def environment():
    cpu = platform.processor() or "?"
    try:
        for line in open("/proc/cpuinfo"):
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
        mem = next(int(l.split()[1]) for l in open("/proc/meminfo") if l.startswith("MemTotal"))
        mem_gb = round(mem / 1024 / 1024, 1)
    except OSError:
        mem_gb = None
    run = lambda *c: subprocess.run(c, capture_output=True, text=True, cwd=ROOT).stdout.strip()
    head = run("git", "rev-parse", "HEAD")
    head = head if re.fullmatch(r"[0-9a-f]{40}", head) else None
    return {"cpu": cpu, "cores": NCPU, "memory_gb": mem_gb, "os": platform.platform(), "python": platform.python_version(),
            "k6": run("k6", "version").split("\n")[0], "commit": head,
            "dirty": bool(run("git", "status", "--porcelain")) if head else None}


def parse_step(summary_path, scenario, rate, duration_s, exit_code):
    m = json.load(open(summary_path))["metrics"]
    dur = m.get(f"http_req_duration{{scenario:{scenario}}}", {})
    failed = m.get(f"http_req_failed{{scenario:{scenario}}}", {})
    iterations = m.get("iterations", {}).get("count", 0)
    row = {
        "rate": rate, "duration_s": duration_s,
        "p50_ms": dur.get("med"), "p95_ms": dur.get("p(95)"), "max_ms": dur.get("max"),
        "fail_rate": failed.get("value", 0.0), "requests": failed.get("passes", 0) + failed.get("fails", 0),
        "dropped": m.get("dropped_iterations", {}).get("count", 0),
        "iterations": iterations, "actual_rate": round(iterations / duration_s, 3),
        "rejections_409": m.get("rule_rejections", {}).get("count", 0), "k6_exit": exit_code,
        "errors_by_op": {k[4:]: v["count"] for k, v in m.items() if k.startswith("err_") and v.get("count")},
    }
    row["passed"] = bool(row["p95_ms"] is not None and row["p95_ms"] < P95_LIMIT_MS and row["fail_rate"] < ERR_LIMIT)
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", choices=("mixed", "heavy"), required=True)
    ap.add_argument("--rates", required=True, help="частоты ступеней через запятую, например 1,2,4,8")
    ap.add_argument("--duration", default="30s")
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--max-vus", default="500")
    ap.add_argument("--app-pattern", default="uvicorn,app.main", help="подстроки командной строки процесса сервиса")
    ap.add_argument("--reseed", action="store_true", help="перед испытанием наполнить базу заново (python seed.py --size work)")
    ap.add_argument("--all", action="store_true", help="не останавливаться на первой ступени, не выдержавшей порог")
    ap.add_argument("--tag", default="", help="суффикс имён файлов результатов (например, w4 для опыта с настройкой)")
    args = ap.parse_args()

    rates = [float(r) for r in args.rates.split(",")]
    dur_s = seconds(args.duration)
    name = args.scenario + (f"_{args.tag}" if args.tag else "")
    out = ROOT / "results"
    (out / "k6").mkdir(parents=True, exist_ok=True)
    db_url = os.environ.get("DATABASE_URL", "postgresql+psycopg://app:changeme@localhost:5432/anzhelika_merdenova").replace("+psycopg", "")

    if args.reseed:
        print("наполнение базы заново…")
        subprocess.run([sys.executable, "seed.py", "--size", "work"], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)

    sampler = Sampler(db_url, args.app_pattern.split(","))
    sampler.start()
    time.sleep(3)
    if not find_pids("app", args.app_pattern.split(",")):
        sampler.stop()
        sys.exit("не найден процесс сервиса (uvicorn app.main). Запустите сервис или задайте --app-pattern.")

    app_procs = len(find_pids("app", args.app_pattern.split(",")))
    steps = []
    print(f"сценарий {args.scenario}, ступени {args.rates}, длительность {args.duration}")
    print(f"{'частота':>8}{'p50, мс':>10}{'p95, мс':>10}{'макс, мс':>11}{'ошибки':>9}{'пропущено':>11}{'факт, 1/с':>11}  порог")
    for idx, rate in enumerate(rates):
        label = f"{rate:g}"
        sampler.step = label
        summary = out / "k6" / f"{name}_{label}.json"
        cmd = ["k6", "run", "--quiet", "--summary-export", str(summary),
               "-e", f"SCENARIO={args.scenario}", "-e", f"RATE={rate}", "-e", f"DURATION={args.duration}",
               "-e", f"BASE_URL={args.base}", "-e", f"MAX_VUS={args.max_vus}", "-e", f"OFFSET={idx * 1000}",
               str(ROOT / "load" / "k6" / "test.js")]
        res = subprocess.run(cmd, capture_output=True, text=True)
        (out / "k6" / f"{name}_{label}.txt").write_text(res.stdout + res.stderr)
        if not summary.exists():
            sampler.stop()
            sys.exit(f"k6 не выдал сводку (код {res.returncode}):\n{res.stderr[-800:]}")
        row = parse_step(summary, args.scenario, rate, dur_s, res.returncode)
        row["error_samples"] = sorted(set(re.findall(r'ERR ([^"\n]+)', res.stdout + res.stderr)))[:5]
        row["t_end_sec"] = round(time.time() - sampler.t0, 1)
        steps.append(row)
        print(f"{rate:>8g}{row['p50_ms']:>10.1f}{row['p95_ms']:>10.1f}{row['max_ms']:>11.1f}{row['fail_rate'] * 100:>8.2f}%"
              f"{row['dropped']:>11.0f}{row['actual_rate']:>11.2f}  {'выдержан' if row['passed'] else 'НАРУШЕН'}", flush=True)
        if not row["passed"] and not args.all:
            break
        if idx < len(rates) - 1:
            if not sampler.wait_idle():
                print("предупреждение: процессор не вернулся к простою за 5 минут, продолжаю", file=sys.stderr)
    sampler.wait_idle(60)
    sampler.stop()

    failed = [s for s in steps if not s["passed"]]
    knee = failed[0]["rate"] if failed else None
    ok = [s["rate"] for s in steps if s["passed"] and (knee is None or s["rate"] < knee)]
    result = {"scenario": args.scenario, "duration": args.duration, "rates": rates, "env": environment(), "app_processes": app_procs,
              "thresholds": {"p95_ms": P95_LIMIT_MS, "error_rate": ERR_LIMIT},
              "knee_rate": knee, "allowed_rate": max(ok) if ok else None, "steps": steps}
    (out / f"steps_{name}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    sampler.write_csv(out / f"resources_{name}.csv")
    print(f"\nточка излома: {knee if knee is not None else 'не достигнута'}; допустимая интенсивность: {max(ok) if ok else 'нет'}")
    print(f"результаты: results/steps_{name}.json, results/resources_{name}.csv")


if __name__ == "__main__":
    main()
