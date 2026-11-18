// Нагрузочный тест сервиса «Служба поддержки» (вариант 2).
//
// Запуск (параметры — переменными окружения):
//   k6 run -e SCENARIO=mixed -e RATE=4 -e DURATION=30s load/k6/test.js
//   k6 run -e SCENARIO=heavy -e RATE=1 -e DURATION=60s load/k6/test.js
//
//   SCENARIO  mixed — операции по весам профиля; heavy — только самая тяжёлая операция (сводка)
//   RATE      частота, запросов в секунду (допускается дробная, например 0.05)
//   DURATION  длительность ступени
//   BASE_URL  адрес сервиса (по умолчанию http://localhost:8000)
//   MAX_VUS   предел виртуальных пользователей (по умолчанию 500)
//   OFFSET    сдвиг в списке заявок для перевода статусов (разные ступени берут разные заявки)
//
// Модель нагрузки открытая (constant-arrival-rate): запросы приходят с заданной частотой независимо от того,
// успел ли сервис ответить на предыдущие, — как в лабораторной работе № 3.
import http from 'k6/http';
import exec from 'k6/execution';
import { Counter } from 'k6/metrics';

const BASE = __ENV.BASE_URL || 'http://localhost:8000';
const SCENARIO = __ENV.SCENARIO || 'mixed';
const RATE = parseFloat(__ENV.RATE || '2');
const DURATION = __ENV.DURATION || '30s';
const MAX_VUS = parseInt(__ENV.MAX_VUS || '500');
const OFFSET = parseInt(__ENV.OFFSET || '0');

// Частота меньше 1 запроса в секунду задаётся как «n запросов за 100 секунд».
const arrival = RATE >= 1 ? { rate: Math.round(RATE), timeUnit: '1s' } : { rate: Math.max(1, Math.round(RATE * 100)), timeUnit: '100s' };

export const options = {
  scenarios: {
    [SCENARIO]: {
      executor: 'constant-arrival-rate',
      exec: SCENARIO,
      duration: DURATION,
      preAllocatedVUs: Math.min(50, MAX_VUS),
      maxVUs: MAX_VUS,
      // тяжёлая операция: запросы, не завершённые к концу ступени, обрываются, чтобы фактическая частота считалась по ступени
      gracefulStop: SCENARIO === 'heavy' ? '0s' : '30s',
      ...arrival,
    },
  },
  // Порог для всех один: p95 < 500 мс и доля ошибок < 1 %. Вход в setup() в результаты не попадает:
  // пороги считаются только по запросам сценария.
  thresholds: {
    [`http_req_duration{scenario:${SCENARIO}}`]: ['p(95)<500'],
    [`http_req_failed{scenario:${SCENARIO}}`]: ['rate<0.01'],
  },
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'max'],
};

// Ожидаемые ответы: 2xx и 409. 409 — отказ по правилам варианта (переход статуса уже выполнен другим запросом,
// комментарий в закрытую заявку); ошибкой он не считается. Остальное (500, 401, 404, обрыв соединения) — ошибка.
http.setResponseCallback(http.expectedStatuses({ min: 200, max: 299 }, 409));
const rejections = new Counter('rule_rejections');
// Неожиданные ответы (не 2xx и не 409) считаются по операциям: метрики err_<операция>, первые записи — в журнал k6.
const KEYS = ['list_first', 'list_new', 'list_deep', 'card', 'comment', 'status', 'create', 'categories', 'me', 'summary'];
const errors = {};
KEYS.forEach((k) => { errors[k] = new Counter(`err_${k}`); });
let logged = 0;

export function setup() {
  const headers = { 'Content-Type': 'application/json' };
  const login = http.post(`${BASE}/api/login`, JSON.stringify({ login: 'agent1', password: 'demo' }), { headers, tags: { name: 'setup' } });
  if (login.status !== 200) throw new Error(`вход не удался: ${login.status}`);
  const cookie = `session=${login.cookies.session[0].value}`;
  const get = (path) => http.get(`${BASE}${path}`, { headers: { Cookie: cookie }, tags: { name: 'setup' } }).json();

  // Пулы заявок для операций записи: «новые» — для перевода в работу, «в работе» — для комментариев.
  const ids = (status, pages) => {
    const out = [];
    for (let p = 1; p <= pages; p++) get(`/api/tickets?status=${status}&size=100&page=${p}`).items.forEach((t) => out.push(t.id));
    return out;
  };
  return {
    cookie,
    total: get('/api/tickets?size=1').total,
    newIds: ids('new', 20),
    workIds: ids('in_progress', 5),
  };
}

const rnd = (n) => 1 + Math.floor(Math.random() * n);
const pick = (arr) => arr[Math.floor(Math.random() * arr.length)];

function call(d, method, path, name, body, key) {
  const params = { headers: { Cookie: d.cookie, 'Content-Type': 'application/json' }, tags: { name } };
  const res = http.request(method, `${BASE}${path}`, body ? JSON.stringify(body) : null, params);
  if (res.status === 409) rejections.add(1, { name });
  else if (res.status < 200 || res.status > 299) {
    errors[key].add(1);
    if (logged++ < 20) console.error(`ERR ${name} ${res.status} ${String(res.body).slice(0, 100)}`);
  }
  return res;
}

// Профиль нагрузки: вес — доля операции в потоке запросов, сумма весов 100 (обоснование — в отчёте, таблица 2).
const PROFILE = [
  [25, (d) => call(d, 'GET', '/api/tickets?page=1&size=20', 'GET /api/tickets (стр. 1)', null, 'list_first')],
  [15, (d) => call(d, 'GET', '/api/tickets?page=1&size=20&status=new', 'GET /api/tickets (status=new)', null, 'list_new')],
  [5, (d) => call(d, 'GET', '/api/tickets?page=10&size=20', 'GET /api/tickets (стр. 10)', null, 'list_deep')],
  [24, (d) => call(d, 'GET', `/api/tickets/${rnd(d.total)}`, 'GET /api/tickets/{id}', null, 'card')],
  [12, (d) => call(d, 'POST', `/api/tickets/${pick(d.workIds)}/comments`, 'POST /api/tickets/{id}/comments', { body: 'Принято в работу' }, 'comment')],
  [6, (d) => {
    const id = d.newIds[(OFFSET + exec.scenario.iterationInTest) % d.newIds.length];
    return call(d, 'PATCH', `/api/tickets/${id}/status`, 'PATCH /api/tickets/{id}/status', { status: 'in_progress' }, 'status');
  }],
  [5, (d) => call(d, 'POST', '/api/tickets', 'POST /api/tickets', { title: 'Нагрузка', body: 'нагрузочная заявка', category_id: rnd(10) }, 'create')],
  [3, (d) => call(d, 'GET', '/api/categories', 'GET /api/categories', null, 'categories')],
  [4, (d) => call(d, 'GET', '/api/me', 'GET /api/me', null, 'me')],
  [1, (d) => call(d, 'GET', '/api/summary', 'GET /api/summary', null, 'summary')],
];
const CUMULATIVE = PROFILE.reduce((acc, [w, fn]) => { acc.push([(acc.length ? acc[acc.length - 1][0] : 0) + w, fn]); return acc; }, []);

export function mixed(d) {
  const x = Math.random() * 100;
  CUMULATIVE.find(([edge]) => x < edge)[1](d);
}

// Самая тяжёлая операция по замерам первого задания — сводка.
export function heavy(d) {
  call(d, 'GET', '/api/summary', 'GET /api/summary', null, 'summary');
}
