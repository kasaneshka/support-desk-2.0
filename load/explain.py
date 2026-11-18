"""Планы выполнения запросов списка и сводки на рабочем наполнении: python load/explain.py

Нужны для проверки гипотез первого задания (сортировка без индекса, число запросов в сводке).
Результат печатается и пишется в results/explain.txt.
"""
import os
from pathlib import Path

import psycopg

url = os.environ.get("DATABASE_URL", "postgresql+psycopg://app:changeme@localhost:5432/anzhelika_merdenova").replace("+psycopg", "")
S = os.environ.get("DB_SCHEMA", "anzhelika_merdenova")
QUERIES = [
    ("Список: число заявок (count)", f'SELECT count(*) FROM "{S}".tickets'),
    ("Список: страница, сортировка по дате создания", f'SELECT * FROM "{S}".tickets ORDER BY created_at DESC, id DESC LIMIT 20 OFFSET 0'),
    ("Список: фильтр status=new", f"SELECT * FROM \"{S}\".tickets WHERE status = 'new' ORDER BY created_at DESC, id DESC LIMIT 20 OFFSET 0"),
    ("Сводка: комментарии одной заявки (запрос, который выполняется в цикле)",
     f'SELECT * FROM "{S}".comments WHERE ticket_id = 70000 ORDER BY created_at, id'),
]
out = []
with psycopg.connect(url) as db:
    for title, sql in QUERIES:
        rows = db.execute("EXPLAIN (ANALYZE, BUFFERS, COSTS OFF) " + sql).fetchall()
        out.append(f"-- {title}\n{sql}\n" + "\n".join(r[0] for r in rows) + "\n")
text = "\n".join(out)
Path("results").mkdir(exist_ok=True)
Path("results/explain.txt").write_text(text)
print(text)
