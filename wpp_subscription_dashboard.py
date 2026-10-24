"""Read-only public dashboard shown when a subscription URL opens in a browser."""

import datetime
import html
from urllib.parse import urlparse


def _esc(value):
    return html.escape(str(value), quote=True)


def _bytes(value):
    try:
        size = max(0, int(value or 0))
    except (TypeError, ValueError):
        size = 0
    units = ("Б", "КБ", "МБ", "ГБ", "ТБ")
    number = float(size)
    for unit in units:
        if number < 1024 or unit == units[-1]:
            return (("%.0f" % number) if unit == "Б" else ("%.1f" % number)) + " " + unit
        number /= 1024


def _date(value, empty="Нет данных"):
    try:
        stamp = int(value or 0)
    except (TypeError, ValueError):
        stamp = 0
    if stamp <= 0:
        return empty
    return datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc).strftime("%d.%m.%Y · %H:%M UTC")


def _connection_meta(connection):
    protocol = str(connection.get("protocol", "")).lower()
    if protocol == "hysteria":
        return "Hysteria2", "UDP · прямое подключение"
    cdn = connection.get("cdn") if isinstance(connection.get("cdn"), dict) else None
    if not cdn:
        return "VLESS XHTTP", "Прямое подключение"
    provider = str(cdn.get("provider") or cdn.get("provider_name") or "CDN").strip()
    mode = str(cdn.get("mode") or "XHTTP").strip()
    return "VLESS XHTTP", provider + " · " + mode


def render(subscription, profiles, traffic_state, public_url="", qr_data_uri="", config_items=None,
           logo_data_uri="", action_url="", action_token=""):
    """Render public information without issuing keys or mutating device state."""
    sid = str(subscription.get("id", ""))
    profiles = [p for p in profiles if isinstance(p, dict) and str(p.get("subscription_id", "")) == sid]
    traffic_state = traffic_state if isinstance(traffic_state, dict) else {}
    uploaded = 0
    downloaded = 0
    connection_traffic = {}
    for profile in profiles:
        counters = traffic_state.get(str(profile.get("id", "")), {})
        counters = counters if isinstance(counters, dict) else {}
        up = max(0, int(counters.get("up", 0) or 0))
        down = max(0, int(counters.get("down", 0) or 0))
        uploaded += up
        downloaded += down
        connection_id = str(profile.get("connection_id", ""))
        if connection_id:
            item = connection_traffic.setdefault(connection_id, {"up": 0, "down": 0})
            item["up"] += up
            item["down"] += down

    all_devices = [d for d in subscription.get("devices", []) if isinstance(d, dict)]
    devices = [d for d in all_devices if not d.get("revoked")]
    last_seen = max([int(d.get("last_seen", 0) or 0) for d in devices] + [0])
    max_devices = max(0, int(subscription.get("max_devices", 0) or 0))
    device_value = (str(len(devices)) + " / " + str(max_devices)) if max_devices else "Общий профиль"
    device_hint = "Привязка по X-HWID" if max_devices else "Без привязки к устройству"
    device_cards = []
    if max_devices:
        for device in all_devices:
            revoked = bool(device.get("revoked"))
            did = _esc(device.get("id", ""))
            name = _esc(device.get("name", "Устройство"))
            state = "Доступ отозван" if revoked else "Подключено"
            state_class = " revoked" if revoked else ""
            last_seen_text = _date(device.get("last_seen", 0), "Ещё не использовалось")
            created_text = _date(device.get("created_at", 0), "Нет данных")
            controls = ""
            if not revoked and action_url and action_token:
                controls = ('<div class="device-actions"><form data-device-action="rename-device">'
                            '<input type="hidden" name="device_id" value="%s"><input name="name" value="%s" '
                            'maxlength="40" aria-label="Имя устройства"><button class="mini">Сохранить</button></form>'
                            '<button class="mini danger" data-revoke-device="%s">Отозвать</button></div>'
                            % (did, name, did))
            device_cards.append('<article class="device-card%s"><div class="device-copy"><span class="device-state">%s</span>'
                                '<strong>%s</strong><small>Добавлено: %s · последняя активность: %s</small></div>%s</article>'
                                % (state_class, state, name, _esc(created_text), _esc(last_seen_text), controls))
        if not device_cards:
            device_cards.append('<div class="empty">Устройства появятся после добавления подписки в Happ.</div>')

    expires_at = int(subscription.get("expires_at", 0) or 0)
    expires_value = _date(expires_at, "Без срока")
    total_limit = max(0, int(subscription.get("traffic_limit", 0) or 0))
    total_used = uploaded + downloaded
    limit_value = (_bytes(total_used) + " / " + _bytes(total_limit)) if total_limit else "Без лимита"

    config_by_connection = {
        str(item.get("connection_id", "")): item for item in (config_items or []) if isinstance(item, dict)
    }
    connection_cards = []
    config_cards = []
    for connection in subscription.get("connections", []):
        if not isinstance(connection, dict):
            continue
        protocol, detail = _connection_meta(connection)
        counters = connection_traffic.get(str(connection.get("id", "")), {"up": 0, "down": 0})
        up = counters["up"]
        down = counters["down"]
        connection_cards.append(
            '<div class="connection"><div class="protocol">%s</div><strong>%s</strong><span>%s</span>'
            '<div class="connection-traffic"><b>%s</b><small>↑ %s&nbsp;&nbsp;·&nbsp;&nbsp;↓ %s</small></div></div>'
            % (_esc(protocol), _esc(connection.get("name", "Подключение")), _esc(detail),
               _esc(_bytes(up + down)), _esc(_bytes(up)), _esc(_bytes(down)))
        )
        config = config_by_connection.get(str(connection.get("id", "")), {})
        link = str(config.get("link", ""))
        qr = str(config.get("qr", ""))
        if link and qr:
            controls = ('<button class="mini" data-copy-config data-link="%s">Копировать</button>'
                        '<button class="mini" data-qr-config data-name="%s" data-qr="%s">QR</button>'
                        % (_esc(link), _esc(connection.get("name", "Конфиг")), _esc(qr)))
        else:
            controls = '<span class="config-note">Импортируйте подписку в приложение</span>'
        config_cards.append(
            '<div class="config-row"><div class="config-main"><div><b>%s</b> <b>%s</b></div>'
            '<strong>%s</strong><span>%s</span></div><div class="config-actions">%s</div></div>'
            % (_esc("Hysteria2" if protocol == "Hysteria2" else "VLESS"),
               _esc("UDP" if protocol == "Hysteria2" else "XHTTP"), _esc(connection.get("name", "Подключение")),
               _esc(_bytes(up + down) + " · ↑ " + _bytes(up) + " · ↓ " + _bytes(down)), controls)
        )
    if not connection_cards:
        connection_cards.append('<div class="empty">В подписке пока нет доступных подключений.</div>')
        config_cards.append('<div class="empty">В подписке пока нет доступных конфигов.</div>')

    safe_host = urlparse(public_url).hostname or "сервер подписки"
    title = _esc(subscription.get("name", "Подписка"))
    created = _date(subscription.get("created_at", 0))
    activity = _date(last_seen, "Ещё не использовалась")
    state_text = "Доступна" if subscription.get("enabled", True) else "Отключена"
    state_class = "ok" if subscription.get("enabled", True) else "off"
    display_url = public_url or "https://" + safe_host + "/wpp-sub/…"
    happ_url = "happ://add/" + display_url
    qr_markup = ('<img src="%s" alt="QR-код подписки">' % _esc(qr_data_uri)) if qr_data_uri else '<div class="qr-empty">QR</div>'
    logo_markup = ('<img src="%s" alt="">' % _esc(logo_data_uri)) if logo_data_uri else "WPP"

    return """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s · подписка</title>
<style>
:root{color-scheme:dark;--bg:#090d13;--panel:#151b24;--panel2:#111720;--line:#293445;--text:#f2f5fb;--muted:#8f9bb0;--blue:#3b82f6;--green:#35d399}[hidden]{display:none!important}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(circle at 12%% -10%%,#17325a 0,transparent 33%%),radial-gradient(circle at 100%% 15%%,#201d4a 0,transparent 30%%),var(--bg);font:14px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--text)}
.wrap{width:min(980px,calc(100%% - 32px));margin:0 auto;padding:42px 0 54px}.top{display:flex;align-items:center;justify-content:space-between;gap:18px;margin-bottom:22px}.brand{display:flex;align-items:center;gap:12px;color:#c9d4e7;font-weight:700;letter-spacing:.02em}.mark{display:grid;place-items:center;width:42px;height:42px;border:1px solid #315c9b;border-radius:12px;background:#122442;color:#66a3ff;font-size:16px;overflow:hidden}.mark img{display:block;width:100%%;height:100%%;object-fit:cover}.status{display:flex;align-items:center;gap:8px;padding:8px 12px;border:1px solid var(--line);border-radius:999px;background:#121821;color:#b7c2d3;font-size:12px}.dot{width:8px;height:8px;border-radius:50%%;background:var(--green);box-shadow:0 0 0 4px #35d39918}.status.off .dot{background:#ff6375;box-shadow:0 0 0 4px #ff637518}
.hero,.block{border:1px solid var(--line);background:linear-gradient(145deg,#18212eeb,#121821f5);box-shadow:0 22px 70px #0005}.hero{border-radius:24px;padding:30px}.eyebrow{margin-bottom:7px;color:#6ea8ff;font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:.1em}h1{margin:0;font-size:clamp(26px,5vw,40px);line-height:1.15;letter-spacing:-.035em}.subline{margin:10px 0 24px;color:var(--muted)}.actions{display:flex;flex-wrap:wrap;gap:10px}.button{appearance:none;border:1px solid #4b8df8;border-radius:12px;background:var(--blue);color:white;padding:11px 16px;font:inherit;font-weight:750;cursor:pointer}.button.secondary{border-color:var(--line);background:#151c26;color:#d9e2ef}.button.done{border-color:#249f77;background:#16765b}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:18px}.stat{min-width:0;border:1px solid #293647;border-radius:15px;background:#10161f;padding:16px}.stat span{display:block;color:var(--muted);font-size:12px}.stat strong{display:block;margin-top:7px;font-size:18px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.tabs{display:flex;gap:5px;margin:20px 0 0;padding:5px;border:1px solid var(--line);border-radius:14px;background:#0e141d;overflow:auto}.tab{flex:1;min-width:max-content;border:0;border-radius:10px;background:transparent;color:#8f9bb0;padding:10px 14px;font:inherit;font-weight:700;cursor:pointer}.tab.active{background:#172b49;color:#72aaff}.panel{display:none}.panel.active{display:block}.layout{display:grid;grid-template-columns:1.45fr .8fr;gap:18px;margin-top:18px}.block{border-radius:20px;padding:22px}.block h2{margin:0 0 16px;font-size:17px}.connections{display:grid;gap:10px}.connection{position:relative;display:grid;gap:3px;border:1px solid #293647;border-radius:14px;background:#10161f;padding:15px 16px}.connection>strong{padding-right:100px}.connection>span{color:var(--muted);font-size:12px}.protocol{position:absolute;top:13px;right:13px;padding:4px 8px;border-radius:8px;background:#14294a;color:#65a4ff;font-size:10px;font-weight:800}.connection-traffic{display:flex;align-items:end;justify-content:space-between;gap:14px;margin-top:12px;padding-top:11px;border-top:1px solid #25303f}.connection-traffic b{font-size:15px}.connection-traffic small{color:#8090a8;text-align:right}.details{display:grid;gap:0}.row{display:flex;justify-content:space-between;gap:18px;padding:13px 0;border-bottom:1px solid #273141}.row:last-child{border-bottom:0}.row span{color:var(--muted)}.row strong{text-align:right}.subscription-row{display:flex;align-items:center;gap:12px;border:1px solid #2c3849;border-radius:14px;background:#10161f;padding:14px}.sub-icon{display:grid;place-items:center;width:42px;height:42px;border-radius:11px;background:#153c22;color:#54d576;font-weight:800}.sub-value{min-width:0;flex:1}.sub-value strong{display:block}.sub-value code{display:block;color:#8f9bb0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.qr-box{display:flex;align-items:center;gap:22px;margin-top:15px;border:1px dashed #31517f;border-radius:16px;background:#101720;padding:17px}.qr-box img,.qr-empty{width:132px;height:132px;border-radius:10px;background:white;padding:8px;object-fit:contain}.qr-empty{display:grid;place-items:center;color:#111;font-size:25px;font-weight:900}.qr-copy h3{margin:0 0 7px}.qr-copy p{margin:0;color:var(--muted)}.apps{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}.app-card{border:1px solid #293647;border-radius:14px;background:#10161f;padding:16px}.app-card strong{display:block}.app-card span{display:block;margin-top:4px;color:var(--muted);font-size:12px}.config-list{display:grid;gap:9px}.config-row{display:flex;align-items:center;gap:14px;border:1px solid #293647;border-radius:14px;background:#10161f;padding:12px}.config-main{min-width:0;flex:1}.config-main>div{display:flex;gap:6px}.config-main>div b{padding:3px 7px;border-radius:6px;background:#14294a;color:#72aaff;font-size:10px;text-transform:uppercase}.config-main>strong{display:block;margin-top:6px}.config-main>span{display:block;color:var(--muted);font-size:11px}.config-actions{display:flex;gap:7px}.mini{border:1px solid #35445a;border-radius:9px;background:#151d28;color:#dce5f2;padding:8px 10px;font:inherit;font-size:12px;font-weight:700;cursor:pointer}.mini.done{border-color:#249f77;color:#55d4aa}.config-note{max-width:150px;color:#7f8ca1;font-size:11px;text-align:right}.qr-modal{position:fixed;inset:0;z-index:50;display:grid;place-items:center;padding:20px;background:#03060acc;backdrop-filter:blur(8px)}.qr-dialog{position:relative;width:min(390px,100%%);border:1px solid #34445b;border-radius:20px;background:#151c26;padding:28px;text-align:center;box-shadow:0 30px 100px #000}.qr-dialog img{display:block;width:230px;height:230px;margin:16px auto 10px;border-radius:14px;background:white;padding:10px}.qr-dialog h3{margin:0;font-size:19px}.qr-dialog p{margin:0;color:var(--muted)}.modal-close{position:absolute;top:12px;right:12px;width:36px;height:36px;border:1px solid #364257;border-radius:10px;background:#111720;color:white;font-size:20px;cursor:pointer}.notice{margin-top:18px;padding:16px 18px;border:1px solid #28436b;border-radius:15px;background:#11223b;color:#c7dbfa}.notice strong{display:block;margin-bottom:4px;color:#f2f7ff}.notice p{margin:0;color:#9fb1ca}.foot{text-align:center;color:#68758a;font-size:12px;margin-top:20px}.empty{color:var(--muted);padding:14px}
@media(max-width:760px){.wrap{width:min(100%% - 20px,980px);padding-top:20px}.hero{padding:21px}.stats{grid-template-columns:repeat(2,1fr)}.layout{grid-template-columns:1fr}.top{margin-bottom:14px}.connection>strong{padding-right:0;padding-top:28px}.protocol{left:13px;right:auto}.row{align-items:flex-start}.row strong{max-width:58%%}.qr-box{align-items:flex-start}.apps{grid-template-columns:1fr}}@media(max-width:480px){.qr-box{flex-direction:column}.qr-box img,.qr-empty{width:116px;height:116px}.subscription-row{align-items:flex-start;flex-wrap:wrap}.sub-value{width:calc(100%% - 58px);flex:none}}
.subscription-row{border-color:#303b4b;border-radius:12px;background:#171d26;padding:13px}.sub-icon{width:40px;height:40px;border:1px solid #3266ad;border-radius:10px;background:#132c52;color:#67a6ff;font-size:11px;letter-spacing:.04em}.sub-value strong{font-size:13px}.sub-value code{margin-top:2px;color:#8795aa;font-size:11px}.qr-box{border-color:#315887;border-radius:12px;background:#171d26}.qr-box img,.qr-empty{border-radius:8px}.config-main>div b{border:1px solid #294e82;border-radius:5px;background:#132845;color:#67a6ff}.mini{border-radius:8px;background:#171d26}
.app-recommend{display:flex;align-items:center;justify-content:space-between;gap:18px;margin-top:15px;padding:16px;border:1px solid #315887;border-radius:12px;background:linear-gradient(135deg,#132845,#171d26)}.app-recommend strong,.app-recommend span{display:block}.app-recommend span{margin-top:3px;color:var(--muted);font-size:12px}.app-actions{display:flex;gap:8px;flex-wrap:wrap}.button-link{text-decoration:none;text-align:center}.failover-note{display:flex;align-items:center;gap:9px;margin-top:14px;color:#a9bdd8;font-size:12px}.failover-note i{width:8px;height:8px;border-radius:50%%;background:#35d399;box-shadow:0 0 0 4px #35d39918}.devices{display:grid;gap:9px}.device-card{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:13px;border:1px solid #303b4b;border-radius:12px;background:#171d26}.device-card.revoked{opacity:.7}.device-copy{min-width:0}.device-copy strong,.device-copy small{display:block}.device-copy small{margin-top:3px;color:var(--muted);font-size:11px}.device-state{display:inline-block;margin-bottom:5px;color:#51d7aa;font-size:10px;font-weight:800;text-transform:uppercase}.revoked .device-state{color:#ff7485}.device-actions,.device-actions form{display:flex;align-items:center;gap:7px}.device-actions input{width:170px;border:1px solid #35445a;border-radius:8px;background:#10161f;color:var(--text);padding:8px 9px;font:inherit;font-size:12px}.mini.danger{border-color:#663541;color:#ff7d8c}@media(max-width:650px){.app-recommend,.device-card{align-items:stretch;flex-direction:column}.device-actions,.device-actions form{width:100%%}.device-actions form{flex:1}.device-actions input{width:100%%;min-width:0}.app-actions .button{flex:1}}
</style></head><body><main class="wrap">
<div class="top"><div class="brand"><div class="mark">%s</div><span>WEB PANEL PROXY</span></div><div class="status %s"><i class="dot"></i>%s</div></div>
<section class="hero"><div class="eyebrow">Ваша подписка</div><h1>%s</h1><p class="subline">Все подключения и состояние доступа в одном месте.</p>
<div class="actions"><button class="button" data-copy-sub>Скопировать ссылку</button><button class="button secondary" data-open-tab="subscription">Показать QR</button><button class="button secondary" onclick="location.reload()">Обновить данные</button></div>
<div class="stats"><div class="stat"><span>Использовано</span><strong>%s</strong></div><div class="stat"><span>Получено</span><strong>↓ %s</strong></div><div class="stat"><span>Отправлено</span><strong>↑ %s</strong></div><div class="stat"><span>Устройства</span><strong>%s</strong></div><div class="stat"><span>Срок действия</span><strong>%s</strong></div><div class="stat"><span>Лимит трафика</span><strong>%s</strong></div></div></section>
<nav class="tabs" role="tablist"><button class="tab active" data-tab="overview">Обзор</button><button class="tab" data-tab="subscription">Подписка</button><button class="tab" data-tab="configs">Конфиги</button></nav>
<section class="panel active" data-panel="overview"><div class="layout"><section class="block"><h2>Доступные подключения</h2><div class="connections">%s</div></section>
<aside class="block"><h2>Информация</h2><div class="details"><div class="row"><span>Создана</span><strong>%s</strong></div><div class="row"><span>Последняя активность</span><strong>%s</strong></div><div class="row"><span>Режим устройств</span><strong>%s</strong></div><div class="row"><span>Сервер</span><strong>%s</strong></div></div></aside></div></section>
<section class="panel" data-panel="subscription"><div class="block" style="margin-top:18px"><h2>Ссылка подписки</h2><div class="subscription-row"><div class="sub-icon">SUB</div><div class="sub-value"><strong>Подписка</strong><code>%s</code></div><button class="button secondary" data-copy-sub>Копировать</button></div><div class="qr-box">%s<div class="qr-copy"><h3>Сканируйте телефоном</h3><p>Откройте сканер QR-кода в VPN-приложении, чтобы добавить подписку без ручного копирования.</p></div></div><div class="app-recommend"><div><strong id="app-title">Добавить в Happ</strong><span id="app-hint">Подходящая версия будет определена по вашему устройству.</span></div><div class="app-actions"><a class="button button-link" href="%s">Открыть в Happ</a><a class="button secondary button-link" href="https://happ.info/" target="_blank" rel="noopener">Скачать приложение</a></div></div>%s</div></section>
<section class="panel" data-panel="configs"><div class="block" style="margin-top:18px"><h2>Конфиги в подписке</h2><div class="config-list">%s</div></div></section>
%s
<div class="notice"><strong>Как подключиться</strong><p>Скопируйте ссылку и добавьте её в приложение как подписку. Эта страница не регистрирует новое устройство и не расходует доступный лимит.</p></div>
<div class="foot">HWID устройств не отображаются. Переименование и отзыв устройства не меняют ссылку подписки.</div>
<div class="qr-modal" id="config-qr-modal" hidden><div class="qr-dialog"><button class="modal-close" type="button" aria-label="Закрыть">×</button><h3 id="config-qr-title">QR конфигурации</h3><img id="config-qr-image" alt="QR-код конфигурации"><p>Отсканируйте код в VPN-приложении.</p></div></div>
</main><script>
const activate=name=>{document.querySelectorAll('[data-tab]').forEach(x=>x.classList.toggle('active',x.dataset.tab===name));document.querySelectorAll('[data-panel]').forEach(x=>x.classList.toggle('active',x.dataset.panel===name))};document.querySelectorAll('[data-tab]').forEach(x=>x.addEventListener('click',()=>activate(x.dataset.tab)));document.querySelectorAll('[data-open-tab]').forEach(x=>x.addEventListener('click',()=>{activate(x.dataset.openTab);document.querySelector('.tabs').scrollIntoView({behavior:'smooth'})}));const copyText=async(button,text)=>{const old=button.textContent;try{await navigator.clipboard.writeText(text);button.textContent='Скопировано';button.classList.add('done')}catch(_){prompt('Скопируйте конфигурацию',text)}setTimeout(()=>{button.textContent=old;button.classList.remove('done')},2200)};document.querySelectorAll('[data-copy-sub]').forEach(x=>x.addEventListener('click',()=>copyText(x,location.origin+location.pathname)));document.querySelectorAll('[data-copy-config]').forEach(x=>x.addEventListener('click',()=>copyText(x,x.dataset.link)));const modal=document.getElementById('config-qr-modal'),modalImage=document.getElementById('config-qr-image'),modalTitle=document.getElementById('config-qr-title');document.querySelectorAll('[data-qr-config]').forEach(x=>x.addEventListener('click',()=>{modalImage.src=x.dataset.qr;modalTitle.textContent=x.dataset.name;modal.hidden=false}));const closeModal=()=>{modal.hidden=true;modalImage.removeAttribute('src')};modal.querySelector('.modal-close').addEventListener('click',closeModal);modal.addEventListener('click',e=>{if(e.target===modal)closeModal()});document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!modal.hidden)closeModal()});
const ua=navigator.userAgent.toLowerCase(),appTitle=document.getElementById('app-title'),appHint=document.getElementById('app-hint');if(/iphone|ipad|ipod/.test(ua)){appTitle.textContent='Happ для iPhone и iPad';appHint.textContent='Нажмите кнопку — подписка откроется сразу в приложении.'}else if(/android/.test(ua)){appTitle.textContent='Happ для Android';appHint.textContent='Подписка будет передана установленному приложению Happ.'}else if(/windows/.test(ua)){appTitle.textContent='Happ Desktop для Windows';appHint.textContent='Если Happ уже установлен, импорт откроется автоматически.'}else if(/macintosh|mac os/.test(ua)){appTitle.textContent='Happ для macOS';appHint.textContent='Откройте подписку в установленном приложении Happ.'}
const actionUrl=%s,actionToken=%s;async function deviceAction(operation,values){const body=new URLSearchParams({operation,token:actionToken,...values});const response=await fetch(actionUrl,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded','X-WPP-Dashboard':'1'},body});let result={};try{result=await response.json()}catch(_){throw new Error('Сервер вернул некорректный ответ.')}if(!response.ok||!result.ok)throw new Error(result.message||'Изменение не применено.');location.reload()}document.querySelectorAll('[data-device-action]').forEach(form=>form.addEventListener('submit',async event=>{event.preventDefault();const button=form.querySelector('button');button.disabled=true;try{await deviceAction(form.dataset.deviceAction,Object.fromEntries(new FormData(form)))}catch(error){alert(error.message);button.disabled=false}}));document.querySelectorAll('[data-revoke-device]').forEach(button=>button.addEventListener('click',async()=>{if(!confirm('Отозвать доступ этого устройства? Оно перестанет получать конфигурации подписки.'))return;button.disabled=true;try{await deviceAction('revoke',{device_id:button.dataset.revokeDevice})}catch(error){alert(error.message);button.disabled=false}}));
</script></body></html>""" % (
        title, logo_markup, state_class, _esc(state_text), title, _esc(_bytes(total_used)), _esc(_bytes(downloaded)),
        _esc(_bytes(uploaded)), _esc(device_value), _esc(expires_value), _esc(limit_value),
        "".join(connection_cards), _esc(created), _esc(activity), _esc(device_hint), _esc(safe_host),
        _esc(display_url), qr_markup, _esc(happ_url),
        ('<div class="failover-note"><i></i>Автоматическое переключение между доступными подключениями включено</div>'
         if subscription.get("failover_enabled", True) else ''),
        "".join(config_cards),
        ('<section class="block" style="margin-top:18px"><h2>Устройства</h2><div class="devices">%s</div></section>' % "".join(device_cards)) if max_devices else '',
        repr(str(action_url)), repr(str(action_token))
    )
