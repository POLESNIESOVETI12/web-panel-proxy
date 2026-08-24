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

# 2. Самый простой способ установки

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

YouTube channel:
  https://www.youtube.com/@POLESNIESOVETI12

Status:
  HTTPS          OK
  MTProxy        ACTIVE
  Relay          READY
  Firewall       ACTIVE
============================================================
```

**Secret никому не передавайте и не публикуйте.**

---

# 5. Проверка установки

Проверить сервисы:

```bash
systemctl --no-pager --full status mtproxy tproxy-server caddy
```

Проверить порты:

```bash
ss -lntp | grep -E ':(2398|8080|8081|80|443)\b'
```

Проверить relay:

```bash
curl --fail http://127.0.0.1:8081/healthz && echo
curl --fail http://127.0.0.1:8081/readyz && echo
```

Ожидается:

```text
ok
ready
```

Проверить сайт:

```bash
curl -I https://YOUR-DOMAIN/
```

Например:

```bash
curl -I https://proxy.example.com/
```

Ожидается:

```text
HTTP/2 200
```

---

# 6. Если хотите установить через Nano

Это удобно для видео, если не хочется использовать GitHub напрямую.

Создайте файл:

```bash
nano /root/install-webproxy.sh
```

Вставьте содержимое `install-webproxy.sh`.

Сохраните:

```text
Ctrl+O
Enter
Ctrl+X
```

Проверьте синтаксис:

```bash
bash -n /root/install-webproxy.sh && echo "SCRIPT OK"
```

Сделайте файл исполняемым:

```bash
chmod +x /root/install-webproxy.sh
```

Запустите:

```bash
/root/install-webproxy.sh
```

---

# 7. Как заменить HTML-заглушку

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

# 8. Готовая HTML-заглушка

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

# 9. Установка из уже скачанного файла

Если файл уже находится на VPS:

```bash
chmod +x /root/install-webproxy.sh
bash -n /root/install-webproxy.sh
/root/install-webproxy.sh
```

---

# 10. Полное удаление установки

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

# 11. Если порт 80 или 443 занят

Проверьте:

```bash
ss -lntp | grep -E ':(80|443)\b'
```

Установщик специально останавливается, если эти порты уже заняты.

---

# 12. Если HTTPS не готов сразу

Проверить Caddy:

```bash
systemctl --no-pager --full status caddy
```

Посмотреть последние сообщения:

```bash
journalctl -u caddy -n 100 --no-pager
```

Проверить HTTPS:

```bash
curl -I https://YOUR-DOMAIN/
```

Установщик ждёт выдачу HTTPS-сертификата до 120 секунд.

---

# 13. Если tproxy-server не готов

Проверить:

```bash
systemctl --no-pager --full status tproxy-server
```

Логи:

```bash
journalctl -u tproxy-server -n 100 --no-pager
```

Relay:

```bash
curl --fail http://127.0.0.1:8081/readyz && echo
```

---

# 14. Если MTProxy не запускается

Проверить:

```bash
systemctl --no-pager --full status mtproxy
```

Логи:

```bash
journalctl -u mtproxy -n 100 --no-pager
```

Проверить бинарник:

```bash
namei -l /opt/MTProxy/objs/bin/mtproto-proxy
```

---

# 15. Что такое Telegram Web Proxy

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

Это **не VPN** и не универсальный SOCKS/HTTP proxy.

---

# 16. Важно

Не публикуйте:

```text
Secret
Пароли
SSH-ключи
API-ключи
```

Не вставляйте реальные secrets в GitHub.

Установщик генерирует secret во время установки.

---

# 17. Репозиторий

GitHub:

```text
https://github.com/POLESNIESOVETI12/webtelegram
```

Установщик:

```text
https://raw.githubusercontent.com/POLESNIESOVETI12/webtelegram/main/install-webproxy.sh
```

Удаление:

```text
https://raw.githubusercontent.com/POLESNIESOVETI12/webtelegram/main/uninstall-webproxy.sh
```

---

# 18. Быстрый старт для видео

Самый короткий вариант для зрителя:

```bash
curl -fsSL https://raw.githubusercontent.com/POLESNIESOVETI12/webtelegram/main/install-webproxy.sh -o /root/install-webproxy.sh && chmod +x /root/install-webproxy.sh && /root/install-webproxy.sh
```

После этого введите:

```text
Домен
Email
Secret
```

и дождитесь:

```text
TELEGRAM WEB PROXY IS READY
```

---

## Содержание репозитория

```text
webtelegram/
├── README.md
├── install-webproxy.sh
└── uninstall-webproxy.sh
```

Панель управления в этой версии не используется.
