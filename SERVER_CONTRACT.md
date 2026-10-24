# API SpiritLM для SpiritCode v1

Все маршруты требуют `Authorization: Bearer <API-ключ SpiritLM>`, возвращают
`Cache-Control: no-store`. Пользователь определяется исключительно по ключу.
Неверный/отозванный ключ: HTTP 401. Неверный period: HTTP 400.
Clerk и upstream credentials клиенту не передаются. Финансы — строки, даты — UTC ISO 8601.

`GET /v1/account` (имя/email из Clerk; внутренний Clerk ID не выдаётся):

```json
{"id":"user-id","name":"Анна","username":"anna","email":"anna@example.com","created_at":"2026-10-23T00:00:00.000Z","key":{"name":"CLI","masked":"sk-spiritlm-abcd…","use_subscription":true,"allow_balance_fallback":false}}
```

`GET /v1/account/balance`:

```json
{"balance":"10.00000001","currency":"RUB"}
```

`GET /v1/account/subscription` (тестовые периоды исключены):

```json
{"current":null,"upcoming":[],"running":0,"recent":0}
```

Ненулевой `current` и элементы `upcoming` содержат `id`, `planId`, `planName`,
`mode: "live"`, `credits`, `remainingCredits`, `reservedCredits` (десятичные строки),
`startsAt`, `endsAt` (UTC), `requestsPerMinute`, `concurrentRequests`.

`GET /v1/account/usage?period=2d` (`2d`, `7d`, `30d`, `all`):

```json
{"period":"2d","currency":"RUB","cost":"0.00000000","subscription_credits_used":"0.00000000","requests":1,"unknown_usage_count":1,"known_tokens":0,"total_tokens":null}
```

`known_tokens` — сумма известных данных. `total_tokens` равен `null`, если хотя бы
для одного запроса usage неизвестен. При отсутствии запросов он равен 0.

`GET /v1/spiritcode/models` — только доступные модели из каталога SpiritLM:

```json
{"object":"list","data":[{"id":"provider/model","tool_calling":null,"context_window":null,"max_output_tokens":4096,"price_per_million_tokens":"0.90000000","currency":"RUB","sample_price":true,"subscription_limits":null}]}
```

`tool_calling`/`context_window`/`max_output_tokens` берутся из серверной конфигурации
подтверждённых возможностей; лимит вывода также может приходить из существующего каталога.
`subscription_limits` пока неизвестны; текущие ограничения доступны в account/subscription.
Исходный `/v1/models` сохраняет прежний формат.

Любая ошибка сохраняет существующий формат SpiritLM, например HTTP 401:

```json
{"error":{"message":"Неверный API-ключ SpiritLM","type":"api_error","code":"invalid_api_key"}}
```

Клиент не выводит приватный текст HTTP-ошибок. Ответы 404/501 метаданных означают
старый сервер: доступен каталог и чат без инструментов. Редиректы не выполняются.
