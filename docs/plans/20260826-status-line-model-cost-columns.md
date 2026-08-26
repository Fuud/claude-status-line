# Status line: model + cost columns

## Overview

Добавляем в таблицу статуса две колонки: `model` (id модели из jsonl) и
`cost` (потраченная сумма по ценам из локального файла). Колонки выводятся
для строк `sum:`, `main:` и агентов; `start:` — референсная строка, без
колонок. Файл цен `prices.json` приватный (gitignore): нет файла — обеих
колонок нет; модели нет в файле — `n/a`; модели нет вовсе — пустая ячейка.

Заодно устраняем семантическую несостыковку: строки агентов переходят с
«usage последнего API-вызова» на кумулятивные суммы по всем событиям
(согласовано с пользователем как видимое изменение поведения), `sum:`
становится честным тоталом сессии по каждой модели отдельно.

Ключевые факты из разведки:

- В jsonl у каждого assistant-события есть `message.model`; реальный
  ids: `glm-5.3`, `kimi-k3`, `MiniMax-M3`, `<synthetic>`. Суффикса `[1m]`
  (display_name из payload) в jsonl нет.
- `<synthetic>`-события всегда несут нулевой usage — на рендере
  отсекаются правилом «пропускать per-model строки с нулевыми токенами».
- Следов провайдера в jsonl нет. Но хук — дочерний процесс claude и видит
  `ANTHROPIC_BASE_URL` (проверено: `https://api.z.ai/api/anthropic`).
  Функции запуска из `.bashrc` (`zai-glm-5.2-1m`, `claude-kimi-k3`, …)
  различаются base URL — по хосту и различаем провайдеров.
- Оркестратор (`_main_unsafe`, `status_line.py:1419`) уже собирает всё
  нужное; main-кэш апгрейдился через field-presence уже дважды —
  используем тот же паттерн.

## Context (from discovery)

- Файлы:
  - `status_line.py` — `format_tokens` (~31), `_scan_main_jsonl` (~439),
    `compute_main_cum` (~612), `compute_agent_snapshot` (~740),
    `_read_last_event` (~383), `render_output` (~1087), `_col_width`
    (~1060), `_AGENT_CACHE_FIELDS` (~1269), `_main_unsafe` (~1419).
  - `.gitignore` — добавить `prices.json`.
  - `README.md` — пример вывода + новая секция про prices.json.
  - `prices.example.json` — новый, коммитится как документация формата.
- Тесты: `tests/test_render_output.py`, `tests/test_compute_main_cum.py`,
  `tests/test_compute_agent_snapshot.py`, `tests/test_main_integration.py`;
  fixture `tests/fixtures/real_session/f5044e4f…` — main jsonl содержит
  `kimi-k3` + `glm-5.3` + `<synthetic>` (готовый мульти-модельный кейс).
- Паттерны проекта: кэш-инвалидация через наличие новых полей
  (`per_model` для main, `models` для агентов); atomic write
  (`.tmp` → `os.replace`); «хук не имеет права упасть» — `main()` глотает
  всё; stdlib-only.

## Development Approach

- **testing approach**: TDD (tests first) — сначала падающие тесты, потом
  реализация до зелёного.
- complete each task fully before moving to the next
- make small, focused changes
- **CRITICAL: every task MUST include new/updated tests**
- **CRITICAL: all tests must pass before starting next task** — no exceptions
- **CRITICAL: update this plan file when scope changes during implementation**
- maintain backward compatibility: `prices.json` отсутствует → вывод без
  новых колонок (раскладка как сегодня; числовые значения агентов меняются
  на кумулятивные — согласованное изменение семантики)

## Testing Strategy

- **unit tests**: pytest, `python3 -m pytest tests/ -v`, на каждую задачу.
- **e2e tests**: интеграционные в `tests/test_main_integration.py`
  (subprocess + fake-HOME: `prices.json` пишется в fake-HOME, env
  `ANTHROPIC_BASE_URL` выставляется/снимается per-test через `_run_main`).
- Препрода/инвентаря у проекта нет (локальный хук Claude Code) — критерий
  проверки: зелёная suite + живая сессия (см. Post-Completion).

## Progress Tracking

- mark completed items with `[x]` immediately when done
- add newly discovered tasks with ➕ prefix
- document issues/blockers with ⚠️ prefix
- update plan if implementation deviates from original scope

## Solution Overview

Чистые функции цен (`provider_host`, `load_prices`, `price_for`,
`compute_cost`, `format_cost`) → per-model накопление в существующих
сканах (`_scan_main_jsonl` — дополнение прохода; `compute_agent_snapshot`
— перестройка с reverse-read на один forward-scan) → generic
`render_table` с выравниванием на колонку → `render_output` собирает
строки-группы (метка только на первой строке группы) → оркестратор
врисовывает цены. Кэши апгрейдятся field-presence-проверками.

## Technical Details

### prices.json

```json
[
  {
    "model": "glm-5.3@api.z.ai",
    "in": 6.9,
    "out": 24,
    "cache": 1.7,
    "per": 10000,
    "units": "credits"
  },
  {
    "model": "kimi-k3",
    "in": 3,
    "out": 15,
    "cache": 0.3,
    "per": 1000000,
    "units": "$"
  }
]
```

- Путь: `_PRICES_PATH = Path.home() / ".claude" / "status_line" /
"prices.json"`. В продакшене совпадает с каталогом модуля (он живёт там
  же), но привязка к HOME делает subprocess-интеграционные тесты
  герметичными: fake-HOME харнесса изолирует их от реального файла
  пользователя (module-относительный путь этого не даёт — monkeypatch не
  проникает в child-process, а реальный `prices.json` навсегда ломал бы
  тесты «колонок нет»). Юнит-тесты monkeypatch'ят саму константу.
  Читается при каждом вызове хука (файл крошечный).
- `load_prices()` → `{key: {"in": float, "out": float, "cache": float,
"per": number, "units": str}}` или `None`. `None` когда: файла нет,
  битый JSON, не список / элементы не dict / нет `model`-строки / `per`
  отсутствует, не число или ≤ 0 / цены не числа. Отсутствующие
  `in`/`out`/`cache` → 0; отсутствующий `units` → `""`. Дубли ключей —
  последний выигрывает. Молча, без stderr.
- `provider_host()` → `urlparse(os.environ.get("ANTHROPIC_BASE_URL", "")).hostname or ""`;
  любые ошибки → `""`.
- `price_for(model, prices, host)`: при `host != ""` сначала
  `f"{model}@{host}"`, потом `model`, потом `None`.

### Формула и формат

- `cost = (in·p_in + out·p_out + cached·p_cache) / per`.
  `cache_creation` не считается (нигде не отображается).
- `format_cost(value, units)`: `≥ 1e6` → `X.XM`; `≥ 1000` → `X.Xk`;
  `0.1 ≤ v < 1000` → 1 знак, хвостовое `.0` отбрасывается (`402`);
  `< 0.1` → 2 знака (`0.04`). Units: первый символ не
  `isalnum()` → префикс (`$8.1`), иначе суффикс (`402 credits`); пустые
  units → только число.
- Ячейка cost: цена найдена → число; модель известна, цены нет → `n/a`;
  per-model запись с нулевыми токенами пропускается целиком. После
  отсечения нулевых строк группа (main/sum/агент), оставшаяся без строк,
  рендерится ОДНОЙ нулевой строкой с ПУСТОЙ ячейкой model — инвариант
  «агенты никогда не пропускаются» (в fixture 4 агента с одними
  `<synthetic>`-событиями: per-model dict не пуст, но все записи нулевые).

### Данные

- `_scan_main_jsonl` в тот же проход копит
  `per_model: {model_id: {"in","out","cached"}}` (для assistant-событий с
  usage; `model = str(msg.get("model") or "")`; нулевые записи —
  включая `<synthetic>` — попадают, отсекаются на рендере). Проброс через
  `compute_main_cum` в результат и main-кэш; cache-hit требует наличия
  `per_model`.
- `compute_agent_snapshot` — один forward-scan: кумулятивные
  `tokens_in/out/cached`, `models` (тот же формат per-model dict),
  последний assistant-uuid, последние события для `detect_status`
  (переопределение «0 assistant-событий → err/stop» сохраняется).
  `_read_last_event` после этого не нужен — проверить и удалить.
- [trade-off] forward-scan лишает агентный путь early-exit reverse-чтения:
  на cache-hit теперь парсится весь файл агента каждый тик. I/O не меняется
  (`_read_last_event` и так делал `readlines()` всего файла) — растёт только
  json.loads-работа на файлах в десятки KB. Согласуется с
  задокументированным trade-off `compute_main_cum`; зафиксировать в
  `[decision]`-комментарии у новой реализации.
- Кэш агентов: `_AGENT_CACHE_FIELDS` += `"models"`; cache-hit требует
  `models` (старые записи → один rescan → перезапись).

### Рендер

- `render_table(columns, rows)` → список строк БЕЗ `| `-префикса.
  `columns = [{"label", "align": "left"|"right", "floor": int}]`,
  `rows` — списки готовых (отформатированных) ячеек. Ширина колонки =
  `max(floor, len(label), самая длинная ячейка)` — обобщение `_col_width`;
  сам `_col_width` СОХРАНЯЕМ как helper ширины токен-колонок — его
  импортируют тесты (`tests/test_render_output.py`).
- `render_output(header, start_in, start_out, start_cached, main_models,
agents, prices=None, host="")` — плоская тройка main заменяется на
  `main_models` dict (тоталы строки main = сумма её per-model записей).
  Существующие тесты `test_render_output.py` переводятся на новую
  сигнатуру; требование регрессии — ВЫВОД при `prices=None` раскладкой
  совпадает с текущим (те же колонки/отступы), значения агентов
  кумулятивные.
- Раскладка: `model` между description и `in` (left), `cost` после
  `cached` (right); label/description — left; in/out/cached — right.
  Таблица-заголовок получает метки числовых и model/cost колонок; у колонки
  description метка ПУСТАЯ — иначе раскладка при `prices=None` разъедется с
  сегодняшней.
- Группы: `sum:` (только при агентах; per-model = main_models + все
  agents' `models`, по каждой модели отдельно, кросс-модельных сумм нет),
  `main:`, каждая группа агента (иконка+описание на первой строке).
  Метка/иконка/описание — только на первой строке группы. Порядок моделей
  — первое появление в скане. Агент без событий / с пустым per_model / у
  которого все per-model записи отсечены как нулевые → одна строка с
  нулями и ПУСТОЙ ячейкой model (агенты никогда не пропускаются).
  `start:` — без model/cost.
- `| `-префикс (`_TABLE_ROW_PREFIX`) применяется ко всем строкам кроме
  header, как сейчас.

## What Goes Where

- **Implementation Steps** (`[ ]`): код, тесты, `.gitignore`,
  `prices.example.json`, README.
- **Post-Completion** (без чекбоксов): живая проверка в реальной сессии,
  создание приватного `prices.json`, раскатка (коммит = живой хук).

## Implementation Steps

### Task 1: Функции цен (load_prices, provider_host, price_for, compute_cost, format_cost)

**Files:**

- Modify: `status_line.py`
- Create: `tests/test_prices.py`

- [x] красные тесты: `provider_host` (env есть / нет / битый URL / не-str), `load_prices` (нет файла / битый JSON / не список / нет model / per≤0 / не-числовые цены / partial-поля / валидный / дубли ключей), `price_for` (цепочка `model@host` → `model` → `None`; host=""), `compute_cost` (точные значения, нулевые компоненты), `format_cost` (M/k/1 знак без хвостового .0/2 знака <0.1; префикс `$` / суффикс `credits` / пустые units)
- [x] `_PRICES_PATH` = `Path.home()/.claude/status_line/prices.json` (см. Technical Details — привязка к HOME, не к `__file__`); `import urllib.parse`
- [x] реализовать пять чистых функций по Technical Details
- [x] зелёные тесты, прогон всей suite

### Task 2: per-model накопление в _scan_main_jsonl + bump main-кэша

**Files:**

- Modify: `status_line.py`
- Modify: `tests/test_compute_main_cum.py`

- [x] красные тесты: одна модель; смена модели посреди jsonl (две записи per_model); только `<synthetic>` (запись с нулями остаётся в dict); main-кэш старой формы (без `per_model`) с совпадающим `last_uuid` → miss и перезапись
- [x] `_scan_main_jsonl` копит `per_model` в том же проходе
- [x] `compute_main_cum` пробрасывает `per_model` в результат и кэш; cache-hit требует наличия поля; `"per_model": {}` добавляется в `_EMPTY_MAIN_RESULT` (race-путь «jsonl исчез между проверками» не должен ронять оркестратор)
- [x] зелёные тесты, прогон всей suite

### Task 3: compute_agent_snapshot → один forward-scan, кумулятив + models

**Files:**

- Modify: `status_line.py`
- Modify: `tests/test_compute_agent_snapshot.py`
- Create: `tests/fixtures/` — маленькие handcrafted jsonl/meta при необходимости

- [x] красные тесты: кумулятивные тоталы по нескольким событиям; агент сменил модель (две записи в `models`); агент без assistant-событий (нули, `models` пуст, err/stop override); старый agents-кэш без `models` → miss; `detect_status`-кейсы (interrupt после последнего assistant и т.п.) не регрессировали
- [x] переписать существующие ассерты `tokens_*` с last-event на кумулятивные
- [x] реализовать forward-scan (тоталы, per-model, последний assistant uuid, последние события для статуса); `_read_last_event` удалить, если других вызывающих нет (проверить grep'ом); задокументировать парсинг-trade-off в `[decision]`-комментарии (см. Данные)
- [x] `_AGENT_CACHE_FIELDS` += `"models"`; cache-hit требует `models`; тест в `tests/test_write_agents_cache.py`, что `models` персистится
- [x] зелёные тесты, прогон всей suite

### Task 4: render_table + render_output с колонками model/cost

**Files:**

- Modify: `status_line.py`
- Modify: `tests/test_render_output.py`
- Create: `tests/test_render_table.py`

- [x] красные тесты `render_table`: ширины (floor / метка / контент), left/right выравнивание, пустые ячейки
- [x] красные тесты `render_output` с prices: мульти-модельные группы sum/main/агента (метка/иконка/описание только на первой строке), zero-token per-model строки пропущены, порядок моделей по первому появлению, `n/a` без цены, пустая model-ячейка у агента без событий и у агента с одними нулевыми per-model записями, cost-формат из format_cost, `| `-префикс на всех табличных строках
- [x] перевести существующие тесты `render_output` на новую сигнатуру (`main_models` dict, `prices=None`); убедиться, что раскладка без цен совпадает со старой
- [x] реализовать `render_table`, перестроить `render_output` (sum = main_models + agents' models по каждой модели; группа без записей после отсечения нулевых → одна нулевая строка с пустой model)
- [x] в ЭТОЙ ЖЕ задаче адаптировать вызов в `_main_unsafe`: `main_models = main_cum.get("per_model") or {}`, `prices=None, host=""` — иначе старый позиционный вызов молча деградирует до fallback-хедера (except в `main()`) и роняет все интеграционные тесты на границе Task 4/5
- [x] зелёные тесты, прогон всей suite

### Task 5: Оркестратор, gitignore, prices.example.json, интеграция

**Files:**

- Modify: `status_line.py` (`_main_unsafe`)
- Modify: `.gitignore`
- Create: `prices.example.json`
- Modify: `tests/test_main_integration.py`

- [x] расширить `_run_main` возможностью выставлять И снимать `ANTHROPIC_BASE_URL` per-test (сейчас env машины протекает в subprocess — тесты `@host`-матчинга становятся машинозависимыми)
- [x] красные интеграционные тесты на fixture-сессии (subprocess + fake-HOME, `prices.json` пишется в fake-HOME): колонки есть с моделями kimi-k3/glm-5.3 и cost (вариант с `@host`-ключом и с plain-ключом); без файла — колонок нет; битый файл — колонок нет; `<synthetic>` не отображается
- [x] обновить ожидания кумулятивных значений агентов/sum в интеграционных тестах (если не закрыто в Task 3)
- [x] `_main_unsafe`: `provider_host()` + `load_prices(_PRICES_PATH)` → `render_output` (`main_models` передаётся уже с Task 4)
- [x] `.gitignore` += `prices.json`; создать `prices.example.json` с двумя моделями из Technical Details
- [x] зелёные тесты, прогон всей suite

### Task 6: README

**Files:**

- Modify: `README.md`

- [x] пример вывода с колонками model/cost (мульти-модельная сессия)
- [x] секция prices.json: формат, `model@host` из `ANTHROPIC_BASE_URL`, нет файла → нет колонок, `n/a`, `prices.example.json`
- [x] обновить описание семантики строк агентов (кумулятивно), Runtime dependencies (env `ANTHROPIC_BASE_URL` опционально)

### Task 7: Verify acceptance criteria

- [ ] проверить все требования Overview: колонки на sum/main/агентах, start без них, нет файла → нет колонок, n/a, пустая ячейка, `@host`-матчинг
- [ ] edge cases: битый prices.json, только synthetic, агент без событий, смена модели, host="" при отсутствии env
- [ ] полная suite: `python3 -m pytest tests/ -v` — зелёная
- [ ] smoke вручную: `echo '<payload>' | python3 status_line.py` с временным prices.json и без него

### Task 8: [Final] Документация и закрытие плана

- [ ] сверить README с реальным выводом `render_output`
- [ ] перенести план в `docs/plans/completed/` (`mkdir -p docs/plans/completed`)

## Post-Completion

_Требует ручных действий/внешних систем — информационно, без чекбоксов_

**Живая проверка (раскатка — follow-up, не задачи плана):**

- коммит в `main` = раскатка: `status_line.sh` уже подключён, хук живой на
  следующем тике статуса; препрода/inventory у проекта нет
- проверить в реальной сессии: с `prices.json` (колонки, цены, `@host`
  провайдера) и без него (старая раскладка)
- создать приватный `prices.json` с реальными ценами пользователя
  (в репо не попадает — gitignore)
