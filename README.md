<p align="center">
  <img src="panel-logo.png" alt="WEB PANEL PROXY" width="190">
</p>

<h1 align="center">WEB PANEL PROXY 2.3.0</h1>

<p align="center">WEB Proxy, MTProto, VLESS XHTTP, Hysteria2, OpenFlux и панель управления для собственного VPS</p>

## Требования

- Чистый VPS с Ubuntu 22.04+, Ubuntu 24.04+ или Debian 12+.
- Архитектура `x86_64`.
- Домен или поддомен с A-записью на IPv4 вашего VPS.
- Доступ к серверу от пользователя `root`.
- Открытые `80/tcp` и `443/tcp`.
- Для Hysteria2 — `8443/udp`.
- Для MTProto — TCP-порты из диапазона `2399–2430`.
- Для OpenFlux — публичная ссылка на документ в классическом редакторе Яндекс Документов. Дополнительный входящий порт не требуется.

## Установка

Подключитесь к VPS по SSH, перейдите в режим `root` и выполните одну команду:

```bash
apt-get -o DPkg::Lock::Timeout=600 update && apt-get -o DPkg::Lock::Timeout=600 install -y curl ca-certificates git && WEB_PANEL_PROXY_REF=v2.3.0 bash -c "$(curl -fsSL --proto '=https' --tlsv1.2 https://raw.githubusercontent.com/POLESNIESOVETI12/web-panel-proxy/v2.3.0/install.sh)"
```

Во время установки потребуется указать:

1. Домен панели.
2. Email для HTTPS-сертификата.
3. Логин администратора.
4. Пароль длиной не менее 3 символов.

После завершения установщик покажет URL панели и данные для входа. Сохраните их в безопасном месте.

## Обновление

Обновление до последнего опубликованного стабильного релиза:

```bash
sudo /usr/local/sbin/web-panel-proxy-update
```

Пользователи, ключи, пароль администратора, адрес панели и HTML-заглушка сохраняются. Перед обновлением автоматически создаётся резервная копия.

Также обновление можно запустить через меню:

```bash
sudo WPP
```

Выберите пункт **Обновить**.

## Удаление

Полное удаление WEB PANEL PROXY:

```bash
sudo /usr/local/sbin/web-panel-proxy-uninstall
```

Команда удаляет пользователей, ключи, конфигурации, службы, правила firewall и сайт проекта без дополнительного подтверждения. Если Caddy используется другими сайтами, скрипт постарается сохранить их конфигурацию.


## Ссылки

- [GitHub проекта](https://github.com/POLESNIESOVETI12/web-panel-proxy)
- [YouTube автора](https://www.youtube.com/@POLESNIESOVETI12)

## Лицензия

MIT License. Подробности находятся в файле [LICENSE](LICENSE).
