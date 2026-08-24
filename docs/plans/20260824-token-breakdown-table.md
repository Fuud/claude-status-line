# Token Breakdown Table

## Overview

- Заменить плоский вывод токенов (`sum: 5k`, `main: 3k`, `[ok] desc 100`) в статус-линии Claude Code на таблицу с заголовком и разбивкой по категориям `in / out / cached`.
- Колонка `cached` = только `cache_read_input_tokens`. `cache_creation` не выводится.
- Никакого total нигде — ни в render, ни в кэше. Поле `tokens` уходит целиком (было derived, теперь dead).
- Run-агенты теперь показывают текущие значения из последнего assistant event (а не `None`, как раньше).
- Агенты без assistant events рендерятся тремя нулями (никогда не пропускаются).
- Cache-hit для агентов теперь проверяет не только ключи (last_uuid, mtime_jsonl, mtime_meta), но и наличие новых полей — иначе апгрейд на живой системе даст один цикл неверного вывода.

## Context (from discovery)

- **Файлы под изменение:**
- `status_line.py` — основной runtime-модуль (~970 строк)
- `tests/test_render_output.py` — тесты рендера
- `tests/test_compute_agent_snapshot.py` — тесты snapshot
- `tests/test_compute_main_cum.py` — тесты main-cum (затрагивает drop `total`)
- `tests/test_main_integration.py` — интеграционные тесты
- `CLAUDE.md` — архитектурные заметки
- **Существующие паттерны:**
- `compute_main_cum` уже возвращает `cum_in, cum_out, cum_cache_create, cum_cache_read`. Поле `total` убираем целиком (dead после этого изменения).
- `_sum_usage()` суммирует все 4 типа. Перестаём использовать — нам нужны отдельные числа.
- Cache-hit ветка `compute_agent_snapshot` сравнивает только (last_uuid, mtime_jsonl, mtime_meta). Добавляем проверку наличия breakdown-полей для корректного upgrade-пути.
- Существующие константы `_TOKEN_COLUMN_WIDTH = 7`, `_STATUS_GAP`, `_DESC_TOKEN_GAP` переиспользуем.

## Development Approach

- **testing approach**: TDD (тесты сначала)
- complete each task fully before moving to the next
- make small, focused changes
- **every task MUST include new/updated tests** — обязательная часть чек-листа
- **all tests must pass before starting next task** — без исключений
- **update this plan file when scope changes** during implementation
- **run tests after each change** — `python -m pytest tests/`

## Testing Strategy

- **unit tests**: обязательны в каждой задаче (см. Development Approach)
- **integration tests**: `tests/test_main_integration.py` обновляется в той же задаче, что и оркестратор (`_main_unsafe`)
- **smoke test**: ручная проверка через `status_line.sh` в хуке Claude Code на собственной сессии — глазами убедиться, что header таблицы появляется, колонки выровнены, run-агенты показывают текущие значения

## Progress Tracking

- mark completed items with `[x]` immediately when done
- add newly discovered tasks with ➕ prefix
- document issues/blockers with ⚠️ prefix

## Solution Overview

1. `compute_agent_snapshot` возвращает три breakdown-поля: `tokens_in`, `tokens_out`, `tokens_cached`. Поле `tokens` удаляется полностью (snapshot, cache fields, тесты).
2. `_AGENT_CACHE_FIELDS` расширяется тремя новыми полями. Cache-hit **дополнительно** проверяет наличие этих полей в `cache_entry` — иначе устаревшие записи (с подходящими ключами, но без breakdown) дадут `None`-значения в выводе.
3. `compute_main_cum` перестаёт возвращать `total`. Поле убирается из `_EMPTY_MAIN_RESULT` и из result-словаря.
4. `render_output` переписывается: новая сигнатура принимает `main_in/out/cached` (не `main_total`), рендерит заголовок таблицы, форматирует каждое число через `format_tokens()`.
5. `_main_unsafe` распаковывает `cum_in/cum_out/cum_cache_read` из `compute_main_cum` и передаёт в render.
6. Тесты обновляются под новый формат и под drop `tokens`/`total`.

## Technical Details

### Новый формат `compute_agent_snapshot`

```python
{
    "agentId": "agent-abc",
    "status": "ok" | "run" | "err" | "stop",
    "tokens_in":     int,   # input_tokens из last assistant event
    "tokens_out":    int,   # output_tokens
    "tokens_cached": int,   # cache_read_input_tokens (только read)
    "description": str,
    "toolUseId": str,
    "last_uuid": str | None,
    "mtime_jsonl": float,
    "mtime_meta":  float,
}
```

Правила заполнения:

- `last_event is None` → `tokens_in = tokens_out = tokens_cached = 0`
- `last_event.message.usage` отсутствует/не dict → `0` во всех трёх
- Иначе → `int(usage.get(field, 0) or 0)` для каждого

Поле `tokens` НЕ возвращается. Никто его не потребляет после изменения, в кэше оно тоже не нужно.

### Cache hit для upgrade-пути

В `compute_agent_snapshot` cache-hit ветка проверяет **дополнительно**, что в `cache_entry` присутствуют все три breakdown-поля. Если хотя бы одного нет — fallback на пересчёт. Без этого устаревший кэш (ключи совпадают, но полей нет) вернёт `{**cache_entry, "agentId": agent_id}` без breakdown → render подставит нули через `int(... or 0)` и пользователь увидит ложную картину до первого мутирования jsonl.

Конкретный код (заменить существующий hit-check):

```python
if cache_entry is not None:
    if (
        cache_entry.get("last_uuid") == last_uuid_for_compare
        and cache_entry.get("mtime_jsonl") == mtime_jsonl
        and cache_entry.get("mtime_meta") == mtime_meta_for_compare
        and "tokens_in" in cache_entry
        and "tokens_out" in cache_entry
        and "tokens_cached" in cache_entry
    ):
        return {**cache_entry, "agentId": agent_id}
```

### `compute_main_cum` — drop `total`

Убираем из `_EMPTY_MAIN_RESULT` ключ `"total"`. Убираем из result-словаря в `compute_main_cum` строку `"total": cum_in + cum_out + cum_cache_create + cum_cache_read`. Никто не читает `total` после изменения — render получает три отдельных числа.

### Новая сигнатура `render_output`

```python
def render_output(
    header: str,
    main_in: int,
    main_out: int,
    main_cached: int,
    agents: list,
) -> str
```

### Алгоритм render

1. Собрать все ячейки колонок:

- `in_col = [main_in] + [int(a.get("tokens_in") or 0) for a in agents]`
- `out_col = [main_out] + [int(a.get("tokens_out") or 0) for a in agents]`
- `cached_col = [main_cached] + [int(a.get("tokens_cached") or 0) for a in agents]`

2. Для каждой колонки вычислить ширину: `max(len(format_tokens(v)) for v in col + [label])`. Минимум — существующий `_TOKEN_COLUMN_WIDTH = 7`.
3. Первая строка: `header`.
4. Вторая строка: три метки `in / out / cached`, каждая выровнена по правому краю под свою колонку, разделены пробелом.
5. Далее строки:

- `sum: {format_tokens(main_in):>W1} {format_tokens(main_out):>W2} {format_tokens(main_cached):>W3}` (только если `len(agents) > 0`)
- `main: {format_tokens(main_in):>W1} {format_tokens(main_out):>W2} {format_tokens(main_cached):>W3}`
- `f"{icon}{_STATUS_GAP}{description}{_DESC_TOKEN_GAP}{format_tokens(a['tokens_in']):>W1} {format_tokens(a['tokens_out']):>W2} {format_tokens(a['tokens_cached']):>W3}"` — по одной на агента

**Важно:** каждое число оборачивается в `format_tokens()` ДО форматирования `:>W`. Без этого 1000 отрендерится как `"   1000"`, а не `"   1k"`.

### Cache backward compatibility

- `data/main_<sid>.json`: drop `total` ключа. Старые записи с `total` будут читаться без ошибок (мы просто не используем `total`), но пересчитаются при cache miss из-за смены ключа (last_uuid не меняется → cache hit → `total` в словаре есть, но игнорируется). Для чистоты — можно мигрировать через проверку наличия `"total"` в записи и удаление, либо оставить как dead-поле (низкий риск).
- `data/agents_<sid>.json`: добавляются 3 поля в `_AGENT_CACHE_FIELDS`. Старые записи без них → cache miss **благодаря field-presence check** (см. выше) → forward-скан jsonl → новые поля заполнятся.
- Никакого `schema_version` — обходимся in-place валидацией.

## What Goes Where

- **Implementation Steps** (с чекбоксами): задачи внутри этого кодбейса
- **Post-Completion**: ручная проверка через `status_line.sh` на собственной машине (smoke test)

## Implementation Steps

### Task 1: Расширить `compute_agent_snapshot` breakdown-полями + cache upgrade-check

**Files:**

- Modify: `status_line.py` (функция `compute_agent_snapshot`, константа `_AGENT_CACHE_FIELDS`)
- Modify: `tests/test_compute_agent_snapshot.py`

- [x] добавить тесты: `tokens_in / tokens_out / tokens_cached` присутствуют в snapshot с правильными значениями из `last_event.message.usage`
- [x] добавить тест: `last_event is None` → все три поля = 0
- [x] добавить тест: status `run` + last_event есть → breakdown не нули
- [x] добавить тест: usage-блок отсутствует → все три поля = 0
- [x] добавить тест: cache hit (cache_entry с breakdown-полями + совпадающие ключи) → возвращается запись с breakdown
- [x] добавить тест: cache miss при совпадающих ключах, но отсутствии breakdown-полей → пересчёт заполняет все поля (upgrade-путь)
- [x] удалить или обновить существующие тесты, проверяющие поле `tokens` (например `test_agent_ok_full_snapshot`, `test_agent_running_snapshot` и т.п.) — больше нет поля `tokens`
- [x] обновить `test_agent_no_assistant_*`: теперь проверять `tokens_in/out/cached == 0` (не `tokens is None`)
- [x] в `compute_agent_snapshot` после получения `last_event` добавить вычисление `tokens_in`, `tokens_out`, `tokens_cached` через `int(usage.get(field, 0) or 0)`
- [x] в `compute_agent_snapshot` убрать ветку `tokens=None` для run-агентов — теперь всегда возвращаем числа (или нули)
- [x] в `compute_agent_snapshot` убрать поле `tokens` из возвращаемого dict (полностью)
- [x] в cache-hit ветке `compute_agent_snapshot` добавить field-presence check: `"tokens_in" in cache_entry and "tokens_out" in cache_entry and "tokens_cached" in cache_entry`
- [x] в `_AGENT_CACHE_FIELDS` убрать `"tokens"`, добавить `"tokens_in", "tokens_out", "tokens_cached"`
- [x] запустить `python -m pytest tests/test_compute_agent_snapshot.py` — все тесты должны проходить

### Task 2: Drop `total` из `compute_main_cum`

**Files:**

- Modify: `status_line.py` (функция `compute_main_cum`, константа `_EMPTY_MAIN_RESULT`)
- Modify: `tests/test_compute_main_cum.py`

- [x] добавить тест: `compute_main_cum` возвращает dict БЕЗ ключа `"total"`
- [x] добавить тест: `_EMPTY_MAIN_RESULT` (через `compute_main_cum` с несуществующим jsonl) не содержит `"total"`
- [x] удалить ключ `"total": 0` из `_EMPTY_MAIN_RESULT`
- [x] удалить строку `"total": cum_in + cum_out + cum_cache_create + cum_cache_read` из result-словаря в `compute_main_cum`
- [x] удалить или обновить существующие тесты, проверяющие `result["total"]` (например `test_compute_main_cum_basic`, `test_total_field_*` и т.п.)
- [x] запустить `python -m pytest tests/test_compute_main_cum.py` — все тесты должны проходить

### Task 3: Переписать `render_output` под табличный формат (TDD)

**Files:**

- Modify: `status_line.py` (функция `render_output`)
- Modify: `tests/test_render_output.py`

- [x] обновить `test_single_ok_agent`: новый формат — header, заголовок таблицы, `sum:`, `main:`, строка агента с тремя числами (через `format_tokens`)
- [x] обновить `test_zero_agents_no_sum_line`: новый формат — header, заголовок таблицы, `main:` (без `sum:`)
- [x] обновить `test_38_agents_produce_41_lines`: новое ожидание — 42 строки (+1 на заголовок таблицы)
- [x] обновить `test_token_alignment_right_aligned`: три колонки, каждая выровнена независимо; проверить, что для 1000 рендерится `1k`, а не `1000`
- [x] обновить `test_long_description_truncated`: формат строки агента с тремя числами, эллипсис всё ещё присутствует
- [x] удалить `test_agent_with_no_tokens` (семантика ушла) → заменить на `test_run_agent_shows_current_values`
- [x] обновить `test_sum_calculation`: sum = сумма breakdown (in+out+cached) по всем строкам
- [x] добавить `test_table_header_row`: после `header` идёт строка с метками `in / out / cached`, каждая right-aligned
- [x] добавить `test_three_columns_right_aligned`: 3 агента с разной шириной in/out/cached → каждая колонка выравнивается независимо
- [x] добавить `test_sum_aggregates_all_rows`: sum = main_in + sum(agent_in), аналогично для out/cached
- [x] добавить `test_agent_no_assistant_events_renders_zeros`: агент с отсутствующими/None breakdown полями → рендер `0 0 0` (не пропускается)
- [x] добавить `test_run_agent_shows_current_values`: status `run`, но breakdown из last_event не нули
- [x] добавить `test_large_values_format_as_k`: in=2000 → рендерится как `2k`, не `2000`
- [x] в `render_output` заменить сигнатуру: `header, main_in, main_out, main_cached, agents`
- [x] в `render_output` вычислить ширины трёх колонок через `max(len(format_tokens(v)) for v in col + [label])`
- [x] в `render_output` собрать строки: header → заголовок таблицы → (если есть агенты) `sum:` → `main:` → по строке на агента
- [x] **критично:** в каждом f-string с числом обернуть значение в `format_tokens()` ДО `:>W` (строки sum, main, агент)
- [x] обработать `tokens_in/out/cached is None` → подставить `0` через `int(a.get(field) or 0)`
- [x] запустить `python -m pytest tests/test_render_output.py` — все тесты должны проходить

### Task 4: Обновить `_main_unsafe` под новую сигнатуру `render_output`

**Files:**

- Modify: `status_line.py` (функция `_main_unsafe`)
- Modify: `tests/test_main_integration.py`

- [ ] в `_main_unsafe` распаковать `cum_in / cum_out / cum_cache_read` из результата `compute_main_cum`
- [ ] в `_main_unsafe` заменить вызов `render_output(header, main_cum.get("total", 0), agents)` на `render_output(header, cum_in, cum_out, cum_cache_read, agents)`
- [ ] обновить `test_main_integration.py`: expected output под новый формат (header → заголовок таблицы → sum/main/agent lines); значения отформатированы через `format_tokens`
- [ ] запустить `python -m pytest tests/` — все тесты должны проходить

### Task 5: Обновить `CLAUDE.md` под новый формат

**Files:**

- Modify: `CLAUDE.md`

- [ ] в `CLAUDE.md` проверить, нет ли описания старого формата вывода. Текущий формат документирован в `status_line.py:render_output` docstring (status_line.py:766-779) и в `docs/plans/completed/20260824-status-line-tokens-aggregation.md` — не в CLAUDE.md
- [ ] в `CLAUDE.md` добавить короткую заметку в секцию Deviations log: «2026-08-24 — переход на табличный формат с breakdown in/out/cached. См. plan `20260824-token-breakdown-table.md`»
- [ ] убедиться, что описание cache-стратегии остаётся актуальным (ключи кэша те же, новое — field-presence check)

### Task 6: Верификация и финализация

- [ ] запустить `python -m pytest tests/` — все тесты проходят
- [ ] вручную (через `status_line.sh` на собственной сессии) проверить:
- заголовок таблицы `in / out / cached` появляется сразу после строки сессии
- три колонки выровнены по правому краю под максимальную ширину
- значения отформатированы через `format_tokens` (1k, 500, 2k, 1.2M)
- run-агенты показывают текущие значения (не нули)
- агенты со всеми нулями всё равно отображаются
- если есть живой кэш от старой версии — убедиться, что после первого запуска breakdown-поля появились (а не три нуля из-за stale cache hit)
- [ ] удалить устаревшие артефакты: проверить, что `total` нигде не остался в коде/тестах, `tokens` нигде не остался в коде/тестах
- [ ] перенести план в `docs/plans/completed/`

## Post-Completion

_Элементы, требующие ручного вмешательства — чекбоксы не нужны, информационно_

**Manual smoke-test** (на собственной машине):

- Запустить `status_line.sh` в реальном Claude Code-сеансе с несколькими агентами в разных состояниях (ok / run / err / stop).
- Убедиться визуально: header таблицы появляется, колонки выровнены, run-агенты показывают текущие значения, агенты со всеми нулями отображаются.
- Особенно проверить сессии с **предсуществующим** `data/agents_<sid>.json` от старой версии — после первого запуска breakdown-поля должны заполниться корректно (благодаря field-presence check в cache-hit). Если видим три нуля — значит upgrade-check не сработал.

**Rollout** (follow-up, отдельный шаг):

- `status_line.py` — user-level конфиг (живёт в `~/.claude/status_line/` на каждой машине пользователя). Никаких env / staging / prod / PMS-пропертей / компонентов calls-инвентаря — этот код не про calls/mediasoup.
- Релиз = `git pull` (или ручное копирование) файла на каждую машину. Никаких CI/CD-джобов или деплоя на серверы не требуется.
- Поведение кэша при апгрейде: на сессиях с предсуществующим `data/agents_<sid>.json` первый вызов после апгрейда делает один лишний forward-скан jsonl (cache miss из-за field-presence check). Это безопасно и однократно — после первого вызова кэш обновляется с breakdown-полями и далее работает как обычно.
