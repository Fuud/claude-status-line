# Merge subagents across duplicate session dirs

## Overview

В сессии `eacc81d9-0a13-4f6f-ae40-5ba51190cfd9` status_line не отображает агентов.
Корневая причина: под одним session id существует два каталога в
`~/.claude/projects/` — основной проект и worktree-проект
(`C--Users-f-bobin-IdeaProjects--worktrees-agentic-terminal-pty-backend`).
`find_session_dir` (`status_line.py:900`) берёт первое совпадение glob
`**/<sid>` — по алфавиту это пустой worktree-каталог (только `tool-results/`,
без `subagents/`) → `_compute_agents` возвращает `[]` → кэш
`agents_<sid>.json` = `{}` → агенты не рендерятся.

Ключевой факт из реальных данных: CC пишет `subagents/` в проектный каталог,
соответствующий cwd **на момент спауна агента**. Из 7 «раздвоённых» сессий
на этой машине в 5 агенты распределены по ОБОИМ каталогам (напр. `b29d2372`:
1 агент в основном, 23 в worktree-копии). Поэтому недостаточно выбрать
правильный каталог — надо объединять агентов из всех одноимённых каталогов.

## Context (from discovery)

- Файлы: `status_line.py` — `find_session_dir` (~line 900), `_find_main_jsonl`
  (~line 942), `_compute_agents` (~line 1349), `_main_unsafe` (~line 1419).
- Тесты: `tests/test_find_session_dir.py`, `tests/test_find_main_jsonl.py`,
  `tests/test_compute_agent_snapshot.py` (секция `_compute_agents`, ~line 519+),
  `tests/test_main_integration.py`.
- Существующие тесты вызывают `_compute_agents(session_dir, ...)` с одним
  `Path` — сигнатуру меняем обратно-совместимо (принимает `Path | list[Path]`).
- Кэш агентов (`agents_<sid>.json`) keyed по `agentId` и валидируется по mtime
  файла — объединение не нарушает его инварианты (дедуп происходит до
  построения снапшотов).
- Производительность: сегодня `find_session_dir` short-circuit'ит glob на
  первом совпадении; `find_session_dirs` будет проходить всё дерево
  `~/.claude/projects` целиком — это необходимо для корректности, но на
  Windows это самый syscall-тяжёлый путь хука (проверка времени — в Task 5).
  Скан `subagents/` добавляется у всех совпадений (на практике 1–2 каталога).

## Development Approach

- **testing approach**: TDD (tests first) — сначала падающие тесты, потом
  реализация до зелёного.
- complete each task fully before moving to the next
- make small, focused changes
- **CRITICAL: every task MUST include new/updated tests**
- **CRITICAL: all tests must pass before starting next task** — no exceptions
- **CRITICAL: update this plan file when scope changes during implementation**
- maintain backward compatibility (`find_session_dir` и одноаргументный вызов
  `_compute_agents` продолжают работать)

## Testing Strategy

- **unit tests**: required for every task (pytest, `python -m pytest tests/`).
- **e2e tests**: покрыты subprocess-интеграционными тестами в
  `tests/test_main_integration.py` — тесты Task 4 идут туда.

## Progress Tracking

- mark completed items with `[x]` immediately when done
- add newly discovered tasks with ➕ prefix
- document issues/blockers with ⚠️ prefix
- update plan if implementation deviates from original scope

## Solution Overview

Option A+ (согласовано в брейншторме):

1. Новый `find_session_dirs(session_id, projects_root=None) -> list[Path]` —
   возвращает ВСЕ каталоги `**/<sid>`, а не первое совпадение.
2. Новый `_resolve_session_dirs(transcript_path, session_id) -> list[Path]` —
   все совпадения glob, при этом каталог
   `Path(transcript_path).parent / session_id` (если существует) ставится
   первым — приоритет при дедупе (transcript_path — authoritative-источник
   расположения сессии, как и в `_find_main_jsonl`).
3. `_compute_agents` принимает `Path | list[Path]`: сканирует `subagents/`
   каждого каталога, объединяет снапшоты с дедупом по `agentId` (побеждает
   первый каталог списка). Дедуп на уровне путей — до вызова
   `compute_agent_snapshot`, чтобы не парсить дубликат дважды.
4. Старый `find_session_dir` остаётся тонкой обёрткой над `find_session_dirs`
   (возвращает первый элемент) — существующие тесты и контракт не ломаются.
5. Протухший кэш `{}` для пострадавших сессий перезапишется сам при следующем
   вызове хука — миграция не нужна.

## Technical Details

- `find_session_dirs`: тот же glob с `recurse_symlinks=False`-семантикой и
  фильтром `is_dir()`, что и `find_session_dir`; возвращает список в порядке
  glob (OS-dependent, как задокументировано); пустой `session_id` или
  несуществующий `projects_root` → `[]`.
- `_resolve_session_dirs`: если `transcript_path` непустой и
  `Path(transcript_path).parent / session_id` — существующий каталог, он идёт
  первым; если glob вернул его же — не дублируем; остальные совпадения — в
  порядке glob следом. Пустой `transcript_path` → просто `find_session_dirs`.
- `_compute_agents(session_dirs | session_dir, ...)`: нормализация
  `Path → [Path]` в начале. Обход каталогов по порядку; `agentId` уже
  присутствующий в map пропускается (first-dir wins). Остальная логика
  (meta_path, cache, orchestrator queue override) без изменений.
- `_main_unsafe`: заменяет вызов `find_session_dir(session_id)` на
  `_resolve_session_dirs(parsed.get("transcript_path", ""), session_id)`;
  `session_dir` для `_find_main_jsonl` — `dirs[0] if dirs else None`;
  `_compute_agents` получает весь список; запись кэша — если **список
  каталогов** непуст (текущая семантика: кэш пишется при найденном каталоге,
  даже если `agents == []` — каталог без `subagents/`; пропускается запись
  только для dirless-сессий).

## What Goes Where

- **Implementation Steps** (`[ ]` checkboxes): код и тесты в этом репо.
- **Post-Completion**: раскатка — см. конец файла.

## Implementation Steps

### Task 1: find_session_dirs — возвращать все совпадения

**Files:**

- Modify: `status_line.py`
- Modify: `tests/test_find_session_dir.py`

- [ ] TDD: написать тесты `find_session_dirs`: два одноимённых каталога в
      разных проектах → оба в результате; один каталог → список из одного;
      несуществующий sid → `[]`; пустой sid → `[]`; несуществующий
      projects_root → `[]`; файлы (не каталоги) с именем sid игнорируются
- [ ] убедиться, что новые тесты падают (функции ещё нет)
- [ ] реализовать `find_session_dirs` в `status_line.py` (glob `**/<sid>`,
      фильтр `is_dir()`, список в порядке glob)
- [ ] переписать `find_session_dir` как обёртку: первый элемент
      `find_session_dirs` или `None` (докстринг обновить; заодно модульный
      докстринг `tests/test_find_session_dir.py` — «returns the first match»
      больше не вся правда)
- [ ] прогнать `python -m pytest tests/test_find_session_dir.py` — все
      (старые + новые) зелёные

### Task 2: _resolve_session_dirs — приоритет transcript_path

**Files:**

- Modify: `status_line.py`
- Create: `tests/test_resolve_session_dirs.py`

- [ ] TDD: написать тесты: transcript-каталог существует и есть среди
      glob-совпадений → он первый, без дубля; transcript-каталог существует, но
      glob его не вернул (лежит вне projects_root) → он первый + glob-совпадения
      следом; пустой transcript_path → порядок чистого glob; transcript_path
      указывает на несуществующий файл/каталог → fallback на чистый glob
- [ ] убедиться, что тесты падают
- [ ] реализовать `_resolve_session_dirs(transcript_path, session_id,
projects_root=None) -> list[Path]`
- [ ] прогнать `python -m pytest tests/test_resolve_session_dirs.py` — зелёные

### Task 3: _compute_agents — объединение и дедуп по agentId

**Files:**

- Modify: `status_line.py`
- Modify: `tests/test_compute_agent_snapshot.py`

- [ ] TDD: написать тесты: агенты распределены по двум session-каталогам
      (разные agentId) → в результате объединение всех; одинаковый agentId в
      двух каталогах → один снапшот, из первого каталога списка; один `Path`
      вместо списка → поведение как раньше (back-compat); пустой список → `[]`;
      каталог без `subagents/` в списке → пропускается, остальные обрабатываются
- [ ] убедиться, что новые тесты падают
- [ ] изменить `_compute_agents`: нормализация аргумента в список, обход
      каталогов по порядку, дедуп по `agentId` на уровне путей до
      `compute_agent_snapshot`; orchestrator queue override — без изменений
- [ ] прогнать `python -m pytest tests/test_compute_agent_snapshot.py` —
      все зелёные (старые вызовы с одним `Path` работают)

### Task 4: интеграция в _main_unsafe

**Files:**

- Modify: `status_line.py`
- Modify: `tests/test_main_integration.py`

- [ ] TDD: написать интеграционный тест: payload с `transcript_path` и
      `session_id`, два проектных каталога с `<sid>/subagents/` (агенты
      распределены, одинаковый agentId в обоих) → вывод содержит строки всех
      агентов без дублей; кейс «воркtree-каталог первый по алфавиту и пустой»
      (воспроизведение бага eacc81d9) → агенты всё равно рендерятся
      (`_build_synth_session` хардкодит `encoded="synthetic-project"` —
      добавить параметризованное имя проекта для второго каталога);
      pre-seed кэша `agents_<sid>.json` значением `{}` (артефакт бага) →
      агенты всё равно рендерятся, кэш перезаписывается непустым
      (self-heal из Solution Overview п. 5)
- [ ] убедиться, что тесты падают на текущем коде
- [ ] `_main_unsafe`: заменить `find_session_dir` на `_resolve_session_dirs`,
      передать список в `_compute_agents`, `dirs[0] if dirs else None` —
      в `_find_main_jsonl`; запись кэша — только если список непуст
- [ ] прогнать `python -m pytest tests/test_main_integration.py` — зелёные

### Task 5: Verify acceptance criteria

- [ ] verify all requirements from Overview are implemented
- [ ] verify edge cases are handled (дедуп, dirless-сессия, пустой payload)
- [ ] run full test suite: `python -m pytest tests/`
- [ ] ручная проверка на реальных данных: прогнать `status_line.py` с
      payload сессии `eacc81d9-0a13-4f6f-ae40-5ba51190cfd9` → в выводе 18+
      строк агентов (раньше — ни одной)
- [ ] замер wall-clock одного прогона на реальном дереве `~/.claude/projects`
      (до/после) — убедиться, что полный обход дерева glob не замедлил хук
      патологически

### Task 6: [Final] Update documentation

- [ ] update README.md если описание поведения session dir затронуто
- [ ] move this plan to `docs/plans/completed/`

## Post-Completion

**Раскатка (follow-up, не задача плана):**

- Локальная: statusline-хук подхватит изменение на следующий вызов —
  дополнительных действий не требуется. Протухшие кэши `agents_<sid>.json`
  (`{}`) перезапишутся автоматически.
- Препрод-окружения/компоненты/проперти — неприменимо (не серверное
  изменение; сверка с `inventory` не требуется).

**Manual verification:**

- Понаблюдать status line в живой сессии с worktree-резюмом (напр. текущей
  `eacc81d9`) — строки агентов появляются и обновляются.
