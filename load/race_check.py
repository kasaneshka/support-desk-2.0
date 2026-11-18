"""Проверка одновременного доступа: N параллельных запросов переводят одну заявку «новая → в работе».

    python load/race_check.py [--base http://localhost:8000] [--n 20] [--rounds 10]

Правило: переход допустим один раз, поэтому в каждом раунде должен быть ровно один ответ 200, остальные — 409.
Результат печатается и пишется в results/race.json.
"""
import argparse
import json
import os
import threading
from collections import Counter

import httpx

ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://localhost:8000")
ap.add_argument("--n", type=int, default=20)
ap.add_argument("--rounds", type=int, default=10)
ap.add_argument("--out", default="results/race.json")
args = ap.parse_args()


def client():
    c = httpx.Client(base_url=args.base, timeout=60)
    assert c.post("/api/login", json={"login": "agent1", "password": "demo"}).status_code == 200
    return c


main = client()
clients = [client() for _ in range(args.n)]
codes_total, ok_per_round = Counter(), []
for _ in range(args.rounds):
    tid = main.post("/api/tickets", json={"title": "race", "body": "race", "category_id": 1}).json()["id"]
    barrier = threading.Barrier(args.n)
    codes = [None] * args.n

    def run(i):
        barrier.wait()
        codes[i] = clients[i].patch(f"/api/tickets/{tid}/status", json={"status": "in_progress"}).status_code

    threads = [threading.Thread(target=run, args=(i,)) for i in range(args.n)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    codes_total.update(codes)
    ok_per_round.append(codes.count(200))

result = {"parallel_requests": args.n, "rounds": args.rounds, "codes": dict(codes_total), "ok_per_round": ok_per_round,
          "rounds_with_more_than_one_200": sum(1 for k in ok_per_round if k > 1)}
os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
json.dump(result, open(args.out, "w"), ensure_ascii=False, indent=2)
print(json.dumps(result, ensure_ascii=False, indent=2))
