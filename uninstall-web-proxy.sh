<p align="center">
  <img src="panel-logo.png" alt="WEB PANEL PROXY" width="190">
</p>

<h1 align="center">WEB PANEL PROXY 2.4.0</h1>

<p align="center">WEB Proxy, MTProto, VLESS XHTTP, Hysteria2, AWG 2.0 / 3.1, OpenFlux и удобная панель управления для собственного VPS</p>

## Быстрый старт

Подключитесь к серверу по SSH и перейдите в режим `root`:

```bash
sudo -i
```

### Установка

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/POLESNIESOVETI12/web-panel-proxy/v2.4.0/install.sh)
```

Установщик запросит домен, email для HTTPS-сертификата, логин и пароль панели. После установки он покажет адрес панели и данные для входа.

### Обновление

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/POLESNIESOVETI12/web-panel-proxy/main/update.sh)
```

Обновление устанавливает последний стабильный релиз и сохраняет пользователей, ключи, настройки, адрес панели и HTML-заглушки.

### Удаление

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/POLESNIESOVETI12/web-panel-proxy/v2.4.0/uninstall-web-proxy.sh)
```

> **Внимание:** удаление выполняется без дополнительного подтверждения и стирает пользователей, ключи, конфигурации, службы и сайт WEB PANEL PROXY.

## Требования

- Чистый VPS с Ubuntu 22.04+, Ubuntu 24.04+ или Debian 12+.
- Архитектура `x86_64`.
- Домен или поддомен с A-записью на IPv4 сервера.
- Доступ `root` и установленные `curl` и `ca-certificates`.
- Свободные и открытые `80/tcp` и `443/tcp`.

Дополнительные порты выбранных подключений установщик и панель покажут автоматически. Их также необходимо открыть во внешнем firewall VPS-провайдера.

## Полезно знать

- Команда `WPP` открывает консольное меню управления.
- Caddy постоянно использует `80/tcp` и `443/tcp` для HTTP/HTTPS и сертификатов.
- Адрес панели, Node API token и ссылки подключений являются секретными — не публикуйте их.
- Перед обновлением автоматически создаётся резервная копия.

## Ссылки

- [GitHub проекта](https://github.com/POLESNIESOVETI12/web-panel-proxy)
- [YouTube автора](https://www.youtube.com/@POLESNIESOVETI12)

## Лицензия

MIT License. Подробности находятся в файле [LICENSE](LICENSE).
