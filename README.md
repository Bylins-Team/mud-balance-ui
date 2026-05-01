# mud-balance-ui

Веб-интерфейс для запуска и просмотра прогонов автономного симулятора
баланса (`mud-sim` из [bylins/mud](https://github.com/Bylins-Team/mud),
issue #2967).

## Что это

`mud-sim` — headless-сборка движка Bylins MUD: грузит мир, гоняет
PC/мобов в боевом цикле и пишет JSONL с боевыми событиями. Этот
репозиторий даёт удобный веб-UI поверх него:

- **Список прогонов** с короткой сводкой (классы, dpr, hit rate, статус,
  бейджи `в очереди` / `⏳ выполняется` / `упал`).
- **Форма создания** прогона — structured fields (классы из dropdown,
  моб/спелл/предмет через autocomplete с поиском), live-preview YAML
  с подсветкой синтаксиса (highlight.js), reverse parse «YAML → форма».
- **Очередь** прогонов: POST мгновенно возвращает 303 на `/runs/<id>`,
  страница показывает спиннер + прогресс-бар `N / Total раундов · ETA`.
  ThreadPoolExecutor с 2 worker'ами.
- **Viewer прогона** — round-based timeline с кнопками `⏮ ‹ ▶ › ⏭`,
  Space/←/→/Home/End hotkeys, autoscroll combat-log при play.
- **State-панель**: identity-карточка (PC/моб + class/level/vnum/имя),
  атрибуты с разницей `25→26 (+1)`, бонусы (HP/AC/hitroll/damroll/
  spellpower/physdam/морал/инициатива), экипировка с применёнными
  applies/affects под каждым предметом, аффекты chip-list (отдельно
  временные с длительностью и постоянные от шмота).
- **Combat log** — damage/miss/affect_added/removed, фильтр по раунду.
- **Screen output** — то, что персонаж увидел бы в telnet-клиенте,
  захвачено через headless `DescriptorData`. `&`-цветовые коды → CSS-spans.
- **Аналитика** — один объединённый chart.js: stacked area по источнику
  (melee/spell/pets) на левой Y + кумулятивный итог на правой.
- **«Повторить»** на странице прогона — открывает форму с этим YAML
  для быстрой правки и нового запуска. Последний submitted YAML также
  сохраняется в localStorage.

К продакшен-серверу MUD это приложение отношения не имеет — отдельная
аналитическая утилита для имма / имплементера.

## Архитектура

```
┌──────────────────────────────────────────────────────────────┐
│ mud (git submodule, ветка claude/vibrant-raman-695d14)        │
│  src/simulator/* — headless engine, scenario_runner,           │
│  observability/event_sink list, screen_output capture, ...    │
└──────────────────────┬───────────────────────────────────────┘
                       │ собирается на стадии build Dockerfile в /opt/mud-sim
┌──────────────────────▼───────────────────────────────────────┐
│ Flask UI (этот репо, app/)                                    │
│  jobqueue.py — ThreadPool, status queued→running→ok/failed   │
│  routes.py — /runs, /runs/<id>, /api/{state,log,screen,      │
│              analytics,spells,mobs,objects}                   │
│  world.py — read-only sканирует /opt/small или /data/world,  │
│              отдаёт spells/mobs/objects autocomplete         │
└──────────────────────┬───────────────────────────────────────┘
                       │ HTTP, gateway nginx + basic-auth
                ┌──────▼──────┐
                │   Browser   │ HTMX + chart.js + highlight.js + js-yaml
                └─────────────┘
```

## Запуск

```bash
git clone --recurse-submodules git@github.com:Bylins-Team/mud-balance-ui.git
cd mud-balance-ui
docker compose up --build -d
# UI на http://localhost:5001/  (или за твоим nginx-gateway)
```

Default-мир — встроенный `small/` (готовится в build-stage). Для
production-снимка положи `~/worlds/world.tgz`, распакуй в `world/` и
сконвертируй в YAML — compose уже монтирует его как `/data/world`:

```bash
mkdir world && cd world
tar -xzf ~/worlds/world.tgz --strip-components=1
python3 ../mud/tools/converter/convert_to_yaml.py -i . -o . -f yaml
cd .. && docker compose restart
```

## Конфигурация

| env var | default | назначение |
|---|---|---|
| `MUD_SIM_BIN` | `/opt/mud-sim` | путь к собранному `mud-sim` |
| `MUD_SIM_WORLD_DIR` | `/data/world` | каталог мира (RW — engine пишет etc/players) |
| `RUNS_DIR` | `/data/runs` | где хранить прогоны |
| `MUD_SIM_TIMEOUT_S` | `600` | лимит на один прогон |
| `MUD_SIM_QUEUE_WORKERS` | `2` | сколько прогонов параллельно |

## Структура прогона

```
runs/<ulid>/
├── scenario.yaml      # KOI8-R, как для mud-sim
├── events.jsonl       # сырой output симулятора
├── meta.json          # {created_at, status, scenario_yaml, dpr,
│                       #  hit_rate_pct, rounds, round_ts, roles,
│                       #  target_rounds, error, ...}
├── stderr.log         # на случай падения mud-sim
├── syslog             # engine syslog (для отладки)
└── log/               # engine log dir (errlog, depot, и т.д.)
```

## Развёртывание за gateway

В этой инсталляции UI закрыт за nginx + basic-auth (см.
`~/repos/gateway/sites/mud-balance.conf`):

```
mud-balance.kvirund.dev
├── basic-auth: ~/repos/gateway/auth/mud-balance.htpasswd
└── proxy_pass http://mud-balance-ui:5001
```

Контейнер подключён к external network `gateway`. nginx достаёт его по
docker DNS-имени `mud-balance-ui:5001`.

## Эндпоинты

| метод + путь | назначение |
|---|---|
| `GET /` | редирект на `/runs` |
| `GET /runs` | HTML список прогонов |
| `GET /runs/new[?from=<id>]` | форма создания, опц. с pre-fill |
| `POST /runs` | enqueue + redirect 303 на `/runs/<id>` |
| `GET /runs/<id>` | viewer (или pending-страница если `running`/`queued`) |
| `GET /runs/<id>/status` | JSON status + progress (для polling) |
| `GET /runs/<id>/api/state?round=N&role=R` | HTML-фрагмент state-panel |
| `GET /runs/<id>/api/log?round=N` | HTML-фрагмент combat log |
| `GET /runs/<id>/api/screen?round=N&role=R` | HTML-фрагмент screen output |
| `GET /runs/<id>/api/analytics` | JSON для chart.js |
| `GET /runs/<id>/events.jsonl` | сырой JSONL для downloads |
| `POST /runs/<id>/delete` | удалить прогон |
| `GET /api/spells?q=` | autocomplete заклинаний |
| `GET /api/mobs?q=` | autocomplete мобов (по имени или vnum) |
| `GET /api/objects?slot=&q=` | autocomplete предметов по слоту |

## Out of scope (пока)

- Auth внутри приложения — закрывается nginx-gateway снаружи.
- Сравнение N прогонов на одной странице — пока только две вкладки
  браузера.
- Экспорт прогона в shareable JSON — backlog.
- Параметризованные прогоны: «прогнать матрицу wisdom 10..80» одной
  формой → серия + сводный график — backlog.
- `screen_output` для broadcast'ов (`SendMsgToOutdoor`/`SendMsgToGods`) —
  для арены с двумя участниками неактуально.

## TODO

- [ ] **Скриншоты в README** — добавить картинки UI: list, runs_new
      форма с YAML preview, viewer state-panel + combat log + screen,
      analytics chart.
