# Бот розыгрышей и промоакций

Бот для MAX с промокодами партнёров, ежедневными розыгрышами и накоплением шансов на финальный приз.

## Что реализовано

- меню из пяти разделов по заданию
- один промокод партнёра на пользователя, повторный запрос возвращает тот же код
- участие в розыгрыше даже после окончания промокодов
- проверка подписки через список участников канала MAX
- одна регистрация и один финальный шанс за день, максимум 10 шансов
- отдельное число победителей для каждого партнёра
- дневной и взвешенный финальный розыгрыши без повторяющихся победителей
- черновик результатов и подтверждение администратором перед рассылкой
- полная выгрузка участников, победителей и резерва в CSV и JSON
- сбор имени, фамилии и согласия победителя
- передача невостребованного приза следующему участнику резерва
- очередь личных и массовых уведомлений
- статистика пользователей, запросов и выдачи промокодов, остатков и участников

## Запуск на Windows

Нужен Python 3.11 или новее и бот, созданный в [MAX для бизнеса](https://business.max.ru/).

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

В `.env` нужно указать `MAX_BOT_TOKEN`. Остальные настройки уже имеют рабочие значения по умолчанию.

Партнёры загружаются из CSV. Поля и пример находятся в `data/partners.example.csv`.

```powershell
.\.venv\Scripts\python.exe admin.py import-partners data/partners.example.csv
.\.venv\Scripts\python.exe admin.py import-promos partner-one data/promos.example.csv
```

Чтобы бот мог проверять подписку, его нужно добавить администратором в канал партнёра, а в CSV указать настоящий `channel_id`. Проверка использует официальный метод `GET /chats/{chatId}/members`.

Баннер из `assets/banner.png` загружается один раз:

```powershell
.\.venv\Scripts\python.exe admin.py upload-banner assets/banner.png
```

Полученный токен нужно записать в `WELCOME_BANNER_TOKEN` внутри `.env`. После этого можно запускать бота:

```powershell
.\.venv\Scripts\python.exe bot.py
```

Для локального запуска используется Long Polling. В рабочем окружении MAX рекомендует Webhook; бизнес-логика от способа получения событий не зависит.

## Проведение розыгрыша

Сначала создаётся черновик. Уведомления на этом этапе не отправляются.

```powershell
.\.venv\Scripts\python.exe admin.py draw-daily --day 2026-11-01
.\.venv\Scripts\python.exe admin.py draws
.\.venv\Scripts\python.exe admin.py export 1
```

После проверки выгрузки администратор подтверждает результат и запускает очередь уведомлений:

```powershell
.\.venv\Scripts\python.exe admin.py approve 1
.\.venv\Scripts\python.exe admin.py send-notifications
```

Финальный розыгрыш выбирает пять уникальных победителей и пять резервных участников. Вес пользователя равен числу его активных дней.

```powershell
.\.venv\Scripts\python.exe admin.py draw-final --day 2026-11-15
```

Просроченные заявки передаются резерву командой:

```powershell
.\.venv\Scripts\python.exe admin.py expire-claims
```

## Статистика и пуши

```powershell
.\.venv\Scripts\python.exe admin.py stats
.\.venv\Scripts\python.exe admin.py push "Завтра новый партнёр и новый розыгрыш"
.\.venv\Scripts\python.exe admin.py send-notifications
```

Для сообщения одному пользователю добавляется `--user-id ID`.

## Проверка

```powershell
py -m unittest discover -s tests -v
```

Тесты проверяют повторную выдачу промокода, лимит шансов, участие без промокода, отсутствие дублей в розыгрышах, подтверждение результатов, сбор согласия и замену победителя резервом.

## Структура

```text
promo_bot/
  app.py          обработчики событий и тексты бота
  database.py     схема SQLite и механика акции
  max_api.py      запросы к API MAX
admin.py          команды организатора
bot.py            запуск бота
data/             примеры CSV и локальная база
exports/          выгрузки розыгрышей
tests/            автоматические тесты
```

Документация API: [получение событий](https://dev.max.ru/docs-api/methods/GET/updates), [отправка сообщений](https://dev.max.ru/docs-api/methods/POST/messages), [кнопки](https://dev.max.ru/docs-api/use-cases/sending-messages/keyboard).
