# Subagent status detection via main-log queue-operation task-notifications

## Overview

`status_line.py` показывает `[run]` для subagent-ов, которые в main log уже
завершены. Root cause: `detect_status` смотрит только на последний event в
jsonl агента; когда агент убивается посреди `tool_use`, финальный `end_turn`
не записывается — последним event-ом на диске остаётся `tool_use`, и статус
попадает в default-ветку `run`.

В main log есть более надёжный сигнал: `queue-operation` event с
`<task-notification>` content, где явно есть `<status>completed</status>` /
`<status>killed</status>` / `<status>failed</status>` и `<task-id>...</task-id>`,
совпадающий с именем subagent-файла (без префикса `agent-`).

Решение: распарсить task-notification-ы в `_scan_main_jsonl` (одна и та же
forward-pass), сохранить в кеше `main_<sid>.json` с ключом
`(last_uuid, mtime_jsonl)`, и в оркестраторе перезаписать `status` каждого
агента по `<task-id>`-ключу — **но не ранее определённых `err`/`stop`**.

Новый статус `[kill]` рендерится наряду с `[ok]`, `[err]`, `[stop]`, `[run]`.

## Context (from discovery)

- **`~/.claude/status_line/status_line.py`** — runtime, ~970 строк. Главные
  точки изменения:
  - `_scan_main_jsonl` (status_line.py:342) — делает forward scan main jsonl,
    возвращает 6-tuple. **Место для нового extractor-а**, return расширяется
    до 7-tuple.
  - `_EMPTY_MAIN_RESULT` (status_line.py:436) — fallback для пустого jsonl.
    Добавить `"task_notifications": {}`.
  - `compute_main_cum` (status_line.py:447) — обёртка над `_scan_main_jsonl`
    с cache-логикой. Возвращает dict; нужно добавить `task_notifications`
    поле И расширить cache-key до `(last_uuid, mtime_jsonl)`.
  - `_compute_agents` (status_line.py:898) — место для orchestrator-level
    override. **Должен уважать err/stop приоритет** (см. Critical Fix #1).
  - `_main_unsafe` (status_line.py:930) — точка передачи `task_notifications`
    в `_compute_agents`.
  - `_STATUSES` (status_line.py:762) — tuple разрешённых статусов в
    `render_output`. Расширяем до 5.
- **Текущая приоритетная цепочка** (CLAUDE.md "Status priority and overrides"):
  `err` (api error) > `stop` (interrupt) > `ok` (end_turn) > `run` (default).
  Новая ветка "queue override" вставляется **между `stop` и `ok`** (см.
  Solution Overview).
- **Сигнал подтверждён на живой сессии** `69a0a582-ca6b-49e6-9178-42b7da57061f`
  (agent-adea4908385e28099 и agent-a27ffc0a5e9a517bd — оба имеют
  `<status>completed</status>` в main log, но рендерятся как `[run]`).
- **Cache-invalidation важна** (см. Solution Overview): queue-events могут
  дописываться в main jsonl без нового assistant event-а, поэтому кеш по
  `last_uuid` НЕ инвалидируется при таких изменениях. Расширяем ключ.
- **Custom rules**: testing — часть плана, rollout — follow-up. Для user-side
  скрипта отдельных env/component-ов нет, PMS-проперти не задаём.
- **Зависимости**: только stdlib (`re` нужно добавить к существующим `json`,
  `os`, `subprocess`, `sys`, `time`, `pathlib`).

## Development Approach

- **testing approach**: TDD. Сначала фикстуры + failing тесты, потом
  implementation. Маленькие фокусированные изменения; `status_line.py`
  остаётся единственным production-файлом.
- Каждый task завершать полностью (тесты → код → прогон тестов) до перехода к
  следующему. Никаких пропусков.
- **Каждый task ОБЯЗАН содержать тесты для нового/изменённого кода** — это
  не опционально.
- Все тесты должны проходить до старта следующего task. Без исключений.
- **Обновлять этот план при изменении scope** в процессе работы.
- Сохранять backwards compatibility: агенты без queue-notification ведут себя
  ровно как раньше.
- **Git workflow**: репа `status_line/` активна (branch `status-line-tokens-aggregation`),
  plan-файл коммитится через стандартный git workflow. Production `~/.claude/status_line/`
  не в git — копируется после merge.

## Testing Strategy

- **unit-тесты** (`tests/test_*.py`):
  - `test_compute_main_cum.py` — extractor (mapping, last-wins, skip rules),
    cache invalidation при изменении mtime_jsonl
  - `test_compute_agent_snapshot.py` — priority (queue vs jsonl-based,
    prefix-strip), guard (queue не перезаписывает err/stop)
  - `test_render_output.py` — `[kill]` рендерится
  - `test_detect_status.py` — БЕЗ изменений (detect_status остаётся чистой
    функцией 4-х статусов; `kill` появляется только в orchestrator override)
- **Фикстуры** (`tests/fixtures/`):
  - `main_with_queue_ops.jsonl` — main jsonl с queue-operation events
  - `main_with_duplicate_task_id.jsonl` — для last-wins
  - `main_with_missing_tags.jsonl` — для skip
  - `agent_killed_in_tool_use.jsonl` + `.meta.json` — последний event =
    `tool_use`, toolUseId матчится queue-notification
  - `agent_completed_after_tool_use.jsonl` + `.meta.json` — последний event =
    `end_turn` (для теста priority/guard)
  - `agent_err_in_tool_use.jsonl` + `.meta.json` — последний event =
    `tool_use` с `apiErrorStatus` (для теста guard)
- **Интеграционные тесты** (`tests/test_main_integration.py`):
  - End-to-end: агент с jsonl `tool_use` + queue `killed` → рендерится `[kill]`
  - End-to-end: агент с jsonl `end_turn` + queue `completed` → рендерится `[ok]`
  - End-to-end: агент с api error + queue `completed` → рендерится `[err]`
    (guard работает)
  - Existing real_session фикстура (`f5044e4f-...`) — **инспекционный тест**
    подтверждает, что в ней нет subagent queue-events (иначе сломаются
    existing assertions).
- **Ручная проверка** (Post-Completion): запустить Claude Code в свежей
  сессии, вызвать `Agent` tool 2-3 раза, дождаться `[ok]` / `[kill]`.

## Progress Tracking

- Отмечать выполненное `[x]` сразу после завершения.
- Новые задачи добавлять с префиксом ➕, блокеры — ⚠️.
- План синхронизировать с фактической работой.

## Solution Overview

### Архитектура

```
main jsonl
  └─ _scan_main_jsonl (forward pass, всегда выполняется)
       ├─ existing: cum tokens, tool_use_positions, last_uuid
       └─ NEW: task_notifications: dict[<task-id>, status]
              (status ∈ {"ok", "kill", "err"}; last wins)
                    ↓
compute_main_cum
  └─ cache в main_<sid>.json по (last_uuid, mtime_jsonl)
       ├─ mtime_jsonl в ключе → новые queue-events без новых
       │   assistant-event-ов инвалидируют кеш
       └─ last_uuid по-прежнему ловит новые assistant-event-ы
                    ↓
_main_unsafe
  └─ main_cum.get("task_notifications", {})
                    ↓
_compute_agents (override loop)
  └─ для каждого агента:
       key = agentId.removeprefix("agent-")
       if key in task_notifications AND a["status"] not in ("err", "stop"):
           a["status"] = task_notifications[key]
                    ↓
sort_agents + render_output
```

### Финальная приоритетная цепочка (CLAUDE.md deviation log)

```
1. err    — _is_assistant_error (api error markers)   ← НЕ перезаписывается queue-override-ом
2. stop   — stoppedByUser OR user interrupt marker    ← НЕ перезаписывается queue-override-ом
3. queue  — task_notifications[stem], если есть         ← НОВОЕ
4. ok     — end_turn в jsonl агента                     ← fallback
5. run    — default (mid-flow, no assistant events)     ← fallback
```

### Cache key change: `(last_uuid, mtime_jsonl)`

**Зачем:** queue-events могут дописываться в main jsonl без нового assistant
event-а (subagent завершился пока main idle). В таком случае `last_uuid` не
меняется, но файл на диске изменился (mtime тоже). Без `mtime_jsonl` в ключе
кеш продолжает возвращать устаревший `task_notifications` dict — и фикс
не работает в именно том сценарии, под который проектируется.

**Стратегия:**

- На read: stat-ить main jsonl, читать `(last_uuid, mtime_jsonl)` из кеша,
  сравнивать с current.
- На write: записывать оба значения в `main_<sid>.json`.
- Файл `last_uuid` остаётся единственным source of truth для token totals
  (новый assistant event ⇒ новый last_uuid ⇒ пересчёт токенов).

**Стоимость:** один stat() на каждый вызов хука. Negligible.

### Почему override в оркестраторе, а не в compute_agent_snapshot

- `compute_agent_snapshot` остаётся pure-function с одним источником правды
  (jsonl + meta); проще тестировать, легче кешировать.
- Queue-сигнал приходит из main jsonl, который сканируется в
  `compute_main_cum`. Агенту не нужно знать про main — он знает только свой
  jsonl.
- Кеш агентов (`agents_<sid>.json`) остаётся без изменений схемы:
  `task_notifications` уже закеширован в `main_<sid>.json` по
  `(last_uuid, mtime_jsonl)`.

### Override guard (Critical Fix #1)

`compute_agent_snapshot` уже мог вернуть `err` (api error) или `stop` (user
interrupt) — это более авторитетные сигналы, чем queue-notification
(который может прийти после или с задержкой). Override **не должен** их
перезаписывать:

```python
if key in task_notifications and a["status"] not in ("err", "stop"):
    a["status"] = task_notifications[key]
```

Task 4 содержит регрессионные тесты: queue `completed` + agent jsonl с
`apiErrorStatus` → `err` сохраняется; queue `completed` + meta
`stoppedByUser=true` → `stop` сохраняется.

### Tokens при `run` → `kill` транзите

Текущий `compute_agent_snapshot` ставит `tokens=None` если `status=="run"`.
При override `run` → `kill` (или `ok`), `tokens` остаётся `None` — рендер
покажет `[kill]` без token-колонки.

**Решение:** оставляем как есть. `[kill]` без токенов семантически означает
"агент был убит до того, как успели посчитать стоимость". Это consistent с
`[run]` (mid-flow без токенов). Пользовательский фикс — отдельная задача
(если понадобится): вернуть `tokens` независимо от статуса. YAGNI сейчас.

## Technical Details

### Extractor внутри `_scan_main_jsonl`

```python
import re  # добавить к существующим импортам

_TASK_ID_RE = re.compile(r"<task-id>([^<]+)</task-id>")
_STATUS_RE  = re.compile(r"<status>([^<]+)</status>")
_QUEUE_STATUS_MAP = {"completed": "ok", "killed": "kill", "failed": "err"}
```

В forward-loop (после блока assistant-event):

```python
if event.get("type") == "queue-operation":
    if event.get("operation") == "enqueue":
        content = event.get("content")
        if isinstance(content, str):
            m_id = _TASK_ID_RE.search(content)
            m_status = _STATUS_RE.search(content)
            if m_id and m_status:
                mapped = _QUEUE_STATUS_MAP.get(m_status.group(1))
                if mapped:
                    task_notifications[m_id.group(1)] = mapped
```

`_scan_main_jsonl` теперь возвращает 7-tuple вместо 6.

### Изменения в `_EMPTY_MAIN_RESULT`

Добавить `"task_notifications": {}`.

### Изменения в `compute_main_cum` (cache key + new field)

1. Stat-ить main jsonl: `mtime = jsonl_path.stat().st_mtime` (или через
   существующий `_jsonl_mtime(jsonl_path)`).
2. Распаковать 7-tuple из `_scan_main_jsonl`.
3. Расширить cache-hit check: `cache.get("last_uuid") == last_uuid AND
cache.get("mtime_jsonl") == mtime`.
4. Добавить `"mtime_jsonl": mtime` и `"task_notifications":
task_notifications` в result dict.

```python
mtime = _jsonl_mtime(jsonl_path)  # 0.0 если файл отсутствует
cum_in, cum_out, cum_cache_create, cum_cache_read, positions, last_uuid, task_notifications = (
    _scan_main_jsonl(jsonl_path)
)

if (
    cache is not None
    and last_uuid
    and cache.get("last_uuid") == last_uuid
    and cache.get("mtime_jsonl") == mtime
):
    return cache

result = {
    "cum_in": cum_in,
    "cum_out": cum_out,
    "cum_cache_create": cum_cache_create,
    "cum_cache_read": cum_cache_read,
    "total": cum_in + cum_out + cum_cache_create + cum_cache_read,
    "last_uuid": last_uuid,
    "mtime_jsonl": mtime,                   # NEW: cache key part
    "tool_use_positions": positions,
    "task_notifications": task_notifications,  # NEW: extracted data
}
```

### Изменения в `_compute_agents`

Принимает `task_notifications: dict[str, str]` третьим параметром. После
построения всех snapshots — override loop **с guard**:

```python
for a in agents:
    aid = a["agentId"]
    key = aid[len("agent-"):] if aid.startswith("agent-") else aid
    if key in task_notifications and a["status"] not in ("err", "stop"):
        a["status"] = task_notifications[key]
```

### Изменения в `_main_unsafe`

После `compute_main_cum`:

```python
task_notifications = main_cum.get("task_notifications", {})
agents = _compute_agents(session_dir, agents_cache, task_notifications)
```

### Изменения в `_STATUSES`

```python
_STATUSES = ("ok", "run", "err", "stop", "kill")
```

`render_output` уже делает `f"[{status}]" if status in _STATUSES else "[?]"`,
поэтому рендеринг `[kill]` работает автоматически.

### Inline `[deviation]` маркер в `_compute_agents`

Добавить комментарий над override loop:

```python
# [deviation] Override живёт здесь, а не в compute_agent_snapshot, потому
# что queue-сигнал приходит из main jsonl (другой файл), а не из agent
# jsonl + meta. compute_agent_snapshot остаётся pure-function с одним
# источником правды (jsonl + meta). Guard "не перезаписывать err/stop"
# реализует приоритет, описанный в CLAUDE.md "Status priority and overrides".
```

### Изменения в module-level docstring

`detect_status returns one of {"err", "stop", "ok", "run"}` — **без
изменений** (detect_status остаётся чистой 4-статусной функцией).

Добавить отдельную строку:

> The orchestrator override in `_compute_agents` may additionally set
> `status='kill'` when a main-log queue-operation task-notification with
> `<status>killed</status>` is present and the agent's detected status from
> `compute_agent_snapshot` is not `err` or `stop`.

### Edge cases

| Case                                                                        | Поведение                                    |
| --------------------------------------------------------------------------- | -------------------------------------------- |
| `operation == "dequeue"` / `"remove"`                                       | content отсутствует, пропуск                 |
| content не строка (None, list)                                              | пропуск (defensive)                          |
| Нет `<task-id>` или `<status>`                                              | пропуск (не пишем в dict)                    |
| Unknown status ("running", "pending")                                       | не в `_QUEUE_STATUS_MAP`, пропуск            |
| Повтор `<task-id>` (resume scenario)                                        | last-wins — перезаписываем                   |
| Partial line при записи                                                     | `json.loads` падает, line skipped (existing) |
| Background bash `<task-id>` (`by3quq7xj`) vs subagent (`adea4908385e28099`) | разные namespace, не пересекаются            |
| queue `killed` + jsonl `end_turn` (clean finish, потом killed)              | override `ok` → `kill` (queue более поздний) |
| run → kill (через queue override)                                           | `tokens=None` остаётся (consistent с run)    |

## What Goes Where

- **Implementation Steps** (`[ ]` checkboxes) — код + тесты в этой репе.
- **Post-Completion** — ручная проверка на живом Claude Code, обновление
  CLAUDE.md / отметка deviations.

## Implementation Steps

### Task 1: Add task-notification extractor to `_scan_main_jsonl` + cache key extension

**Files:**

- Modify: `status_line.py` (импорты, `_scan_main_jsonl`, `_EMPTY_MAIN_RESULT`, `compute_main_cum`)
- Create: `tests/fixtures/main_with_queue_ops.jsonl`
- Create: `tests/fixtures/main_with_duplicate_task_id.jsonl`
- Create: `tests/fixtures/main_with_missing_tags.jsonl`
- Modify: `tests/test_compute_main_cum.py`

- [ ] создать `tests/fixtures/main_with_queue_ops.jsonl` с минимум 5 событиями:
      один assistant event (для last_uuid), плюс 4 queue-operation events:
      `enqueue` с `completed`, `enqueue` с `killed`, `enqueue` с `failed`,
      `dequeue` без content, `enqueue` с unknown status (`"running"`)
- [ ] создать `tests/fixtures/main_with_duplicate_task_id.jsonl` — два
      queue-operation events с одним `<task-id>`, разными `<status>` (для
      проверки last-wins)
- [ ] создать `tests/fixtures/main_with_missing_tags.jsonl` — queue-operation
      event с `<task-id>`, но без `<status>` (для проверки skip)
- [ ] write test: `compute_main_cum` возвращает `task_notifications` dict с
      корректным маппингом (`completed`→`"ok"`, `killed`→`"kill"`, `failed`→`"err"`)
- [ ] write test: last-wins — последний queue-event с тем же task-id
      перезаписывает предыдущий
- [ ] write test: unknown status (например `"running"`) не попадает в dict
- [ ] write test: queue-event без `<task-id>` или без `<status>` не
      попадает в dict (skip)
- [ ] write test: `dequeue` operation без content — dict пустой
- [ ] write test: `main_<sid>.json` cache HIT возвращает кешированный
      `task_notifications` (sentinel-значение вроде `{"sentinel-agent":
    "ok"}` должно пережить cache hit без потери)
- [ ] write test: cache invalidation при изменении mtime_jsonl — pre-write
      cache с пустым `task_notifications`, дописать queue-event в main jsonl
      (touching mtime), вызвать `compute_main_cum`, assert новый `task_notifications`
      подхвачен
- [ ] write test: cache invalidation при изменении last_uuid — новый
      assistant event должен инвалидировать кеш (existing behavior, проверить
      что не сломался)
- [ ] write test: cache HIT сохраняет `mtime_jsonl` поле в возвращаемом dict
      (для downstream consumers если понадобится)
- [ ] write test: пустой main jsonl → `_EMPTY_MAIN_RESULT["task_notifications"]`
      равен `{}`, `mtime_jsonl == 0.0` (не падает на KeyError)
- [ ] write test: `main_<sid>.json` atomic write содержит оба новых поля
      (`mtime_jsonl`, `task_notifications`)
- [ ] реализовать: добавить `import re` и константы `_TASK_ID_RE`,
      `_STATUS_RE`, `_QUEUE_STATUS_MAP` в `status_line.py`
- [ ] реализовать: расширить `_scan_main_jsonl` до 7-tuple, добавить
      extractor-блок в forward loop
- [ ] реализовать: расширить `_EMPTY_MAIN_RESULT["task_notifications"] = {}`,
      добавить `"mtime_jsonl": 0.0`
- [ ] реализовать: в `compute_main_cum` — stat-ить mtime, расширить cache-hit
      check до `(last_uuid, mtime_jsonl)`, добавить оба поля в result dict
- [ ] run tests — все должны проходить до перехода к Task 2

### Task 2: Extend `_STATUSES` to include `[kill]`

**Files:**

- Modify: `status_line.py` (константа `_STATUSES`, module docstring)
- Modify: `tests/test_render_output.py`

- [ ] write test: `render_output` рендерит `[kill]` для статуса `"kill"`
- [ ] write test: неизвестный статус (например `"weird"`) рендерится как `[?]`
      (existing behavior, проверить что не сломалось)
- [ ] write test: `_STATUSES` tuple содержит `"kill"` (все 5 значений)
- [ ] реализовать: расширить `_STATUSES = ("ok", "run", "err", "stop", "kill")`
- [ ] реализовать: обновить module docstring — добавить note про
      orchestrator override и `kill` (НЕ про `compute_agent_snapshot`)
- [ ] run tests — все должны проходить до перехода к Task 3

### Task 3: Wire orchestrator-level override in `_compute_agents` (с guard)

**Files:**

- Modify: `status_line.py` (`_compute_agents`, `_main_unsafe`)
- Create: `tests/fixtures/agent_killed_in_tool_use.jsonl`
- Create: `tests/fixtures/agent_killed_in_tool_use.meta.json`
- Create: `tests/fixtures/agent_completed_after_tool_use.jsonl`
- Create: `tests/fixtures/agent_completed_after_tool_use.meta.json`
- Create: `tests/fixtures/agent_err_in_tool_use.jsonl`
- Create: `tests/fixtures/agent_err_in_tool_use.meta.json`
- Modify: `tests/test_compute_agent_snapshot.py`

- [ ] создать `tests/fixtures/agent_killed_in_tool_use.jsonl` — два event-а:
      user prompt + assistant с `tool_use` (без end_turn)
- [ ] создать `tests/fixtures/agent_killed_in_tool_use.meta.json` —
      `{"agentType": "general-purpose", "description": "Task 1: test",
"toolUseId": "call_function_xxx_1"}`
- [ ] создать `tests/fixtures/agent_completed_after_tool_use.jsonl` —
      user + assistant tool_use + user tool_result + assistant end_turn
- [ ] создать `tests/fixtures/agent_completed_after_tool_use.meta.json` —
      аналогично предыдущему
- [ ] создать `tests/fixtures/agent_err_in_tool_use.jsonl` — user + assistant
      с `tool_use` И `apiErrorStatus: 429` (или `isApiErrorMessage: true`)
- [ ] создать `tests/fixtures/agent_err_in_tool_use.meta.json` — аналогично
- [ ] write test: `compute_agent_snapshot` БЕЗ task_notifications — поведение
      не изменилось (backwards-compat smoke test, использует существующую
      фикстуру agent_running.jsonl)
- [ ] реализовать: добавить параметр `task_notifications: dict[str, str] | None = None`
      в `_compute_agents`, применить override loop **с guard** после построения
      snapshots
- [ ] реализовать: добавить inline `[deviation]` комментарий над override
      loop (см. Technical Details)
- [ ] реализовать: в `_main_unsafe` распарсить `task_notifications` из
      `main_cum` и передать в `_compute_agents`
- [ ] run tests — должны проходить (override пока ни на что не влияет,
      dict всегда пустой)

### Task 4: Tests for priority semantics (queue vs jsonl-based, включая guard)

**Files:**

- Modify: `tests/test_compute_agent_snapshot.py`
- Modify: `tests/test_main_integration.py`
- Create: `tests/fixtures/main_with_mixed_queue_ops.jsonl` (если нужен)

- [ ] write test: queue `killed` + jsonl last event `tool_use` →
      итоговый status `"kill"`
- [ ] write test: queue `completed` + jsonl last event `end_turn` →
      итоговый status `"ok"` (queue не downgrades clean end_turn)
- [ ] write test: queue `completed` + jsonl last event с API error →
      итоговый status `"err"` (queue НЕ перезаписывает api error — **guard**)
- [ ] write test: queue `completed` + meta `stoppedByUser=true` →
      итоговый status `"stop"` (queue НЕ перезаписывает user interrupt — **guard**)
- [ ] write test: agent stem `"agent-XYZ"`, queue key `"XYZ"` → match
      работает (prefix-strip)
- [ ] write test: queue signal отсутствует → fallback к jsonl-based логике
      (используя существующую фикстуру agent_running.jsonl, передаём
      `task_notifications={}` — должно дать `"run"`)
- [ ] реализовать: (если нужно) скорректировать guard или override logic —
      **только если тесты упали**; baseline уже должен проходить
- [ ] run tests — все должны проходить до перехода к Task 5

### Task 5: Integration tests + final verification

**Files:**

- Modify: `tests/test_main_integration.py`
- Create: integration fixtures если нужны

- [ ] **inspection test**: проверить, что `tests/fixtures/real_session/`
      НЕ содержит subagent queue-events (assert: `queue-operation` events
      с `<task-id>` совпадающим с любым `agent-*.meta.json` stem — count == 0).
      Если count > 0 — пометить ⚠️ и решить до продолжения (обновить
      existing assertions или изолировать real_session для другого test).
- [ ] создать синтетическую фикстуру сессии с одним subagent-ом:
  - `<sid>.jsonl` с queue-operation event + assistant event
  - `<sid>/subagents/agent-XYZ.{jsonl,meta.json}` с toolUseId матчащимся
    queue-event-у, last event в jsonl = tool_use (без end_turn)
- [ ] write test: end-to-end — agent jsonl `tool_use` + queue `killed` →
      строка вывода содержит `[kill]` и описание агента
- [ ] write test: end-to-end — agent jsonl `end_turn` + queue `completed` →
      строка вывода содержит `[ok]`
- [ ] write test: end-to-end — agent с api error + queue `completed` →
      строка вывода содержит `[err]` (guard работает end-to-end)
- [ ] write test: end-to-end — agent без queue-event → поведение как раньше
      (regression check)
- [ ] run полный test suite: `pytest tests/ -v`
- [ ] verify все existing тесты по-прежнему проходят (real_session fixture
      не должен сломаться)
- [ ] verify test coverage на новые branches extractor-а

### Task 6: Update documentation + move plan

**Files:**

- Modify: `CLAUDE.md`
- Move: `docs/plans/20260824-subagent-status-via-queue-notifications.md` →
  `docs/plans/completed/`

- [ ] обновить `CLAUDE.md` раздел "Status priority and overrides" — добавить
      новый приоритет "queue operation (main log)" **между `stop` и `ok`**,
      описать join key (`<task-id>` ↔ agent stem), отметить guard
      (не перезаписывает err/stop)
- [ ] обновить `CLAUDE.md` "Cache invalidation strategy" — отметить, что
      `main_<sid>.json` теперь ключуется по `(last_uuid, mtime_jsonl)`
      для инвалидации при queue-event-ах без новых assistant event-ов
- [ ] обновить `CLAUDE.md` "Module layout" — отметить что `_STATUSES`
      теперь включает `kill`
- [ ] добавить inline `[deviation]` маркер в `status_line.py` над override
      loop (если не добавлен в Task 3)
- [ ] переместить план в `docs/plans/completed/`
- [ ] commit изменения с message вида `feat(status_line): detect subagent
    completion via main-log queue-operation task-notifications`

## Post-Completion

_Items requiring manual intervention — no checkboxes, informational only_

**Ручная проверка:**

- Запустить Claude Code в свежей сессии
- Вызвать `Agent` tool 2-3 раза с разными prompt-ами
- Дождаться завершения всех агентов
- Проверить в status line:
  - Агент, успешно завершившийся с `tool_use` посередине → `[ok]`
  - Агент, убитый через Ctrl+C или завершённый по таймауту → `[kill]`
  - Агент, запущенный в текущий момент → `[run]`
- Если `[kill]` появляется для явно mid-flow агентов (более 60с простоя
  без завершения) — это сигнал на откат override-блока в `_compute_agents`

**External system updates:**

- Нет. Это user-side скрипт; никакие consuming projects не затронуты.

**Follow-up rollout (отдельно от плана):**

- Скопировать `status_line.py` в `~/.claude/status_line/status_line.py`
- Изменения применяются автоматически при следующем вызове status-line хука
- Никакой PMS-проперти, никакой CI-джоб, никакой staged rollout
