# mud-balance-ui

Веб-интерфейс для запуска и просмотра прогонов автономного симулятора
баланса (`mud-sim` из [bylins/mud](https://github.com/Bylins-Team/mud),
issue #2967).

## Что это

Симулятор `mud-sim` — это headless-сборка движка Bylins MUD: грузит мир,
гоняет PC/мобов в боевом цикле и пишет JSONL с боевыми событиями
(damage, miss, affect_added/removed, char_state, round, screen_output).
Данных хватает, чтобы реконструировать состояние любого участника на
любой момент боя.

CLI-обвязка вокруг `mud-sim` (подготовь YAML, запусти, читай JSONL
глазами) неудобна. Этот репозиторий даёт веб-UI:

- список прогонов с короткой сводкой (классы, dpr, hit rate, статус);
- форма создания нового прогона (textarea YAML или structured fields);
- viewer прогона с timeline-слайдером: на любой момент времени видно
  HP/аффекты/позицию каждого участника, накопленный combat log и
  «телнет-вывод» каждого PC (как если бы он был подключён клиентом);
- deep links на конкретный прогон и момент.

К продакшен-серверу MUD это приложение отношения не имеет — просто
аналитическая утилита для имма / имплементера.

## Архитектура

```
[Browser, HTMX]
      |
      v
[Flask UI] --spawn--> [mud-sim subprocess] --writes--> [runs/<id>/events.jsonl]
      ^                                                       |
      `------------------------ reads --------------------------'
```

- `mud-sim` подключается через git submodule `mud/` (`bylins/mud`),
  собирается из исходников в Docker stage `build`.
- `runs/` — каталог-volume, один подкаталог на прогон:
  `scenario.yaml` + `events.jsonl` + `meta.json` + `stderr.log`.
- В UI: server-rendered HTML (Jinja2) + HTMX для частичных обновлений.
  Никакого React/build-step.
- Auth нет — рассчитано на nginx (basic auth / VPN) снаружи.

## Запуск

### Через docker-compose (рекомендуется)

```bash
git clone --recurse-submodules git@github.com:Bylins-Team/mud-balance-ui.git
cd mud-balance-ui
docker compose up --build
# открыть http://localhost:5001/
```

### Локально (для разработки UI)

Нужен предсобранный `mud-sim` и подготовленный мир. См.
`mud/src/simulator/README.md` для сборки.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
export MUD_SIM_BIN=/path/to/build_yaml/mud-sim
export MUD_SIM_WORLD_DIR=/path/to/build_yaml/small
export RUNS_DIR=$PWD/runs
flask --app app run --host 127.0.0.1 --port 5001
```

## Конфигурация

Через переменные окружения:

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `MUD_SIM_BIN` | `/opt/mud-sim` | путь к собранному `mud-sim` |
| `MUD_SIM_WORLD_DIR` | `/data/world` (fallback `/opt/small`) | каталог мира |
| `RUNS_DIR` | `/data/runs` | где хранить прогоны |
| `MUD_SIM_TIMEOUT_S` | `120` | лимит на один прогон, сек |

## Структура прогона

```
runs/<ulid>/
├── scenario.yaml      # KOI8-R, как для mud-sim
├── events.jsonl       # сырой output симулятора
├── meta.json          # {created_at, scenario_summary, dpr, hit_rate, classes, status}
└── stderr.log         # на случай падения mud-sim
```

## Out of scope (пока)

- Auth (закрывается nginx'ом снаружи).
- Параллельные прогоны (POST блокирует обработчик до завершения; для MVP ok).
- Сравнение прогонов рядом (две вкладки браузера).
- Экспорт прогона в shareable JSON.
- Параметризованные матричные прогоны (wisdom 10..80 одной формой).
