# Codex CLI — исследование и PoC

## Документация и выбор интерфейса

| Интерфейс | Подтверждённые возможности | Применение |
| --- | --- | --- |
| `codex exec` | Неинтерактивный запуск, JSONL, финальный ответ по JSON Schema, возобновление по ID. CLI умеет вход с ChatGPT и с API-ключом. Device auth доступен при включённой настройке. | Первый пилот, минимум зависимостей, один процесс на turn. [Запуск](https://learn.chatgpt.com/docs/non-interactive-mode) · [Авторизация](https://learn.chatgpt.com/docs/auth) |
| Codex SDK | Управление потоками, запуск и возобновление; доступны официальные инструкции SDK. | Рассмотреть после пилота, если библиотечный клиент упростит обработку событий. [SDK](https://learn.chatgpt.com/docs/codex-sdk) |
| Codex App Server | Долгоживущий процесс с протоколом запуска/возобновления thread и жизненного цикла turn. | Подходит для потокового управления и отмены. Сложнее защищать и сопровождать. Для интеграции предпочесть локальный транспорт. [App Server](https://learn.chatgpt.com/docs/app-server) |

Возможность программно вызвать CLI и войти через действующую подписку документирована. Конкретные траты, доступность трёх подписок и применение условий к непрерывному сервису остаются отдельными проверками. Правила OpenAI запрещают совместное использование аккаунтов и обход ограничений. В предложенной модели у каждой роли свой фиксированный владелец, без передачи авторизации и автоматического fallback. Перед включением автономной работы владельцы подтверждают применимость условий своего плана и отсутствие несогласованных списаний. [Условия](https://openai.com/policies/terms-of-use/) · [Европейские условия](https://openai.com/policies/eu-terms-of-use/) · [Использование подписки](https://help.openai.com/en/articles/20001625-understanding-chatgpt-and-codex-usage-limits)

Инструкции передаются по stdin, схема и разрешённые действия задаются доверенным кодом. В рабочем адаптере нужно различать неопределённый исход, отзыв авторизации, недоступность и лимит. Не распознавать ошибки только по отдельному числу из имитатора. До получения стабильного диагностического сигнала любую неизвестную ошибку останавливать для ручной сверки, закрывать лимиты параллельности и проверять срок возобновления с владельцем. [Песочницы и разрешения](https://learn.chatgpt.com/docs/agent-approvals-security)

## Что проверено

- В рабочей среде `codex --version` показал `0.172.0-alpha.2`, `codex login status` сообщил о входе через ChatGPT. Это наблюдение о режиме входа, без проверки владельца и оплаты.
- Из локального help подтверждены `login --device-auth`, `exec`, `exec resume SESSION_ID`, `--json`, `--output-schema`, `-o` и выбор каталога. Для resume используем проверенный shell `cd` и ставим параметры песочницы в родительскую команду. Help подтвердил разбор этих параметров. Фактическое возобновление модели ещё не проверено.
- Попытка обратиться к встроенной Linux-песочнице остановилась на системном ограничении bwrap `NETLINK_ROUTE`. Живых модельных вызовов, логинов и обращения к содержимому авторизационных файлов в этой среде не было.
- **10 офлайн-тестов** проверили передачу stdin и каталога через подставной invoker, разбор JSONL, сохранение ID, resume после переоткрытия SQLite, разные контексты Игорька и Андрюхи, отказ при смене потока, тайм-аут, отсутствие CLI, ошибки подписки, дедупликацию и невалидный вывод. Имитатор не устанавливает реальные коды ошибок и пределы доступа.
- Регрессия V1/V2 и Судьи в этой среде дала 58 успешных из 63, 5 пропущены (тест транспорта требует закреплённой версии CLI, 4 теста требуют Docker). Исходники этих контуров не изменены. Прежний CI на исходной базе прошёл все 63.

Офлайн-проверку воспроизводит `python3 -m unittest coordinator_v3.test_spike -v` из корня ветки.

## Владелец одного аккаунта — два живых вызова

Сначала владелец сверяет Usage, кредиты и отсутствие дополнительных списаний, выбирает собственную среду со стабильной CLI, проверяет поддержку параметров через help и при необходимости авторизуется сам. При неясном режиме расходов или ошибке песочницы остановиться. Успешный PoC в одном аккаунте нужен до подключения остальных. Команды ниже выполняются владельцем только при подтверждённом режиме оплаты. Нужны Bash, Git и jq.

```bash
set -euo pipefail
umask 077
codex --version
codex login status  # Должен сообщить о входе через ChatGPT
codex exec --help
codex exec resume --help

outer=$(mktemp -d)
mkdir "$outer/work"
cd "$outer/work"
git init -q
cat > "$outer/schema.json" <<'JSON'
{"type":"object","properties":{"answer":{"type":"string"}},"required":["answer"],"additionalProperties":false}
JSON

run_codex() {
  env -u OPENAI_API_KEY -u CODEX_API_KEY -u CODEX_ACCESS_TOKEN \
    codex exec --ignore-user-config --sandbox read-only \
    --disable shell_tool --disable unified_exec --disable hooks --disable apps \
    --disable plugins --disable multi_agent --disable multi_agent_v2 \
    -c 'approval_policy="never"' -c 'web_search="disabled"' "$@"
}

printf '%s\n' 'Верни JSON с answer равным pilot-v3-384. Не выполняй действий.' |
  run_codex --json --output-schema "$outer/schema.json" \
    -o "$outer/first.json" - > "$outer/first.jsonl"
jq -e '.answer == "pilot-v3-384"' "$outer/first.json"
thread=$(jq -er 'select(.type == "thread.started") | .thread_id' "$outer/first.jsonl")
[[ "$thread" =~ ^[0-9a-fA-F-]{36}$ ]]

printf '%s\n' 'Вспомни предыдущее значение answer и добавь к нему суффикс -resume. Верни JSON.' |
  run_codex resume --json --output-schema "$outer/schema.json" \
    -o "$outer/second.json" "$thread" - > "$outer/second.jsonl"
jq -e --arg thread "$thread" 'select(.type == "thread.started") | .thread_id == $thread' "$outer/second.jsonl"
jq -e '.answer == "pilot-v3-384-resume"' "$outer/second.json"
```

В #19 указать версию, режим входа, итог create/resume, совпадение thread ID без публикации самого ID, разницу Usage и наличие списаний. При отказе достаточно класса ошибки. Сырые журналы и каталог остаются у владельца. Толя, Семён и третий владелец подключают свои среды самостоятельно после одобрения PoC и топологии.
