# Status line: временные колонки work/wait/total

## Overview

Добавляем в таблицу статуса три колонки времени:

- `work` — время автономной работы: объединение (union) интервалов
  ходов главной сессии и интервалов жизни всех сабагентов, минус
  AskUserQuestion-паузы. Ожидание агентов считается работой (правило
  пользователя).
- `wait` — время ожидания пользователя: `total − work` (кламп ≥ 0);
  включает текущий незавершённый простой.
- `total` — суммарное стеновое время: `now − time_first_ts`.

Следствие union-модели (согласовано): строки `main:` и `sum:` показывают
одинаковые три числа — ожидание агентов уже входит в `main:`. Параллельные
агенты не задваиваются (union, не сумма).

Колонки всегда видимы — в обоих режимах (с `prices.json` и без), правее
`units` (без цен — правее `cached`). Формат `HH:MM:SS`, часы без предела
(`03:45:12`, `103:25:10`). Live-now: незавершённый интервал считается до
момента рендера; `now` передаётся явным параметром (в тестах фиксируется).

Распределение по строкам:

- `start:` — три пустые ячейки (референс-строка токенов).
- `sum:` / `main:` — union-значения сессии.
- Агенты — `work = длительность − Σ QA-пауз`, `wait = Σ QA-пауз`,
  `total = длительность`; длительность работающего агента растёт до now.
- В prices-режиме значения едут только на первой строке группы (где
  label), продолжения per-model — пустые ячейки.
- Деградация (нет таймстемпов) — пустые ячейки, НЕ `00:00:00`.

## Context (from discovery)

- Файлы:
  - `status_line.py` — `format_tokens` (~49), `_scan_main_jsonl` (~607),
    `compute_main_cum` (~784), `_scan_agent_jsonl` (~937),
    `compute_agent_snapshot` (~1020), `_token_columns` (~1622),
    `render_table` (~1460), `render_output` (~1653),
    `_group_model_rows` (~1580), `_AGENT_CACHE_FIELDS` (~1867),
    `_compute_agents` (~1948), `_main_unsafe` (~2045).
  - `README.md` — примеры вывода, семантика, кэши, тесты.
- Тесты: `tests/conftest.py` (хелперы jsonl), `test_compute_main_cum.py`,
  `test_compute_agent_snapshot.py`, `test_render_output.py`,
  `test_main_integration.py`; фикстура `tests/fixtures/real_session/`
  (38 сабагентов, 16 «настоящих» user-запросов в main jsonl).
- Данные подтверждены: `timestamp` (ISO 8601, `Z`, мс) есть у всех
  user/assistant/attachment/queue-operation событий; НЕТ у `mode`,
  `ai-title`, `last-prompt` и пр. «Настоящие» user-события (строковый
  контент): промпты, команды, interrupt'ы; tool_result'ы — список.
- Паттерны проекта: field-presence гарды кэша (`per_model`, `models`) —
  до-апгрейдный кэш один раз перечитывается; atomic write; «хук не имеет
  права упасть»; stdlib-only; Python 3.9 (`fromisoformat` не парсит `Z`).

## Development Approach

- **Testing approach**: TDD — каждую задачу начинаем с тестов (красный),
  затем реализация (зелёный), прогон перед следующей задачей.
- Каждая задача — один логический блок; маленькие фокусные изменения.
- **Каждая задача обязана содержать новые/обновлённые тесты** — и для
  успеха, и для ошибочных/краевых сценариев.
- **Все тесты зелёные до начала следующей задачи** — без исключений.
- План-файл обновляется при изменении скоупа (➕ новые задачи, ⚠️ блокеры).
- Обратная совместимость: прямой вызов `render_output` без новых
  параметров рендерит пустые временные ячейки; старые кэши
  перечитываются один раз и переписываются.

## Testing Strategy

- Юнит-тесты на каждую задачу (см. выше) — в репо нет e2e/UI-тестов,
  pytest — единственный раннер: `python3 -m pytest tests/ -v`.
- Синтетические jsonl-фикстуры пишутся хелперами по образцу
  существующих тестов (`conftest.py`).
- Заморозка `now`: существующие интеграционные тесты гоняют модуль
  subprocess'ом (monkeypatch не пересекает границу процесса), поэтому
  frozen-now кейсы вызывают `_main_unsafe(now=…)` in-process с
  monkeypatch `sys.stdin` и `capsys`; real_session-проверки
  (`work + wait == total` ±1с, `main:` == `sum:`, > 0, три колонки)
  инвариантны к `now` — их оставляем в subprocess-формате.
- Препрод-окружений нет — модуль живёт в рабочей директории
  `~/.claude/status_line/`; живая проверка оформлена follow-up'ом
  (Post-Completion), не задачей плана.

## Progress Tracking

- Выполненные пункты помечать `[x]` сразу после завершения.
- Новые задачи — с префиксом ➕, блокеры — с префиксом ⚠️.

## Solution Overview

Семантическая сегментация (вариант A брейншторма):

- Main-скан делит сессию на ходы по «настоящим» user-событиям
  (`type=user`, `message.content` — строка: промпт/команда/interrupt;
  tool_result-события со списком — НЕ границы). Ход =
  [запрос → последняя активность], активность = user/assistant события
  с ts (trailing `queue-operation`/`system` ход НЕ продлевают —
  уведомление о фоновом агенте не должно сдвигать начало ожидания).
- AskUserQuestion-пауза = [assistant с `tool_use` name=`AskUserQuestion`
  → следующее user-событие любого вида]. Вырезается из work хода
  (расщепляет его на подынтервалы). Симметрично в main и агентах.
  Неполученный ответ (последний assistant — QA без ответа) = открытая
  пауза: текущий интервал НЕ продлевается, разрыв растёт как wait.
- Агент: интервал жизни [первый ts → последний ts], работающий
  (`status=run` без открытой QA) продлевается до now.
- Открытый ход main продлевается до now. Ход открыт, если: последний
  assistant `stop_reason` ∈ {`tool_use`, `pause_turn`}, или после него
  идут tool_result'ы, или последний настоящий промпт без ответа.
  Закрыт при `end_turn`/interrupt/ошибке/открытой QA.

Уточнение к брейншторму (упрощение без изменения видимой семантики):
поле `time_anchor` не нужно — «где растёт» выражается продлением
последнего подынтервала до now при `time_open=True`; при закрытом ходе
и открытой QA разрыв уходит в wait автоматически через
`wait = total − work`.

## Technical Details

**Новые чистые функции** (`status_line.py`, секция рядом с
`format_tokens`):

- `_parse_ts(value) -> float | None` — ISO 8601 → epoch; `Z` → `+00:00`
  вручную (Py 3.9), naive → UTC, мусор/None → None.
- `format_duration(seconds) -> str` — `HH:MM:SS`, часы без предела,
  минуты/секунды с ведущим нулём, отрицательное → `00:00:00`.
- `union_work(intervals: list) -> float` — сортировка, склейка
  пересекающихся И смежных, сумма длительностей; вырожденные
  (`e <= s`) интервалы отбрасываются.

**Main-скан** (`_scan_main_jsonl`) — новые поля результата:

- `time_first_ts: float` — epoch первого события с ts любого типа
  (якорь total); `0.0` когда ts нет вовсе.
- `time_turns: list` — список ходов; ход = список подынтервалов
  `[s, e]` epoch (QA-паузы расщепляют; ход без активности = `[[u, u]]`).
  Активность до первого настоящего промпта ходом не становится.
- `time_open: bool` — открыт ли последний ход.

`_EMPTY_MAIN_RESULT` расширяется нулями/пустотой. Cache-hit в
`compute_main_cum` дополнительно требует наличия всех трёх полей
(паттерн `per_model`). Ключ валидации не меняется
(`last_uuid` + `mtime_jsonl`).

**Агент-скан** (`_scan_agent_jsonl`) — новые поля:

- `ts_first`, `ts_last: float` — epoch первого/последнего события с ts
  (`0.0` при отсутствии).
- `qa_pauses: list` — закрытые пары `[s, e]`.
- `qa_open_ts: float` — начало незакрытой QA-паузы, `0.0` если нет.

`compute_agent_snapshot` пропускает их в снапшот;
`_AGENT_CACHE_FIELDS` расширяется на все четыре; presence-гард
cache-hit — тоже.

**Оркестратор** (`_main_unsafe(now=None)`; `main()` передаёт
`time.time()`, тесты — фиксируют monkeypatch'ем):

- Интервалы: `time_turns` (развёрнутые подынтервалы) + при
  `time_open` продление последнего подынтервала до `now` + по каждому
  агенту: жизнь минус `qa_pauses`, при `status=run` и `qa_open_ts==0`
  продление последнего подынтервала до now, при `qa_open_ts>0`
  подынтервалы обрезаются по `qa_open_ts`.
- `work = min(union_work(...), total)`, `total = now − time_first_ts`,
  `wait = max(0, total − work)` — инвариант `work + wait == total`.
- Пер-агентно: `dur = (ts_last, продлённый до now если run-без-QA) −
ts_first`; `wait_ag = Σ qa_pauses + (now − qa_open_ts если открыта)`;
  `work_ag = dur − wait_ag`. Значения (секунды или None при деградации)
  инжектятся в agent-дикты ПОСЛЕ `_write_agents_cache` (транзиентны) и
  читаются рендером как `time_work`/`time_wait`/`time_total`.

**Рендер** (`render_output(..., main_time=None)`):

- Новый хелпер `_time_columns()`: три колонки `work`/`wait`/`total`,
  right-aligned, floor 8, гэп блока `_DESC_TOKEN_GAP`, внутри — `" "`.
- Prices-режим: колонки добавляются после units-колонки; plain-режим —
  после `cached` (её гэп становится `_DESC_TOKEN_GAP`).
- Ячейки: `format_duration(сек)` или `""` (None/отсутствие данных);
  `start:` — всегда пустые; `_group_model_rows` получает временные
  ячейки и ставит их только на первую строку группы.

**Края**: непарсящийся/отсутствующий timestamp — событие молча
пропускается для времени; отрицательные длительности клампятся;
мульти-дирные сессии — агенты из всех дир дают интервалы (dedup по
`agentId` уже есть); `ts_first == 0.0` → пустые ячейки строки.
JSON `null` в полях времени кэша проходит presence-гард, но роняет
арифметику — чтение из кэша коэрсит по конвенции репо
(`or 0.0` / isinstance, как `_to_int` в `_main_unsafe`). Работа агента
теоретически может начаться раньше `time_first_ts` main (resumed/
мульти-дир) → `work > total`; клампим `work = min(work, total)`,
инвариант `work + wait == total` сохраняется.

## What Goes Where

- **Implementation Steps** (`[ ]`) — задачи в этом репозитории: код,
  тесты, документация.
- **Post-Completion** (без чекбоксов) — живая проверка на реальной
  сессии (follow-up после тестов, по правилам раскатка не входит в
  основной план).

## Implementation Steps

### Task 1: format_duration + _parse_ts (чистые функции)

**Files:**

- Modify: `status_line.py`
- Create: `tests/test_format_duration.py`

- [x] написать тесты `format_duration`: 0 → `00:00:00`, 59.9с → `00:00:59`, 60с → `00:01:00`, 3599с → `00:59:59`, 3600с → `01:00:00`, 24ч → `24:00:00`, 100ч+ → `103:25:10`-кейс, отрицательное → `00:00:00`, дробные секунды — усечение
- [x] написать тесты `_parse_ts`: `Z`-суффикс, `+00:00`, смещение зоны, naive → UTC, мусор/None/пусто → None, мс сохраняются
- [x] прогнать — красный
- [x] реализовать обе функции рядом с `format_tokens`
- [x] прогнать — зелёный, до задачи 2

### Task 2: union_work (чистая функция)

**Files:**

- Modify: `status_line.py`
- Create: `tests/test_union_work.py`

- [x] написать тесты: пустой список → 0; один интервал; пересечение; вложенность; смежность склеивается (`[0,10]+[10,20]` → 20); несортированный вход; вырожденные `e<=s` отбрасываются; «дырки» — интервал с вырезанной серединой суммируется из кусков
- [x] прогнать — красный
- [x] реализовать `union_work`
- [x] прогнать — зелёный, до задачи 3

### Task 3: сегментация main-скана + main-кэш

**Files:**

- Modify: `status_line.py` (`_scan_main_jsonl`, `_EMPTY_MAIN_RESULT`, `compute_main_cum`)
- Create: `tests/test_time_segmentation.py`
- Modify: `tests/test_compute_main_cum.py`

- [x] написать тесты сегментации на синтетических jsonl (через `_scan_main_jsonl`): один ход; несколько ходов с паузами между ними; interrupt-событие закрывает ход; открытый ход (`stop_reason=tool_use` И `pause_turn` → `time_open=True`); закрытый (`end_turn`; плюс `stop_sequence` — второй по частоте в реальных данных); trailing tool_result'ы держат ход открытым; QA-пауза расщепляет подынтервалы; открытая QA → `time_open=False`, подынтервалы обрезаны; события без ts и trailing `queue-operation` не продлевают ход; активность до первого промпта игнорируется; пустой jsonl → нули/пустота
- [x] написать тесты кэша: до-апгрейдный `main_<sid>.json` без полелей времени → rescan и rewrite; hit возвращает `time_*`-поля
- [x] прогнать — красный
- [x] реализовать сбор `time_first_ts`/`time_turns`/`time_open` в скане; расширить `_EMPTY_MAIN_RESULT`; добавить presence-гард в cache-hit `compute_main_cum`
- [x] прогнать — зелёный, до задачи 4

### Task 4: временные поля агент-скана + agents-кэш

**Files:**

- Modify: `status_line.py` (`_scan_agent_jsonl`, `compute_agent_snapshot`, `_AGENT_CACHE_FIELDS`)
- Modify: `tests/test_compute_agent_snapshot.py`

- [x] написать тесты: `ts_first`/`ts_last` по событиям с ts; события без ts пропускаются; `qa_pauses` — закрытая пара [QA-assistant → user-ответ]; `qa_open_ts` — QA без ответа; пустой/битый jsonl → нули
- [x] написать тесты кэша: снапшот несёт четыре поля; `_AGENT_CACHE_FIELDS` включает их; до-апгрейдная запись без полей → miss и re-scan
- [x] прогнать — красный
- [x] реализовать сбор полей, прокидывание в снапшот, расширение `_AGENT_CACHE_FIELDS` и presence-гарда
- [x] прогнать — зелёный, до задачи 5

### Task 5: колонки work/wait/total в рендере

**Files:**

- Modify: `status_line.py` (`_time_columns`, `render_output`, `_group_model_rows`, `_token_columns`)
- Modify: `tests/test_render_output.py`

- [x] написать тесты: колонки есть в обоих режимах (labels `work`/`wait`/`total`, right-align, floor 8); prices-режим — после units, plain — после cached; `start:` — пустые ячейки; `main_time` рендерится в `sum:`/`main:`; агент без полелей времени → пустые ячейки; агент с `time_*` → значения на первой строке группы, продолжения per-model пустые; None-значения → пустые ячейки (не `00:00:00`); формат `HH:MM:SS`
- [x] прогнать — красный
- [x] реализовать `_time_columns`, параметр `main_time` у `render_output`, временные ячейки в `_group_model_rows` (только первая строка группы)
- [x] обновить устаревающие докстринги: модульный (обещание «prices=None байт-в-байт как раньше»), `render_output` («Layout with prices=None is byte-identical…»), `_token_columns` («the plain single space when nothing follows») — с пометкой `[deviation]` по конвенции репо
- [x] прогнать — зелёный, до задачи 6

### Task 6: оркестратор + интеграция

**Files:**

- Modify: `status_line.py` (`_main_unsafe`, `main`)
- Modify: `tests/test_main_integration.py`

- [x] написать тест-кейс «фоновый агент заполняет простой main»: union превращает wait в work (главное правило) — frozen-now: вызов `_main_unsafe(now=…)` in-process (monkeypatch `sys.stdin`, `capsys`), не subprocess
- [x] написать тест: параллельные агенты не задваиваются (union) — тот же in-process механизм
- [x] написать интеграционные тесты на `real_session` (существующий subprocess-формат, проверки инвариантны к `now`): `work + wait == total` (±1с), строки `main:` == `sum:`, значения > 0, вывод содержит три колонки
- [x] прогнать — красный
- [x] реализовать `_main_unsafe(now=None)`: сбор интервалов, продления (open ход, run-агент, обрезка по `qa_open_ts`), `union_work`, инжект `time_*` в агент-дикты после `_write_agents_cache`, передача `main_time` в `render_output`; `main()` передаёт `time.time()`
- [x] прогнать — зелёный, до задачи 7

### Task 7: Verify acceptance criteria

- [x] проверить требования Overview: три колонки, union-семантика, ожидание агентов = работа, QA-паузы, live-now, формат, распределение по строкам
- [x] проверить края: нет ts → пустые ячейки; до-апгрейдные кэши перечитываются; пустой stdin / битый jsonl не падают
- [x] полный прогон: `python3 -m pytest tests/ -v`
- [x] убедиться, что старые тесты не деградировали (обновлены там, где менялся контракт)

### Task 8: [Final] Update documentation

- [x] обновить `README.md`: примеры вывода с тремя колонками (оба режима), семантика work/wait/total (union, QA-паузы, live-now), новые поля кэшей, актуальное число тестов
- [x] перенести план в `docs/plans/completed/` (skipped — план НЕ перемещается: харнесс переносит его сам после всех фаз; перемещение посреди прогона ломает последующие review/finalize/stats-фазы)

## Post-Completion

_Требует ручного вмешательства — информационно, без чекбоксов._

**Живая проверка (follow-up после тестов):**

- Запустить статус-строку на активной реальной сессии: числа
  правдоподобны (total растёт, wait растёт в простое, work растёт при
  работающих агентах), колонки не разъехались в терминале.
- Проверить переходный кэш: после апгрейда первый запуск перечитывает
  jsonl, второй — cache-hit (data/ не растёт аномально).

**Внешние системы:** нет — модуль локальный, раскатка не требуется
(файл уже в живой директории `~/.claude/status_line/`).
