# Telegram Web Proxy — установка на Ubuntu 24.04

Автоматическая установка Telegram Web Proxy на новый VPS.

В проекте используются:

- MTProxy
- `tproxy-server`
- Caddy
- HTTPS-сертификат
- systemd
- универсальная HTML-заглушка

Панель управления в этой версии **не используется**.

---

## 1. Что понадобится

Новый VPS:

[АРЕНДОВАТЬ VPS И ДОМЕН МОЖНО ТУТ](https://play2go.cloud/?ref_id=m1o4quWG0sE)

```text
Ubuntu 24.04
x86_64
root-доступ
```

Также нужен домен или поддомен.

Пример:

```text
proxy.example.com
```

Создайте DNS A-запись:

```text
proxy.example.com → IP-адрес вашего VPS
```

Порты `80` и `443` должны быть свободны.

---

# 2. Установка в одну команду

На VPS выполните одну команду:

```bash
curl -fsSL https://raw.githubusercontent.com/POLESNIESOVETI12/webtelegram/main/install-webproxy.sh -o /root/install-webproxy.sh && chmod +x /root/install-webproxy.sh && /root/install-webproxy.sh
```

После запуска установщик попросит:

```text
Domain (example: proxy.example.com):
ACME email (example: admin@example.com):
Generate a secure secret automatically? [Y/n]:
```

Например:

```text
Domain (example: proxy.example.com): proxy.example.com
ACME email (example: admin@example.com): admin@example.com
Generate a secure secret automatically? [Y/n]: y
```

Дальше установка выполняется автоматически.

---

# 3. Что делает установщик

Скрипт:

```text
Проверяет систему
        ↓
Проверяет DNS
        ↓
Проверяет порты 80/443
        ↓
Устанавливает зависимости
        ↓
Создаёт сайт-заглушку
        ↓
Устанавливает MTProxy
        ↓
Собирает tproxy-server
        ↓
Настраивает systemd
        ↓
Настраивает Caddy
        ↓
Получает HTTPS-сертификат
        ↓
Запускает сервисы
        ↓
Проверяет MTProxy :2398
        ↓
Проверяет relay /readyz
        ↓
Проверяет /healthz
        ↓
Ждёт HTTPS до 120 секунд
        ↓
Проверяет HTTPS
        ↓
Показывает Telegram Web Proxy
```

---

# 4. Что появится в конце

После успешной установки вы увидите примерно:

```text
============================================================
             TELEGRAM WEB PROXY IS READY
============================================================

Domain:
  https://proxy.example.com/

Secret:
  ******************************

Telegram Web Proxy:
  https://t.me/webproxy?server=proxy.example.com&secret=************************


Status:
  HTTPS          OK
  MTProxy        ACTIVE
  Relay          READY
  Firewall       ACTIVE
============================================================
```

**Secret никому не передавайте и не публикуйте.**

---

# 5. Полное удаление установки

Для удаления Telegram Web Proxy используйте:

```bash
curl -fsSL https://raw.githubusercontent.com/POLESNIESOVETI12/webtelegram/main/uninstall-webproxy.sh -o /tmp/uninstall-webproxy.sh && chmod +x /tmp/uninstall-webproxy.sh && /tmp/uninstall-webproxy.sh
```

Деинсталлятор попросит подтвердить удаление:

```text
REMOVE
```

После этого будут удалены компоненты Telegram Web Proxy, Caddy, конфигурация, сайт и созданные сервисы.

---


# 6. Как заменить HTML-заглушку

Сайт-заглушка находится здесь:

```bash
/srv/tproxy-site/index.html
```

Открыть:

```bash
nano /srv/tproxy-site/index.html
```

Можно заменить HTML на свой.

После изменения:

```bash
chown -R root:root /srv/tproxy-site
find /srv/tproxy-site -type d -exec chmod 0755 {} \;
find /srv/tproxy-site -type f -exec chmod 0644 {} \;
```

После этого обновите страницу в браузере.

Caddy перезапускать не требуется.

---

# 7. Готовая HTML-заглушка

Пример простой страницы:

```html
<!doctype html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">

    <title>Подключение</title>

    <style>
        :root {
            color-scheme: dark;
            --bg: #0a0d12;
            --card: #11161f;
            --text: #f5f7fb;
            --muted: #8f99a8;
        }

        * {
            box-sizing: border-box;
        }

        body {
            margin: 0;
            min-height: 100vh;
            display: grid;
            place-items: center;
            padding: 24px;
            background: var(--bg);
            color: var(--text);
            font-family: system-ui, sans-serif;
        }

        .card {
            width: min(100%, 560px);
            padding: 38px 30px;
            text-align: center;
            background: var(--card);
            border: 1px solid #242c38;
            border-radius: 22px;
        }

        h1 {
            margin: 0;
            font-size: 32px;
        }

        p {
            color: var(--muted);
        }

        .loader {
            width: min(100%, 320px);
            height: 8px;
            margin: 28px auto;
            overflow: hidden;
            border-radius: 999px;
            background: #202733;
        }

        .loader::before {
            content: "";
            display: block;
            width: 34%;
            height: 100%;
            background: #fff;
            border-radius: inherit;
            animation: loading 1.25s ease-in-out infinite;
        }

        @keyframes loading {
            0% {
                transform: translateX(-120%);
            }

            50% {
                transform: translateX(190%);
            }

            100% {
                transform: translateX(320%);
            }
        }
    </style>
</head>

<body>

<main class="card">
    <h1>Подключение</h1>

    <p>
        Пожалуйста, подождите.<br>
        Идёт загрузка страницы.
    </p>

    <div class="loader"></div>
</main>

</body>
</html>
```

---


# 8. Полное удаление установки

Для удаления Telegram Web Proxy используйте:

```bash
curl -fsSL https://raw.githubusercontent.com/POLESNIESOVETI12/webtelegram/main/uninstall-webproxy.sh -o /tmp/uninstall-webproxy.sh && chmod +x /tmp/uninstall-webproxy.sh && /tmp/uninstall-webproxy.sh
```

Деинсталлятор попросит подтвердить удаление:

```text
REMOVE
```

После этого будут удалены компоненты Telegram Web Proxy, Caddy, конфигурация, сайт и созданные сервисы.

---

# 9. Если порт 80 или 443 занят

Проверьте:

```bash
ss -lntp | grep -E ':(80|443)\b'
```

Установщик специально останавливается, если эти порты уже заняты.

---



# 10. Что такое Telegram Web Proxy

Telegram Web Proxy — это промежуточный сервер между клиентом Telegram и Telegram.

Упрощённо:

```text
Телефон
   ↓
HTTPS
   ↓
Caddy
   ↓
tproxy-server
   ↓
MTProxy
   ↓
Telegram
```


## Содержание репозитория

```text
webtelegram/
├── README.md
├── install-webproxy.sh
└── uninstall-webproxy.sh
```

Панель управления в этой версии не используется.
