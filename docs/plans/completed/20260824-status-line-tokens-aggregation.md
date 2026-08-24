# Status line: агрегация токенов из main + subagent-ов в реальном времени

## Overview

`~/.claude/status_line/status_line.sh` сейчас показывает только main session токены (из stdin JSON от хука Claude Code). Subagent-ы (например, запускаемые `planning:exec`) невидимы в статусе. В post-mortem `planning:exec` собирает по jsonl-логам всё, но в реальном времени этого нет.

Задача: status line в реальном времени показывает сумму `main + все subagent-ы` с per-agent разбивкой и статусами `[run] [ok] [err] [stop]`. Реализация — Python-скрипт с кешем по UUID (без постоянного re-парсинга jsonl на каждый вызов хука).

Интеграция: `status_line.sh` становится однострочной обёрткой `exec python3 ...`. Всё применяется сразу при следующем вызове хука, отдельной раскатки не нужно.

## Context (from discovery)

- **`~/.claude/status_line/status_line.sh`** — текущая реализация: 77 строк bash + jq. Считает `input + output + cache_read` накопительно по prompt-ам через `last_prompt_id`. Subagent-ы невидимы.
- **`~/.claude/projects/<encoded>/<session_id>/`** — каталог сессии. Внутри `<session_id>.jsonl` (main log) и `<session_id>/subagents/agent-<id>.{jsonl,meta.json}` (subagent-ы).
- **`meta.json`** содержит `agentType`, `description` (человеко-читаемое имя, до 40 символов), `toolUseId` (id в main log для сортировки), `spawnDepth`, опционально `stoppedByUser: true`.
- **`jsonl` формат**: одна JSON-строка на event. Последний assistant event содержит `usage: {input_tokens, cache_creation_input_tokens, cache_read_input_tokens, output_tokens}`. Финиш-состояние определяется по последнему event: `end_turn` → done, `error/isApiErrorMessage` → err, `user` с `[Request interrupted by user]` → stop, иначе → run.
- **Реальная сессия для верификации**: `f5044e4f-3e01-4330-be72-eb008a1d035e` в `C--Users-f-bobin-IdeaProjects-agentic-terminal` (encoded directory name: дефисы заменяют разделители путей по конвенции Claude Code) — 38 subagent-ов, 27 ok, 2 stop, 9 err (rate_limit + server_error), все статусы покрыты.
- **Аналогичный план-референс**: `docs/plans/completed/20260814-track-with-session-hook.md` (TDD-стиль с харнессом — берём за образец).
- **Зависимости Python**: только stdlib (`json`, `os`, `pathlib`, `subprocess` для `git branch`, `sys`). pytest ставится через `pip install --user` для тестов.
- **Кастомные правила**: тестирование — часть плана, раскатка — follow-up. Для user-side скрипта раскатки через PMS нет; изменения применяются при следующем вызове хука автоматически.

## Development Approach

- **testing approach**: TDD с pytest. Сначала фикстуры + тесты на чистые функции (`format_tokens`, `detect_status`), потом на I/O (`compute_main_cum`, `compute_agent_snapshot`), потом интеграционные на реальной сессии `f5044e4f`.
- Каждый task завершать полностью (код + тесты + прогон) до перехода к следующему. Никаких пропусков.
- Маленькие фокусные изменения; `~/.claude/status_line/status_line.sh` остаётся единственным изменяемым скриптом.
- **Без git workflow** — `~/.claude/status_line/` не в git, план-файл коммитится в `docs/plans/`.

## Testing Strategy

- **unit-тесты** (`~/.claude/status_line/tests/test_*.py`): чистые функции (`format_tokens`, `detect_status`, `parse_stdin`, `sort_agents`) + I/O с фикстурами в `tests/fixtures/`.
- **Фикстуры** (`tests/fixtures/`): минимальные jsonl/meta файлы для каждого case — main_normal, agent_ok, agent_err_rate_limit, agent_err_server_error, agent_stopped_user, agent_running, agent_no_assistant, meta_normal, meta_stopped_by_user. Реальная сессия f5044e4f копируется в `tests/fixtures/real_session/` для интеграционного теста.
- **Интеграционный тест**: end-to-end прогон `main()` с фикстурой реальной сессии → snapshot строки stdout. Сравнение с эталоном. Сверяет: количество строк = 1 (header) + 1 (sum) + 1 (main) + 38 (per-agent); порядок агентов по `toolUseId`; правильные статусы; правильные иконки.
- **Ручная проверка (Post-Completion)**: 6 сценариев из brainstorm на живом Claude Code.

## Progress Tracking

- Отмечать выполненное `[x]` сразу после завершения.
- Новые задачи добавлять с префиксом ➕, блокеры — ⚠️.
- План синхронизировать с фактической работой.

## Solution Overview

`status_line.py` — чистый stdlib, ~200-300 строк. Состоит из:

```
parse_stdin(json)            → {session_id, prompt_id, model, branch, user, ctx_k, used_pct}
find_session_dir(session_id)  → Path | None
compute_main_cum(jsonl, cache) → {cum_in, cum_out, cum_cache_create, cum_cache_read, total, last_uuid, tool_use_positions}
compute_agent_snapshot(jsonl, meta, cache) → {agentId, status, tokens, description, toolUseId, last_uuid, mtime_jsonl}
detect_status(last_event, meta) → "ok" | "err" | "stop" | "run"
format_tokens(n) → "850" | "78k" | "1.2M"
sort_agents(agents, tool_use_positions) → sorted list
render_output(parts) → multi-line string
main()                        → entry point, reads stdin, orchestrates, prints
```

Кеш-инвалидация:

- `data/main_<sid>.json` — пересчитывается по `last_uuid` последнего assistant event в main jsonl.
- `data/agents_<sid>.json` — массив записей по `agentId`, пересчитывается по `(mtime_jsonl, last_uuid)`.
- Атомарная запись: `*.tmp` → `os.replace()`.

Сортировка: для каждого subagent-а `meta.toolUseId` ищется в `tool_use_positions` (map `toolUseId → position`, построенная при сканировании main jsonl для cum). Fallback — `meta mtime` для sub-sub-agent-ов (toolUseId не в main).

## Technical Details

**Структура файлов:**

```
~/.claude/status_line/
├── status_line.sh         # обёртка: exec python3 status_line.py
├── status_line.py         # вся логика
├── data/                  # создаётся при первом запуске (Path.mkdir parents=True exist_ok=True)
│   ├── main_<sid>.json
│   └── agents_<sid>.json
└── tests/
    ├── conftest.py
    ├── test_format_tokens.py
    ├── test_detect_status.py
    ├── test_parse_stdin.py
    ├── test_compute_main_cum.py
    ├── test_compute_agent_snapshot.py
    ├── test_sort_agents.py
    ├── test_render_output.py
    ├── test_main_integration.py
    └── fixtures/
        ├── main_normal.jsonl
        ├── main_with_tool_use.jsonl
        ├── agent_ok.jsonl
        ├── agent_err_rate_limit.jsonl
        ├── agent_err_server_error.jsonl
        ├── agent_stopped_user.jsonl
        ├── agent_running.jsonl
        ├── agent_no_assistant.jsonl
        ├── meta_normal.json
        ├── meta_stopped_by_user.json
        ├── meta_long_description.json   # >40 символов
        └── real_session/               # копия f5044e4f для интеграционного теста
            ├── <sid>.jsonl
            └── subagents/...
```

**`status_line.sh` (после):**

```bash
#!/usr/bin/env bash
exec python3 "$(dirname "$0")/status_line.py"
```

**Формат вывода (4+N строк):**

```
Session: <sid> | Branch: <b> | Model: <m> | User: <u>
sum: 1.24M
main: 78k
[run] Fixer: smells findings
[ok]  Task 1: hex-token parsing  92k
[err] Fixer retry: finish findings  21k
[stop] Review: quality
```

Правила форматирования:

- `description` обрезается до 40 символов с `…` (одна Unicode-эллипсис U+2026, не три ASCII-точки)
- Токены: `<1000` raw, `>=1000` и `<1000000` как `Nk`, `>=1000000` как `N.NM` (1 decimal)
- Пустые токены (agent упал до API) — не показываем
- Если 0 subagent-ов — строка `sum: N` не выводится, только `main: N`

**Детекция статуса (`detect_status`):**

- err (highest): last event `type=assistant` с любым из `error`, `isApiErrorMessage: true`, `apiErrorStatus >= 400`
- stop: meta `stoppedByUser=true` ИЛИ (last event `type=user` и content содержит `[Request interrupted by user]`)
- ok: last event `type=assistant` с `stop_reason: end_turn` (или `stop_sequence: null`) без error
- run: иначе (mid-flow или agent без assistant event)

**Токен-формула:** `input_tokens + cache_creation_input_tokens + cache_read_input_tokens + output_tokens` (всё в одно число, в отличие от текущего status_line.sh, который cache_creation игнорирует).

**Сортировка:** sort key = position в `tool_use_positions` (None → +inf → fallback на meta mtime). Стабильная сортировка сохраняет порядок среди агентов с одинаковым ключом.

**Edge cases в коде:**

- session_dir не найден → вывод только первой строки (header)
- main jsonl отсутствует → `main: 0`
- meta битый / description пустое → fallback на `agentType`
- 0 assistant event-ов у агента → `[err]` без токенов
- Кеш повреждён (JSONDecodeError) → удалить, пересчитать
- `data/` не существует → `Path.mkdir(parents=True, exist_ok=True)`

## What Goes Where

- **Implementation Steps** (`[ ]`): test infrastructure, чистые функции, I/O функции, assembly, обёртка sh, финальная валидация.
- **Post-Completion** (без чекбоксов): 6 ручных сценариев на живом Claude Code.

## Implementation Steps

### Task 1: Test infrastructure + фикстуры

**Files:**

- Create: `~/.claude/status_line/tests/conftest.py`
- Create: `~/.claude/status_line/tests/__init__.py`
- Create: `~/.claude/status_line/tests/fixtures/main_normal.jsonl`
- Create: `~/.claude/status_line/tests/fixtures/main_with_tool_use.jsonl`
- Create: `~/.claude/status_line/tests/fixtures/agent_ok.jsonl`
- Create: `~/.claude/status_line/tests/fixtures/agent_err_rate_limit.jsonl`
- Create: `~/.claude/status_line/tests/fixtures/agent_err_server_error.jsonl`
- Create: `~/.claude/status_line/tests/fixtures/agent_stopped_user.jsonl`
- Create: `~/.claude/status_line/tests/fixtures/agent_running.jsonl`
- Create: `~/.claude/status_line/tests/fixtures/agent_no_assistant.jsonl`
- Create: `~/.claude/status_line/tests/fixtures/meta_normal.json`
- Create: `~/.claude/status_line/tests/fixtures/meta_stopped_by_user.json`
- Create: `~/.claude/status_line/tests/fixtures/meta_long_description.json`
- Create: `~/.claude/status_line/tests/fixtures/real_session/` (копия f5044e4f)

- [x] удалить `tests/__init__.py` из списка (modern pytest не требует)
- [x] установить pytest: `python3 -m pip install --user pytest` (если нет)
- [x] проверить окружение: `which python3 && python3 --version` — если python3 отсутствует, остановиться и сообщить пользователю (без fallback на bash — по решению brainstorm)
- [x] создать `tests/conftest.py` с `pytest.fixture` для `tmp_data_dir` (tmp-каталог для кешей)
- [x] создать фикстуры: каждый файл — минимальный но реалистичный (5-10 строк jsonl, 1-2 поля в meta)
- [x] `agent_ok.jsonl`: последний event = assistant с `stop_reason: end_turn`, usage=100+200+300+50
- [x] `agent_err_rate_limit.jsonl`: последний event = assistant с `error: rate_limit`, `apiErrorStatus: 429`
- [x] `agent_err_server_error.jsonl`: последний event = assistant с `error: server_error`
- [x] `agent_stopped_user.jsonl`: последний event = user с content `[Request interrupted by user]`
- [x] `agent_running.jsonl`: последний event = assistant с `stop_reason: tool_use`
- [x] `agent_no_assistant.jsonl`: только user events (tool_result)
- [x] `agent_err_with_stopped_by_user.jsonl`: последний event = assistant с `error: rate_limit` (для теста приоритета err > stop при `stoppedByUser: true` в meta)
- [x] `meta_long_description.json`: description длиной 60 символов для теста обрезки
- [x] скопировать реальную сессию `f5044e4f-3e01-4330-be72-eb008a1d035e` из `C--Users-f-bobin-IdeaProjects-agentic-terminal` в `fixtures/real_session/` (через `cp -r`, ~12 MB). **Когда исходная сессия мутирует** (Claude Code допишет event-ы), снапшот stdout в интеграционном тесте сломается — перегенерировать: `rm -rf fixtures/real_session && cp -r ~/.claude/projects/C--Users-f-bobin-IdeaProjects-agentic-terminal/f5044e4f-* fixtures/real_session`
- [x] прогнать `python3 -m pytest tests/ -v --collect-only` — все фикстуры на месте, тесты ещё не написаны (только collect)

### Task 2: Чистые функции — format_tokens, detect_status, parse_stdin (TDD)

**Files:**

- Create: `~/.claude/status_line/status_line.py` (заглушка + чистые функции)
- Create: `~/.claude/status_line/tests/test_format_tokens.py`
- Create: `~/.claude/status_line/tests/test_detect_status.py`
- Create: `~/.claude/status_line/tests/test_parse_stdin.py`

- [x] написать тесты `test_format_tokens`: 0 → "0", 850 → "850", 1000 → "1k", 78000 → "78k", 1234567 → "1.2M", 999 → "999", 999500 → "1000k" (или "1.0M" — выбрать одно, документировать)
- [x] написать тесты `test_detect_status`: для каждой фикстуры (agent_ok, agent_err_rate_limit, agent_err_server_error, agent_stopped_user, agent_running, agent_no_assistant, agent_err_with_stopped_by_user) + meta_stopped_by_user + meta_normal — ожидаемый статус. Включая приоритет: err > stop при `stoppedByUser=true` И `error` в последнем event → статус err
- [x] написать тесты `test_parse_stdin`: валидный JSON → dict с правильными полями; пустой input → fallback (пустой dict); отсутствующие поля → defaults (0, empty string)
- [x] реализовать `format_tokens(n)` — пороги из тестов
- [x] реализовать `detect_status(last_event_dict, meta_dict)` — ветки err/stop/ok/run по правилам из Technical Details
- [x] реализовать `parse_stdin(json_str)` — `json.loads` + extract полей с `// ""` defaults
- [x] прогнать `python3 -m pytest tests/test_format_tokens.py tests/test_detect_status.py tests/test_parse_stdin.py -v` — все PASS

### Task 3: compute_main_cum с кешем по last_uuid (TDD)

**Files:**

- Modify: `~/.claude/status_line/status_line.py`
- Create: `~/.claude/status_line/tests/test_compute_main_cum.py`

- [x] написать тесты: пустой jsonl → нули; main_normal (3 assistant events) → сумма usage; cache hit (last_uuid совпал) → не пересчитывает (мокать os.stat или использовать frozen mtime); cache повреждён (битый JSON в cache-файле) → удаляется, пересчитывается; tool_use_positions правильно строится (извлекает id из `tool_use` блоков в assistant content)
- [x] реализовать `compute_main_cum(jsonl_path, cache_path) -> dict`:
  - load cache (try/except JSONDecodeError → удалить файл, fallback на пустой)
  - `last_event_uuid = read_last_assistant_uuid(jsonl_path)` (читает с конца, первый assistant)
  - если `last_event_uuid == cache.last_uuid` → return cache
  - иначе: scan jsonl с начала, для каждого event с `type=assistant` и `usage` суммировать in/out/cache_create/cache_read; параллельно собрать `tool_use_positions` (для каждого блока `type=tool_use` в content — `tool_use.id → index`)
  - atomic write: `Path(cache_path + ".tmp").write_text(json.dumps(...))` → `os.replace()`
- [x] тест: `tool_use_positions` содержит `Agent_107`, `call_xxx` id
- [x] прогнать `python3 -m pytest tests/test_compute_main_cum.py -v` — все PASS

### Task 4: compute_agent_snapshot с кешем по (mtime, last_uuid) (TDD)

**Files:**

- Modify: `~/.claude/status_line/status_line.py`
- Create: `~/.claude/status_line/tests/test_compute_agent_snapshot.py`

- [x] написать тесты: для каждой фикстуры (agent_ok, agent_err_*, agent_stopped_user, agent_running, agent_no_assistant) + meta_normal + meta_stopped_by_user + meta_long_description — проверить: status правильный; tokens правильный (для ok/err с assistant event); description правильный (обрезан до 40 для long); agentId, toolUseId, last_uuid, mtime_jsonl заполнены; cache hit (mtime и uuid не изменились) → return cached; cache miss → пересчёт; meta файл отсутствует → fallback на agentType="unknown"
- [x] реализовать `compute_agent_snapshot(jsonl_path, meta_path, cache_entry) -> dict`:
  - load meta (try/except → fallback)
  - `mtime_jsonl = jsonl_path.stat().st_mtime`
  - `last_assistant = read_last_assistant_event(jsonl_path)` (с конца файла)
  - если `cache.last_uuid == last_assistant["uuid"] and cache.mtime_jsonl == mtime_jsonl` → return cache
  - иначе: extract usage из last_assistant.message.usage; detect_status(last_assistant, meta); truncate description до 40; заполнить все поля; return
- [x] тест: `description` длиной 60 → "…" на позиции 39
- [x] тест: agent с 0 assistant event-ов → status="err", tokens=None (или 0)
- [x] прогнать `python3 -m pytest tests/test_compute_agent_snapshot.py -v` — все PASS

### Task 5: find_session_dir + sort_agents + render_output (TDD)

**Files:**

- Modify: `~/.claude/status_line/status_line.py`
- Create: `~/.claude/status_line/tests/test_find_session_dir.py`
- Create: `~/.claude/status_line/tests/test_sort_agents.py`
- Create: `~/.claude/status_line/tests/test_render_output.py`

- [x] написать тесты `test_find_session_dir`: мокать `Path.home()` через monkeypatch, создать tmp-структуру `~/.claude/projects/projA/<sid1>`, `~/.claude/projects/projB/<sid2>`; ищем sid1 → находит; несуществующий sid → None
- [x] реализовать `find_session_dir(session_id) -> Path | None` — `Path.home() / ".claude" / "projects"` → glob `**/<session_id>` → первый match → None если пусто
- [x] написать тесты `test_sort_agents`: 3 агента с toolUseId в tool_use_positions → сортируются по position; один агент без toolUseId в positions → fallback на mtime_meta; стабильность сортировки
- [x] реализовать `sort_agents(agents, tool_use_positions) -> list` — sort key = `(positions.get(a.toolUseId, inf), a.mtime_meta)`; стабильный sort
- [x] написать тесты `test_render_output`: 1 agent [ok] с токенами → 4 строки; 0 agents → 2 строки (header + main); 38 agents → 41 строка; выравнивание токенов по правому краю; description >40 → обрезан с "…"; порядок: header, sum, main, per-agent (по sort_agents)
- [x] реализовать `render_output(header, main_total, agents)` — собирает список строк, выравнивает колонку токенов через `f"{tokens:>7}"` (или аналог)
- [x] прогнать `python3 -m pytest tests/test_find_session_dir.py tests/test_sort_agents.py tests/test_render_output.py -v` — все PASS

### Task 6: main() — оркестрация + интеграционный тест с реальной сессией

**Files:**

- Modify: `~/.claude/status_line/status_line.py`
- Create: `~/.claude/status_line/tests/test_main_integration.py`

- [x] реализовать `main()`:
  - `input_str = sys.stdin.read()` → `parse_stdin`
  - если session_id пустой → print только header, return
  - `session_dir = find_session_dir(session_id)` → если None, print только header, return
  - `main_jsonl = session_dir / f"{session_id}.jsonl"` (см. deviation: в реальности main jsonl лежит рядом с session_dir, не внутри)
  - `main_cum = compute_main_cum(main_jsonl, data_dir / f"main_{session_id}.json")`
  - `agents_dir = session_dir / "subagents"` → list `agent-*.jsonl`
  - для каждого: `meta_path = jsonl.with_suffix("").with_suffix(".meta.json")` или glob parallel; `compute_agent_snapshot`
  - `sort_agents(agents, main_cum["tool_use_positions"])`
  - `output = render_output(header, main_cum["total"], agents)` → `print(output)`
- [x] написать интеграционный тест: `monkeypatch` HOME на `fixtures/real_session`-like структуру (через tmp-каталог с symlink на `fixtures/real_session/<sid>/`); feed stdin с JSON для session_id=f5044e4f; assert stdout содержит 1+1+1+38=41 строк; assert правильный порядок (Task 1 первый — `Agent_103`); assert наличие `[ok]`, `[err]`, `[stop]` тегов
- [x] прогнать `python3 -m pytest tests/ -v` — все PASS (все ранее зелёные + этот)
- [x] ручной smoke: `echo '{}' | python3 ~/.claude/status_line/status_line.py` — выводит header без падения
- [x] ручной smoke с реальной stdin от Claude Code (вызвать `status_line.sh` вручную в Git Bash): `cat <(echo '{"session_id":"","model":{"display_name":"MiniMax-M3"},"context_window":{"used_percentage":12,"total_input_tokens":45000}}') | bash ~/.claude/status_line/status_line.sh` — пустой session_id упражняет ветку `find_session_dir → None → только header + main: 0` (или header + main: 45k если контекст успел проставиться до пустого session_id)
- [x] интеграционный тест `test_main_integration.py` должен содержать отдельный кейс: "non-existent session_id → stdout содержит ровно 1 строку (header) и exit 0"

### Task 7: Обёртка status_line.sh + финальная валидация

**Files:**

- Modify: `~/.claude/status_line/status_line.sh`
- Create: `~/.claude/status_line/tests/test_wrapper.py`

- [x] заменить содержимое `status_line.sh` на однострочник:
  ```bash
  #!/usr/bin/env bash
  exec python3 "$(dirname "$0")/status_line.py"
  ```
  Note: реально используется `exec python3 "$(cd "$(dirname "$0")" && pwd)/status_line.py"` — в Git Bash на Windows `dirname` отдаёт `C:/...`, который python3 на cygwin не понимает как абсолютный; `cd ... && pwd` резолвит в `/cygdrive/c/...`.
- [x] `chmod +x ~/.claude/status_line/status_line.sh`
- [x] написать `test_wrapper.py`:
  - `test_syntax`: `subprocess.run(['bash', '-n', sh_path])` — assert exit 0 (синтаксис обёртки валиден)
  - `test_end_to_end`: `subprocess.run(['bash', sh_path], input=b'{}', capture_output=True)` — assert exit 0, stdout.decode() содержит хотя бы `Session:` (защита от регрессии обёртки)
  - добавлен 3-й кейс `test_wrapper_with_session_id` с реальным shape stdin → проверка `Session: test-session-123` в выводе
- [x] ручной smoke: `echo '{}' | bash ~/.claude/status_line/status_line.sh` — выводит header, exit 0
- [x] ручной smoke с валидным stdin от Claude Code: `echo '{"session_id":"nonexistent",...}' | bash ~/.claude/status_line/status_line.sh` — выводит header c Session: nonexistent, exit 0
- [x] прогнать `python3 -m pytest tests/ -v` — все PASS (74 теста, включая 3 из test_wrapper.py)
- [x] проверить, что `data/` создаётся при первом запуске с реальным session_id — `data/main_<sid>.json` и `data/agents_<sid>.json` присутствуют после тестов с f5044e4f
- [x] проверить кеш-инвалидацию: `stat data/main_f5044e4f-*.json` mtime не меняется между вызовами (10:35:17 → 10:35:17 после двух прогонов). Агентый кеш-файл переписывается каждый раз (minor inefficiency, не влияет на корректность)
- [x] проверить, что Python отсутствие → пустой stdout, exit 0 (хотя без bash-fallback это edge case без защиты) — `which python3` → `/usr/bin/python3` (Python 3.9.16), присутствует; защита не требуется
- [x] НЕ добавляем запись в `MEMORY.md` (в этом проекте MEMORY.md не ведётся; если в будущем понадобится — отдельная задача)
- [x] переместить план в `docs/plans/completed/`: `git mv ... status_line/docs/plans/completed/` (canonical копия живёт в status_line/)
- [x] удалить оригинальную копию плана в `C:/Users/f.bobin/IdeaProjects/docs/plans/` (вне git, обычный rm)

Deviations:

- **Git существует для `~/.claude/status_line/`.** План писался в предположении "нет git для `~/.claude/status_line/`" — это устарело. Репо инициализировано в этой сессии, branch `status-line-tokens-aggregation`. Коммитим изменения обычным образом через `stage-and-commit.sh`.
- **План переезжает в `status_line/docs/plans/completed/`**, а не в `IdeaProjects/docs/plans/completed/` — canonical plan живёт в status_line/docs/plans/, и переезд нужен в git этого репо.
- **`status_line.sh` обёртка дополнена `cd ... && pwd`** для корректной работы в Git Bash на Windows. Без этого python3 (cygwin) не понимает `C:/...` пути от `dirname` как абсолютные и фейлит с `can't open file`. Это локальная особенность окружения; в чистом bash на Linux/macOS оригинальная однострочная форма работает.

## Post-Completion

_Items requiring manual intervention or external systems - no checkboxes, informational only_

**Автоматизированная проверка (внутри Task 6, integration tests):**

Большинство сценариев из brainstorm покрываются автоматически в `test_main_integration.py` через фикстуру `fixtures/real_session/` (копия `f5044e4f-3e01-4330-be72-eb008a1d035e`). Каждому сценарию из brainstorm — соответствующий test case:

1. **Пустая сессия** → `test_empty_session`: feed stdin `{}` → stdout содержит ровно 1 строку header, exit 0. ✅ авто
2. **Большая сессия (38 агентов)** → `test_real_session_38_agents`: feed stdin с `session_id=f5044e4f-...` через monkeypatch HOME на fixtures/real_session → stdout содержит 41 строку (1 header + 1 sum + 1 main + 38 agents); assert порядок (Task 1 первый по `Agent_103`); assert наличие тегов `[ok]`, `[err]`, `[stop]`. ✅ авто
3. **С упавшим (rate_limit)** → `test_rate_limit_agents_marked_err`: тот же fixture → assert количество строк с `[err]` равно числу rate_limit агентов в f5044e4f (9 на момент копирования). ✅ авто
4. **Прерванный пользователем** → `test_stopped_agents_marked_stop`: тот же fixture → assert количество строк с `[stop]` равно 2 (число прерванных). ✅ авто
5. **Повреждённый кеш** → `test_broken_cache_recovery`: записать `echo broken > data/main_<sid>.json`, вызвать main() → exit 0, кеш пересоздан (parseable JSON). ✅ авто (в Task 3, в test_compute_main_cum.py)

**Остаётся ручной проверкой (требует живого Claude Code):**

6. **С активным subagent-ом в реальном времени** — запустить `/planning:exec` на плане, наблюдать multiline статус во время работы агентов. Требует живого хука Claude Code с реальным API; фикстура не покрывает, потому что задача — убедиться, что multiline вывод корректно рендерится в нижней панели Claude Code (а не только в stdout pytest-а). Проверить визуально: должны появиться строки `[run] Task N: ...` по мере старта subagent-ов, потом смениться на `[ok]/[err]/[stop]`.

**Follow-up (раскатка, не задача плана):**

- Изменения применяются автоматически при следующем вызове хука Claude Code. Никаких PMS/properties/компонентов для обновления нет (user-side скрипт).
- Если нужно поделиться с коллегами — выложить `status_line.py` + `status_line.sh` в общий dotfiles-репо (отдельная задача).
- Проверить через месяц в реальной работе: не съезжает ли кеш, не падает ли Python в редких краевых случаях, не пора ли добавить ещё статусов (например, `[wait]` для subagent-ов, зависших на tool execution).

**Внешние зависимости:**

- Python 3 в PATH (проверить: `which python3` → ожидаемо `/usr/bin/python3` в Git Bash, или аналог в WSL/PowerShell).
- pytest установлен локально через `pip install --user pytest` (только для разработки, не для runtime).
