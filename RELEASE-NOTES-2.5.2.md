# WEB PANEL PROXY 2.5.2

Патч совместимости VLESS XHTTP с VK Cloud CDN.

## Исправлено

- Для VK Cloud CDN создан отдельный локальный Xray inbound на `127.0.0.1:10004`.
- Восходящие данные XHTTP больше не помещаются в динамические заголовки `X-Data-*`, которые могут не проходить через VK CDN.
- VK использует согласованный на клиенте и сервере профиль `packet-up`, GET с данными в теле, Session ID и Sequence в query-параметрах.
- Добавлен отдельный статический маршрут вида `/media/hls/playlist.m3u8`.
- CDN Doctor проверяет отдельный VK inbound.

После обновления сохраните VK-подключение и заново импортируйте выданную VLESS-ссылку в клиент.

## Установка

```bash
sudo -i bash -c 'curl -4fsSL "https://raw.githubusercontent.com/POLESNIESOVETI12/web-panel-proxy/v2.5.2/install.sh" -o /tmp/wpp-install.sh && bash /tmp/wpp-install.sh; code=$?; rm -f /tmp/wpp-install.sh; exit $code'
```
