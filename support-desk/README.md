# Служба поддержки: заявки, статусы, срок реакции

Учебный веб-сервис (вариант 2 курсовой работы по дисциплине «Оптимизация клиент-серверных приложений»,
префикс `anzhelika_merdenova`). Принимает заявки с категорией, ведёт их по статусам «новая → в работе → закрыта»,
хранит переписку комментариями, считает срок реакции (от создания заявки до первого ответа сотрудника) и сравнивает
его с нормативом категории. Сводка показывает число открытых заявок по категориям и долю нарушивших норматив.

## Требования

- Python 3.12
- PostgreSQL 16 (подойдёт контейнер из `docker-compose.yml`; нужен Docker Compose v2)
- Версии библиотек — в `requirements.txt` (FastAPI 0.142.2, SQLAlchemy 2.1.3, psycopg 3.3.6, uvicorn 0.54.0)

## Установка и запуск

    git clone <адрес репозитория> && cd support-desk
    cp .env.example .env
    python3 -m venv .venv && . .venv/bin/activate
    pip install -r requirements.txt
    docker compose up -d db
    set -a && . ./.env && set +a
    python -m app.initdb
    python seed.py --size small
    python -m uvicorn app.main:app --port $APP_PORT

Рабочий объём данных (пересоздаёт схему целиком; повторный запуск даёт ту же базу):

    python seed.py --size work

Если PostgreSQL уже установлен без Docker: создайте пользователя и базу из `.env` вручную, `docker compose` пропустите.

## Переменные окружения

| Переменная | Назначение | Пример |
|---|---|---|
| PREFIX | личный префикс работы | anzhelika_merdenova |
| DATABASE_URL | подключение к БД (драйвер psycopg 3) | postgresql+psycopg://app:changeme@localhost:5432/anzhelika_merdenova |
| DB_SCHEMA | имя схемы (содержит префикс) | anzhelika_merdenova |
| APP_PORT | порт сервиса | 8000 |
| SECRET_KEY | ключ подписи сессионной куки | changeme |
| POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB / POSTGRES_PORT | параметры контейнера БД | app / changeme / anzhelika_merdenova / 5432 |

## Проверка работоспособности

Интерфейс: http://localhost:8000. Учётные записи (пароль у всех `demo`): сотрудник `agent1`
(видит все заявки, меняет статусы, открывает сводку), заказчик `customer1` (видит только свои заявки).
Экраны: список заявок с фильтром и страницами, карточка заявки с комментариями, сводка.

## Тесты

    python -m pytest -q

Тесты работают в отдельной схеме `anzhelika_merdenova_test` той же базы (создаётся и удаляется самими тестами);
рабочие данные не затрагиваются. Нужна запущенная БД и переменные из `.env`.

## Замеры

    python load/measure.py --label small    # после seed.py --size small
    python load/measure.py --label work     # после seed.py --size work

Результаты — в `results/<label>.json` и таблицей в консоли.

Проверка корректности (сразу после `seed.py`, до замеров; сервис должен быть запущен):

    python load/verify.py

Скрипт печатает контрольные суммы таблиц заявок и комментариев, сверяет сводку с независимым SQL-запросом
и проверяет постраничный вывод и фильтры по статусу. Время в базе и число запросов сервис отдаёт заголовками
`X-DB-Time-ms` и `X-DB-Queries` (полное время обработчика — `X-Response-Time-ms`). Время в базе — это сумма
интервалов вокруг вызовов курсора и фиксации транзакции; в «код» попадает всё остальное.

## Ограничения первого задания

Индексы только на первичные и внешние ключи (`users.login` уникален — для входа); кеширования, очередей, фоновой
обработки и хранения файлов вне БД нет. Остальные индексы (по статусу, дате и т. п.) — задача третьего задания.

## Программный интерфейс

Авторизация: `POST /api/login` ставит куку `session`; без неё все операции, кроме входа, отвечают 401.
Роли: `agent` — сотрудник поддержки, `customer` — заказчик (видит и комментирует только свои заявки; чужие — 404).
Статусы: `new`, `in_progress`, `closed`; допустимы только переходы `new → in_progress → closed`.

| Метод и путь | Параметры | Ответ | Ошибки |
|---|---|---|---|
| POST /api/login | login, password (JSON) | 200 `{id, login, name, role}` + кука | 401, 422 |
| POST /api/logout | — | 204 | — |
| GET /api/me | — | 200 `{id, login, name, role}` | 401 |
| GET /api/categories | — | 200 `[{id, name, reaction_norm_minutes}]` | 401 |
| GET /api/tickets | page ≥ 1 (1), size 1–100 (20), status, category_id | 200 `{items:[{id, title, status, category{id,name}, author{id,name}, created_at}], total, page, size}` | 401, 422 |
| GET /api/tickets/{id} | — | 200 заявка с `body`, `updated_at`, `comments[]`, `reaction_minutes`, `reaction_norm_minutes`, `norm_violated` | 401, 404 |
| POST /api/tickets | title, body, category_id (JSON) | 201 созданная заявка (статус `new`) | 401, 404 (нет категории), 422 |
| PATCH /api/tickets/{id}/status | status (JSON) | 200 `{id, status}` | 401, 403 (не сотрудник), 404, 409 (недопустимый переход), 422 |
| POST /api/tickets/{id}/comments | body (JSON) | 201 комментарий `{id, author, body, created_at}` | 401, 404, 409 (заявка закрыта), 422 |
| GET /api/summary | — (только сотрудник) | 200 `{open_total, violated_total, violated_share, by_category:[{category_id, name, reaction_norm_minutes, open, violated, violated_share}]}` | 401, 403 |

Правила срока реакции: ответом считается первый комментарий сотрудника (комментарии заказчика не считаются);
если ответа нет, срок идёт до текущего момента. Норматив нарушен, если срок больше `reaction_norm_minutes` категории.
Сводка считает только открытые заявки (`new`, `in_progress`).

## Нагрузочное испытание (второе задание)

Каталог `load/`: `k6/test.js` — тест на k6, `run_steps.py` — запуск по ступеням со снятием ресурсов, `profile.py` — профиль под
нагрузкой, `race_check.py` — проверка одновременного доступа, `explain.py` — планы запросов.

### Что нужно установить

- k6 (https://grafana.com/docs/k6/latest/set-up/install-k6/) или готовый бинарник:

      curl -sL https://github.com/grafana/k6/releases/download/v1.4.0/k6-v1.4.0-linux-amd64.tar.gz | tar xz
      sudo mv k6-v1.4.0-linux-amd64/k6 /usr/local/bin/

- py-spy: `pip install py-spy` (есть в `requirements.txt`); для снятия профиля нужны права, скрипт вызывает его через `sudo`.

### Модель нагрузки

Открытая (constant-arrival-rate): запросы приходят с заданной частотой независимо от ответов сервиса. Два сценария:
`mixed` — операции по весам профиля (см. таблицу весов в отчёте и массив `PROFILE` в `test.js`), `heavy` — только сводка.
Порог записан в тесте: p95 < 500 мс и доля ошибок < 1 %. Ответ 409 (отказ по правилам: переход статуса уже выполнен,
заявка закрыта) — ожидаемый, ошибкой не считается. Вход выполняется один раз в `setup()` и в результаты не попадает.

Прямой запуск одной ступени (параметры — переменные окружения `SCENARIO`, `RATE`, `DURATION`):

    k6 run -e SCENARIO=mixed -e RATE=4 -e DURATION=30s load/k6/test.js

### Порядок испытания

Сервис запущен одним процессом (`python -m uvicorn app.main:app --port $APP_PORT`), переменные из `.env` загружены.
Перед каждым испытанием база наполняется заново (`--reseed` делает это сам), сервис перезапускается.

    # 1. смешанный профиль: ступени по 30 с, частота удваивается, пока порог не нарушен
    python load/run_steps.py --scenario mixed --rates 1,2,4,8,16,32,64 --duration 30s --reseed

    # 2. тяжёлая операция (сводка): частоты ниже 1 запроса в секунду, т. к. одно выполнение длится секунды
    python load/run_steps.py --scenario heavy --rates 0.02,0.04,0.08,0.16 --duration 120s --reseed --all

    # 3. профиль под нагрузкой на частоте предела тяжёлой операции (столбец «Фактическая» таблицы шага 2)
    python load/profile.py --rate 0.05

    # 4. планы запросов
    python load/explain.py

    # 5. проверка одновременного доступа (после шага 3 сервис должен работать)
    python load/race_check.py

Опыт с настройкой (дополнительное задание): запустить сервис с `--workers 4`, повторить шаг 1 с `--tag w4`,
вернуть запуск одним процессом.

Результаты складываются в `results/`: `steps_*.json` (ступени), `resources_*.csv` (ресурсы раз в секунду),
`profile.raw`, `profile.svg`, `profile_top.json` (профиль), `explain.txt`, `race.json`, `k6/` (сводки k6).
