# Usage Dedup by message.id

## Design

**Проблема.** Claude Code (и нативно, и через Kimi-адаптер) пишет assistant-сообщение с несколькими content-блоками в jsonl отдельной записью на каждый блок (`['thinking']`, `['tool_use']`, `['tool_use']`, …), при этом КАЖДАЯ запись несёт полный `usage` всего сообщения. У Kimi-адаптера `requestId` отсутствует (None). Оба скана (`_scan_main_jsonl`, `_scan_agent_jsonl`) суммируют usage по всем записям без дедупликации → завышение токенов пропорционально числу блоков (наблюдалось ~3x: 416K input вместо реальных 145K; 88 записей с usage при 30 уникальных message.id). Чем лучше модель батчит параллельные tool calls, тем сильнее завышение.

**Верификация ключа дедупликации** (проведена на 15 реальных транскриптах, 1227 usage-bearing message.id):

- 0 случаев «один message.id — разные usage payload»;
- 0 случаев «один message.id — разные requestId»;
- дубли массово встречаются и у нативных `msg_*` id (883 из 1225 уникальных id встречаются >1 раза) — т.е. баг не Kimi-специфичен, нативные сессии тоже завышались.

Вывод: `message.id` — точный ключ, first-wins эквивалентен any-wins.

**Решение.** Дедупликация в точках накопления usage:

1. Новый helper `_is_duplicate_usage(seen: set, msg: dict) -> bool` рядом с `_accumulate_model` (тот же стиль shared-логики): возвращает True, если `msg["id"]` — непустая строка и уже в `seen`; иначе добавляет и возвращает False. Записи без id дедуплицировать нечем — считаются как раньше (решение пользователя; покрывает только экзотику вроде `<synthetic>` с нулевыми токенами).
2. Main scan (`status_line.py` ~строка 1044): тело `if isinstance(usage, dict):` оборачивается гейтом `if not _is_duplicate_usage(seen_usage_ids, msg):`. Внутрь гейта: `seen_first_usage`-захват, `_accumulate_model`, `context_tokens`. Content-обработка (tool_use positions, QA-паузы, `_TurnSegmenter`) остаётся СНАРУЖИ — split-записи несут разные content-блоки, и все они нужны.
3. Agent scan (`status_line.py` ~строка 1449): тот же гейт, свой `seen`-set на каждый agent jsonl (set живёт внутри `_scan_agent_jsonl`).

**Отвергнутые альтернативы:**

- _Фикс адаптера / проставление requestId_ — отклонено пользователем: «про адаптер — глупости, просто мы неправильно считаем». Тем более дубли есть и у нативных транскриптов.
- _Дедупликация записей целиком на уровне `_iter_events`_ — split-записи несут РАЗНЫЕ content-блоки; отбросив «дубли», потеряем tool_use positions и детект AskUserQuestion-пауз. Дедуплицировать можно только usage, не записи.
- _Пре-группировка записей по message.id перед сканом_ — инвазивно, меняет стриминговую семантику, выигрыша нет.
- _Инвалидация кешей через `_USAGE_REV` (по образцу `_STATUS_REV`)_ — отклонено пользователем в пользу разовой ручной чистки кешей. Принятый trade-off: без чистки закрытые сессии продолжат отдавать старые завышенные числа из кеша; защиты от будущих изменений логики подсчёта нет.

**Видимое изменение чисел:** totals/per_model/`sum:` падают до реальных значений (в т.ч. для нативных сессий — раньше завышались). Start-строка и Context не меняются (first/last occurrence, payload дублей идентичен).

## Overview

Багфикс подсчёта токенов в status_line.py: дедупликация usage по `message.id` в обоих jsonl-сканах. Интеграция — точечная: один helper + два гейта, сигнатуры и схемы кешей не меняются.

## Context (from discovery)

- Проект: статус-строка Claude Code, Python 3, один модуль `status_line.py` (~2600 строк), pytest (~499 тестов), fixtures в `tests/fixtures/`.
- Точки изменения:
  - `_accumulate_model` (`status_line.py:714`) — рядом размещается helper.
  - Main scan usage-блок (`status_line.py:1044-1074`).
  - Agent scan usage-блок (`status_line.py:1449-1454`, внутри `_scan_agent_jsonl`).
- Тесты: `tests/test_compute_main_cum.py` (main scan + кеш), `tests/test_compute_agent_snapshot.py` (agent scan). Все существующие fixtures имеют уникальные message.id (проверено) → дедуп на них no-op → текущие тесты служат бесплатной регрессией.
- Кеши: `~/.claude/status_line/data/main_<session>.json`, `agents_<session>.json`; hit по `last_uuid`+`mtime` (+field-presence guards). Схема не меняется — старые кеши валидны по ключам, поэтому нужна ручная чистка (Post-Completion).
- Документация: модульный docstring (`status_line.py:13-14`), docstrings `compute_main_cum` / `compute_agent_snapshot` («summed over all assistant events with usage»), README (~строка 400, счёт тестов и семантика).

## Development Approach

- **Testing approach: TDD** — сначала fixture + падающие тесты (доказывают баг на текущем коде), потом фикс до зелёного.
- Каждая задача завершается полностью перед переходом к следующей; маленькие фокусные изменения.
- **CRITICAL: каждая задача включает тесты** (новые/обновлённые) как обязательный deliverable.
- **CRITICAL: все тесты зелёные перед стартом следующей задачи** — без исключений.
- **CRITICAL: при смене scope во время реализации — обновить этот файл.**
- Обратная совместимость: сигнатуры, схемы кешей и формат вывода не меняются; меняются только числовые значения сумм (исправление завышения).

## Testing Strategy

- **Unit tests (TDD):**
  - Main scan: новый fixture `tests/fixtures/main_split_message.jsonl` — одно сообщение, разрезанное на 3 записи (одинаковый `message.id`, идентичный usage, разные content-блоки `thinking` + 2× `tool_use`), плюс вторая группа записей другого сообщения, плюс одна usage-запись без id. Ассерты: токены каждого сообщения посчитаны один раз; no-id запись посчитана; tool_use positions собраны со ВСЕХ split-записей; `per_model` без дублей; `start_*`/`context_tokens` соответствуют first/last occurrence.
  - Agent scan: новый fixture `tests/fixtures/agent_split_message.jsonl` + тест cumulative totals без дублей.
  - Helper `_is_duplicate_usage`: прямые unit-тесты (пустой/отсутствующий id → всегда False и не добавляется; повтор id → True; разные id → False).
- **Регрессия:** полный прогон `python -m pytest tests/ -v` — существующие тесты должны пройти без правок ожиданий (их fixtures с уникальными id → дедуп no-op). Падение существующего теста = гейт задел лишнее.
- **E2E:** отсутствуют (CLI-инструмент без UI-тестов).
- **Препрод/проперти:** не применимо — локальный инструмент, окружений и компонентов из inventory нет. Проверка на реальном Kimi-транскрипте — ручной sanity-check (Post-Completion).

## Progress Tracking

- Отмечать выполненное `[x]` немедленно; новые задачи — с префиксом ➕; блокеры — ⚠️; держать план в синке с фактической работой.

## Solution Overview

Один helper `_is_duplicate_usage` + два гейта в точках накопления usage. Дедуплицируется ТОЛЬКО usage-накопление; вся остальная обработка событий (content, время, статусы) идёт по всем записям как раньше. Ключ — `message.id`, first-wins (верифицировано: payload дублей идентичен).

## Technical Details

```python
def _is_duplicate_usage(seen: set, msg: dict) -> bool:
    """True when this assistant message's usage was already counted.

    Claude Code writes one jsonl record per content block of an
    assistant message, each carrying the FULL message usage (native
    transcripts too, not only split-message adapters). message.id is
    stable across those records, so first-wins by id is exact.
    Records without an id can't be deduped — counted as before.
    """
    mid = msg.get("id")
    if not isinstance(mid, str) or not mid:
        return False
    if mid in seen:
        return True
    seen.add(mid)
    return False
```

- Main scan: `seen_usage_ids: set = set()` рядом с `per_model`/прочим состоянием скана; гейт вокруг тела `if isinstance(usage, dict):`.
- Agent scan: `seen_usage_ids` — локальное состояние `_scan_agent_jsonl` (новый set на каждый вызов = на каждый agent jsonl).
- Память: плоские строки id, копеечно.

## What Goes Where

- **Implementation Steps** (`[ ]`): код, fixtures, тесты, документация — всё внутри репозитория.
- **Post-Completion** (без чекбоксов): ручной sanity-check на реальном транскрипте и разовая чистка кешей (follow-up раскатка).

## Implementation Steps

### Task 1: Падающие тесты дедупликации для main scan (TDD red)

**Files:**

- Create: `tests/fixtures/main_split_message.jsonl`
- Modify: `tests/test_compute_main_cum.py`

- [x] создать fixture: сообщение A разрезано на 3 записи (один `message.id`, идентичный usage, блоки `thinking`/`tool_use`/`tool_use` с разными tool_use id), сообщение B — 2 записи (другой id), плюс одна usage-запись без id; timestamps/uuid — по образцу `main_with_tool_use.jsonl`
- [x] тест: суммы in/out/cached считают каждое сообщение один раз (A один раз, B один раз, no-id запись посчитана)
- [x] тест: `per_model` не содержит дублированных накоплений
- [x] тест: tool_use positions содержат tool_use id со ВСЕХ split-записей (доказывает, что content-обработка не под гейтом)
- [x] тест: `start_*` — usage первого occurrence, `context_tokens` — последнего (гейт их не ломает)
- [x] прогнать новые тесты — ДОЛЖНЫ УПАСТЬ на текущем коде (подтверждение бага); полный прогон не требуется до Task 3

### Task 2: Падающий тест дедупликации для agent scan (TDD red)

**Files:**

- Create: `tests/fixtures/agent_split_message.jsonl`
- Modify: `tests/test_compute_agent_snapshot.py`

- [x] создать agent-fixture: 2+ assistant-сообщения, разрезанные на split-записи с полным usage
- [x] тест: `tokens_in/out/cached` и `models` считают каждое сообщение один раз (meta — переиспользовать `META_NORMAL`, `cache_entry=None`, по конвенции `tests/test_compute_agent_snapshot.py`)
- [x] прогнать новый тест — ДОЛЖЕН УПАСТЬ на текущем коде

### Task 3: Helper `_is_duplicate_usage` + гейт в main scan (TDD green)

**Files:**

- Modify: `status_line.py`

- [x] добавить `_is_duplicate_usage` рядом с `_accumulate_model` (docstring по образцу соседних helpers)
- [x] unit-тесты helper'а: отсутствующий id → False; пустой/не-строковый id → False; новый id → False + добавлен в seen; повторный id → True
- [x] main scan: `seen_usage_ids: set` в состоянии скана; обернуть usage-блок гейтом (внутри: `seen_first_usage`, `_accumulate_model`, `context_tokens`)
- [x] прогнать тесты Task 1 + helper'а — зелёные
- [x] прогнать ПОЛНЫЙ suite `python -m pytest tests/ -v` — зелёный, кроме пока красного теста Task 2 (принятое отклонение от правила all-green: TDD red для agent scan фиксится в Task 4 — не «чинить» переупорядочиванием)

### Task 4: Гейт в agent scan (TDD green)

**Files:**

- Modify: `status_line.py`

- [x] `_scan_agent_jsonl`: свой `seen_usage_ids` на вызов; тот же гейт вокруг usage-накопления
- [x] прогнать тест Task 2 — зелёный
- [x] прогнать полный suite — зелёный

### Task 5: Verify acceptance criteria

- [x] split-сообщение считается один раз в обоих сканах; записи без id — как раньше
- [x] content-обработка (tool_use positions, QA-паузы, сегментер) не под гейтом — покрыто тестом Task 1
- [x] существующие тесты не потребовали правок ожиданий (регрессия no-op подтверждена)
- [x] полный прогон: `python -m pytest tests/ -v` — все зелёные

### Task 6: Update documentation

- [x] модульный docstring (`status_line.py:13-14`): «summed over all assistant events with usage» → дедупликация по message.id
- [x] docstrings `compute_main_cum` и `compute_agent_snapshot`: уточнить семантику накопления (first-wins по message.id)
- [x] docstrings `_scan_main_jsonl` (`status_line.py:957-1008`) и `_scan_agent_jsonl` (`status_line.py:1362-1406`): правила накопления `per_model`/totals — дедупликация по message.id
- [x] docstrings тест-модулей `tests/test_compute_main_cum.py:1-24` и `tests/test_compute_agent_snapshot.py:9-14`: «CUMULATIVE sums over ALL assistant events» → с дедупликацией по message.id
- [x] README: обновить счёт тестов и, если описано, семантику подсчёта токенов
- [x] переместить план в `docs/plans/completed/` — plan move handled by the exec harness

## Post-Completion

**Manual verification:**

- Sanity-check на реальном Kimi-транскрипте: числа сессии должны сойтись с ручным подсчётом по уникальным message.id (наблюдавшийся кейс: 416K→~145K input).

**Follow-up: раскатка (отдельный шаг после завершения плана, НЕ задача плана):**

- Разовая чистка кешей, чтобы закрытые сессии пересчитались (иначе старые завышенные числа отдаются из кеша вечно):
  ```bash
  rm ~/.claude/status_line/data/main_*.json ~/.claude/status_line/data/agents_*.json
  ```
- Препрод-окружений/пропертей нет — локальный инструмент; сверка с inventory не требуется.
