"""Panel-only HTML helpers. No public subscription request may render admin data."""
import base64
import html
import time


def esc(value):
    return html.escape(str(value), quote=True)


def stamp(value):
    return time.strftime('%d.%m.%Y %H:%M UTC', time.gmtime(value)) if value else '—'


def subscription_ui(subs, path, domain, csrf, traffic, human_bytes):
    path, token = esc(path), esc(csrf)

    def hidden(sub, op):
        return (f'<input type="hidden" name="csrf" value="{token}">'
                f'<input type="hidden" name="id" value="{esc(sub.get("id", ""))}">'
                f'<input type="hidden" name="operation" value="{op}">')

    def checks(sub):
        return ''.join(f'<label class="check"><input type="checkbox" name="{p}" value="1" '
                       f'{"checked" if p in sub.get("protocols", ["vless", "hysteria"]) else ""}> {title}</label>'
                       for p, title in [('vless', 'VLESS XHTTP'), ('hysteria', 'Hysteria2')])

    cards = []
    for sub in subs:
        sid = esc(sub['id'])
        url = 'https://' + domain + '/wpp-sub/' + sub['token']
        active = sum(not d.get('revoked') for d in sub['devices'])
        limit = sub['max_devices']
        total = sum(int(traffic.get(uid, {}).get('up', 0)) + int(traffic.get(uid, {}).get('down', 0))
                    for uid in sub.get('profile_ids', []))
        devices = []
        for d in sub['devices']:
            revoked = d.get('revoked', False)
            devices.append(f'''<div class="device"><div><b>{esc(d['name'])}</b>
<small>{'Отозвано' if revoked else 'Разрешено'} · ID {esc(d['id'])}<br>Обновление подписки: {stamp(d.get('last_seen'))}</small></div>
<form method="post" action="{path}/subscription-action">{hidden(sub, 'allow' if revoked else 'revoke')}
<input type="hidden" name="device_id" value="{esc(d['id'])}"><button class="btn {'danger' if not revoked else ''}">{'Разрешить регистрацию' if revoked else 'Отозвать устройство'}</button></form></div>''')
        cards.append(f'''<article class="card subscription-card"><div class="sub-title"><h3>{esc(sub['name'])}</h3><span class="status {'on' if sub['enabled'] else 'off'}">{'Включена' if sub['enabled'] else 'Отключена'}</span></div>
<p class="muted">{'HWID: '+str(active)+' / '+str(limit) if limit else 'Без лимита HWID · общий ключ'} · Трафик: {human_bytes(total)}</p>
<label>Единая ссылка подписки</label><input class="sub-url" value="{esc(url)}" readonly aria-label="Ссылка подписки {esc(sub['name'])}">
<div class="actions"><button type="button" class="btn primary copy-sub" data-link="{esc(url)}">Скопировать</button><button type="button" class="btn show-sub-qr" data-src="{path}/subscription-qr?id={sid}">QR подписки</button></div>
<details><summary>Настройки и устройства</summary><form method="post" action="{path}/subscription-action">{hidden(sub,'update')}
<label>Имя</label><input name="name" value="{esc(sub['name'])}" maxlength="80" required>
<label>Максимум устройств (HWID), 0 — без привязки</label><input type="number" name="max_devices" min="0" max="20" value="{limit}" required>
<div class="checks">{checks(sub)}</div><button class="btn primary">Сохранить настройки</button></form>
<p class="muted">При переходе между 0 и HWID-лимитом старые ключи отзываются. «Обновление подписки» не означает, что устройство сейчас онлайн.</p>
<div class="devices">{''.join(devices) or '<p class="muted">Устройства появятся после импорта подписки в клиент.</p>'}</div>
<div class="actions"><form method="post" action="{path}/subscription-action">{hidden(sub,'toggle')}<button class="btn">{'Отключить подписку' if sub['enabled'] else 'Включить подписку'}</button></form>
<form method="post" action="{path}/subscription-action" data-confirm="Сменить ссылку и отозвать ключи всех устройств?">{hidden(sub,'rotate')}<button class="btn danger">Сменить ссылку и сбросить устройства</button></form>
<form method="post" action="{path}/subscription-action" data-confirm="Удалить подписку и отозвать все её ключи?">{hidden(sub,'delete')}<button class="btn danger">Удалить</button></form></div></details></article>''')
    return f'''<style>
.sub-title,.device{{display:flex;align-items:center;justify-content:space-between;gap:16px}}.sub-title h3{{margin:0}}.actions,.checks{{display:flex;flex-wrap:wrap;gap:10px;margin:12px 0}}.check{{display:flex;gap:8px;align-items:center}}.check input{{width:auto;margin:0}}.device{{padding:14px 0;border-top:1px solid #ffffff12}}.device small{{display:block;color:#aab4c9;line-height:1.6}}.device form{{flex-shrink:0}}summary{{cursor:pointer;padding:16px 0;color:#c5baff}}.subscription-card{{margin-bottom:16px}}.sub-url{{font-size:12px;min-width:0}}.sub-qr{{max-width:260px;width:100%;background:#fff;padding:12px;border-radius:12px}}@media(max-width:600px){{.device{{align-items:flex-start;flex-direction:column}}}}
</style><div class="topbar"><div><h1>Подписки</h1><p>Одна ссылка: VLESS XHTTP + Hysteria2</p></div></div>
<div class="card"><p>Лимит учитывает HWID, переданный клиентом (например, Happ), а не IP и не число соединений. Клиенты без HWID при включённом лимите не получат конфигурацию. Для каждого устройства создаются отдельные ключи.</p>
<p class="muted">HWID можно подделать, а полученный ключ — скопировать. Это контроль регистрации и отзыва, не гарантированная аппаратная привязка. WEB Proxy остаётся отдельной Telegram-ссылкой. Первая регистрация и отзыв устройства кратковременно перезапускают Xray.</p></div>
<div class="card"><h3>Новая подписка</h3><form method="post" action="{path}/subscription-action">{hidden({},'create')}
<label>Имя пользователя</label><input name="name" maxlength="80" placeholder="Например, Александр" required>
<label>Максимум устройств (HWID), 0 — без привязки</label><input type="number" name="max_devices" value="2" min="0" max="20" required>
<div class="checks">{checks({})}</div><button class="btn primary">Создать подписку</button></form></div>
{''.join(cards) or '<div class="card muted">Подписок пока нет. Старые отдельные подключения сохранены во вкладке «Пользователи».</div>'}
<dialog id="subQrDialog" style="border:0;border-radius:20px;padding:24px"><img id="subQrImg" class="sub-qr" alt="QR подписки"><p><button class="btn" id="closeSubQr">Закрыть</button></p></dialog>
<script>
document.querySelectorAll('.copy-sub').forEach(b=>b.addEventListener('click',async()=>{{try{{await navigator.clipboard.writeText(b.dataset.link);b.textContent='Скопировано ✓'}}catch(e){{b.closest('article').querySelector('.sub-url').select();document.execCommand('copy');b.textContent='Скопировано ✓'}}}}));
document.querySelectorAll('.show-sub-qr').forEach(b=>b.addEventListener('click',()=>{{document.getElementById('subQrImg').src=b.dataset.src;document.getElementById('subQrDialog').showModal()}}));
document.getElementById('closeSubQr').addEventListener('click',()=>document.getElementById('subQrDialog').close());
document.querySelectorAll('form[data-confirm]').forEach(f=>f.addEventListener('submit',e=>{{if(!confirm(f.dataset.confirm))e.preventDefault()}}));
</script>'''


def editor_ui(source, path, csrf, presets, has_draft):
    buttons = ''.join(f'<form method="post" action="{esc(path)}/draft-preset" class="preset-card">'
                      f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
                      f'<input type="hidden" name="preset" value="{esc(p["id"])}">'
                      f'<b>{esc(p["name"])}</b><p class="muted">{esc(p["description"])}</p>'
                      '<button class="btn">Открыть в черновике</button></form>' for p in presets)
    return f'''<style>.editor-actions{{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0}}.preview-wrap{{overflow:auto;padding:12px;background:#090d18;border-radius:16px}}#landingPreview{{display:block;width:100%;height:660px;max-width:100%;border:0;margin:0 auto;background:white}}#landingPreview.phone{{width:390px;height:720px}}#htmlSource{{width:100%;font:12px/1.6 ui-monospace,monospace}}#previewStatus{{min-height:24px;color:#bdcce9}}</style>
<div class="card"><h3>Пресеты заглушек</h3><p class="muted">Пресет открывается в черновике. Рабочий сайт изменится только после публикации. Текущий черновик будет заменён.</p><div class="preset-grid">{buttons}</div></div>
<div class="card"><h3>HTML главной страницы · {'черновик' if has_draft else 'опубликованный исходник'}</h3>
<p class="muted">Предпросмотр изолирован от панели. Внешние ресурсы, формы и сетевые запросы заблокированы; стили и скрипты должны быть внутри HTML. В живом сайте ограничения могут отличаться — после публикации проверьте страницу.</p>
<form id="editorForm" method="post" action="{esc(path)}/site-html"><input type="hidden" name="csrf" value="{esc(csrf)}">
<textarea id="htmlSource" name="html" rows="20" spellcheck="false" required>{esc(source)}</textarea>
<div class="editor-actions"><button class="btn" formaction="{esc(path)}/save-draft">Сохранить черновик</button><button class="btn" type="button" id="previewHtml">Предпросмотр</button><button class="btn primary" id="publishHtml">Опубликовать на сайте</button></div></form>
<form method="post" action="{esc(path)}/discard-draft"><input type="hidden" name="csrf" value="{esc(csrf)}"><button class="btn" onclick="return confirm('Удалить черновик и вернуться к опубликованному исходнику?')">Удалить черновик</button></form>
<div class="editor-actions"><button type="button" class="btn" id="previewDesktop">Компьютер</button><button type="button" class="btn" id="previewPhone">Телефон</button></div>
<p id="previewStatus">Нажмите «Предпросмотр», чтобы увидеть текущий код без публикации.</p><div class="preview-wrap"><iframe id="landingPreview" title="Изолированный предпросмотр заглушки" sandbox="allow-scripts" referrerpolicy="no-referrer"></iframe></div></div>
<script>
const ef=document.getElementById('editorForm'),pf=document.getElementById('landingPreview'),ps=document.getElementById('previewStatus');
document.getElementById('previewHtml').addEventListener('click',async()=>{{ps.textContent='Подготовка…';try{{const body=new URLSearchParams(new FormData(ef));const r=await fetch('{esc(path)}/preview-html',{{method:'POST',body}});const d=await r.json();if(!r.ok)throw new Error(d.message||'Ошибка предпросмотра');pf.srcdoc=d.document;ps.textContent='Предпросмотр обновлён. Рабочий сайт не изменён.'}}catch(e){{ps.textContent=e.message}}}});
document.getElementById('previewPhone').addEventListener('click',()=>pf.classList.add('phone'));
document.getElementById('previewDesktop').addEventListener('click',()=>pf.classList.remove('phone'));
ef.addEventListener('submit',e=>{{if(e.submitter&&e.submitter.id==='publishHtml'&&!confirm('Опубликовать текущий HTML на основном сайте?'))e.preventDefault()}});
</script>'''


def preview_document(source, externalize):
    rendered, css, js, css_name, js_name = externalize(source)
    for name, value, mime in [(css_name, css, 'text/css'), (js_name, js, 'text/javascript')]:
        if name:
            data = 'data:' + mime + ';base64,' + base64.b64encode(value.encode()).decode()
            rendered = rendered.replace('/' + name, data)
    # First CSP is enforced even if untrusted HTML includes another, weaker CSP.
    # Only generated data assets run. No unsafe-inline, same-origin or network.
    csp = "default-src 'none'; script-src data:; style-src data:; img-src data:; font-src data:; connect-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'"
    return '<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="' + csp + '">' + rendered
