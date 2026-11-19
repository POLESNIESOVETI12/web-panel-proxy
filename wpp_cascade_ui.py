"""Cascade editor assets and a browser-safe view of managed nodes."""
import json
from html import escape
from pathlib import Path
import re
from functools import lru_cache
from urllib.parse import urlsplit
import wpp_cascade_ops_ui
from wpp_cascade import ENTRY_PROTOCOLS
import wpp_ssh_cascade_ui


@lru_cache(maxsize=1)
def assets():
    root = Path(__file__).with_name('cascade-ui')
    if not root.is_dir():
        root = Path(__file__).parent / 'frontend' / 'cascade'
    return tuple((root / name).read_text(encoding='utf-8')
                 for name in ('editor.css', 'editor.html', 'production.js', 'methods.css', 'methods.html', 'methods.js'))


def render(connections, nodes, local, domain, path, csrf, standalone=False, include_monitor=True):
    css, markup, script, method_css, method_markup, method_script = assets()
    def node_view(node, nid, host):
        code = str(node.get('country_code', 'UN')).lower()
        if not re.fullmatch(r'[a-z]{2}', code): code = 'un'
        return {'id': nid, 'country': str(node.get('country_name') or node.get('name') or 'Сервер'),
                'country_code': code.upper(), 'flag': path + '/__flag/' + code + '.svg', 'host': host}
    options = [node_view(local, 'local', domain)]
    for node in nodes:
        if not node.get('enabled', True):
            continue
        host = urlsplit(str(node.get('url', ''))).hostname
        if host:
            options.append(node_view(node, str(node.get('id', '')), host))
    records = [{'id': item['id'], 'name': item['name'], 'protocol': item['protocol'],
                'cascade': item.get('config', {}).get('cascade', {'mode': 'direct', 'nodes': []}),
                'ssh_entry': bool(item.get('config', {}).get('ssh_entry')),
                'route': item.get('config', {}).get('route', 'direct')}
               for item in connections if item.get('protocol') in ENTRY_PROTOCOLS]
    data = json.dumps({'nodes': options, 'connections': records, 'path': path, 'csrf': csrf,'standalone':standalone},
                      ensure_ascii=False).replace('<', '\\u003c')
    markup = markup.replace('<span>ПРЕДПРОСМОТР</span>', '')
    markup = re.sub(r'<section id="cascadeGeneral".*?</section>', '', markup, flags=re.S)
    markup = markup.replace('aria-controls="cascadeGeneral"', 'aria-controls="connectionEditor"')
    markup = markup.replace('Сохранить черновик', 'Применить цепочку').replace('Проверить выбор', 'Проверить цепочку')
    markup = markup.replace('Макет интерфейса · серверы не изменяются', 'Настройки только для выбранного подключения')
    return '<style>' + css + method_css + '</style>' + markup + method_markup + '<script type="application/json" data-cascade-config>' + data + '</script><script>' + script + '</script>'+wpp_ssh_cascade_ui.render(connections,domain,path,csrf,standalone=standalone)+'<script>'+method_script+'</script>'+(wpp_cascade_ops_ui.render(connections,nodes,domain,path,csrf) if include_monitor else '')


WORKSPACE_STYLE='<style>.cascade-workspace-head{display:flex;justify-content:space-between;align-items:center;gap:18px;margin-bottom:22px}.cascade-workspace-head h1{font-size:29px;line-height:1.25;margin:0}.cascade-workspace-head p{font-size:15px;color:var(--muted);margin:8px 0 0;line-height:1.6}@media(max-width:650px){.cascade-workspace-head{align-items:flex-start;flex-direction:column}.cascade-workspace-head .btn{width:100%}}</style>'


def page(connections,nodes,local,domain,path,csrf):
    """Dedicated workspace; only public connection metadata reaches the page."""
    esc=lambda value:escape(str(value),quote=True)
    cards=[]
    for row in connections:
        if row.get('protocol') not in ENTRY_PROTOCOLS:continue
        config=row.get('config',{});plan=config.get('cascade',{})
        protocol='Hysteria2' if row['protocol']=='hysteria' else 'VLESS XHTTP'
        method='RU-вход по SSH' if config.get('ssh_entry') else 'Через панели' if plan.get('mode','direct')!='direct' else 'Мост не настроен'
        note='Вход: '+str(config['ssh_entry'].get('host',''))+':'+str(config['ssh_entry'].get('port',443)) if config.get('ssh_entry') else '3 сервера в цепочке' if plan.get('mode')=='three' else '2 сервера в цепочке' if plan.get('mode')=='two' else 'Выберите способ и серверы для этого подключения'
        cards.append('<article class="cascade-connection"><div><h2>'+esc(row['name'])+'</h2><p>'+esc(protocol)+' · '+esc(method)+' · '+esc(note)+'</p></div><button type="button" data-open-cascade="'+esc(row['id'])+'">Настроить мост</button></article>')
    content=''.join(cards) or '<div class="card"><h2>Нет подключений VLESS или Hysteria2</h2><p class="muted">Сначала создайте серверное подключение, затем выберите для него мост.</p><a class="btn primary" href="'+esc(path)+'/connections">Открыть подключения</a></div>'
    header='<div class="cascade-workspace-head"><div><h1>Мост</h1><p>Настройка цепочки серверов для готовых подключений</p></div><a class="btn" href="'+esc(path)+'/monitoring">Открыть мониторинг</a></div>'
    overview='<div class="note">Через панели: устройство → входная панель → выходная нода. По SSH: устройство → RU-1 → RU-2 при необходимости → зарубежный VPS с этой панелью.</div>'
    bridge='<form id="connectionForm" hidden><input name="id" type="hidden"><input name="protocol" type="hidden" value="vless"></form>'
    styles='<style>.cascade-workspace-list{display:grid;gap:14px}.cascade-connection{display:flex;justify-content:space-between;align-items:center;gap:20px;padding:24px;border:1px solid var(--line);border-radius:14px;background:var(--surface)}.cascade-connection h2{font-size:18px;margin:0}.cascade-connection p{font-size:14px;color:var(--muted);margin:8px 0 0}@media(max-width:650px){.cascade-connection{align-items:stretch;flex-direction:column;padding:18px}.cascade-connection button{width:100%}}</style>'
    return WORKSPACE_STYLE+styles+'<section id="connectionEditor">'+header+overview+bridge+'<div class="cascade-workspace-list">'+content+'</div></section>'+render(connections,nodes,local,domain,path,csrf,standalone=True,include_monitor=False)


def connection_shortcut(path):
    data=json.dumps(path).replace('<','\\u003c')
    return '''<script>(()=>{const base=document.getElementById('connectionEditor'),form=document.getElementById('connectionForm');if(!base||!form)return;const link=document.createElement('a');link.className='btn';link.textContent='Мост';link.hidden=true;base.querySelector('.dialog-head').insertAdjacentElement('afterend',link);let baseline='';const snapshot=()=>new URLSearchParams(new FormData(form)).toString();new MutationObserver(()=>{if(base.open){link.hidden=!['vless','hysteria'].includes(form.elements.protocol.value)||!form.elements.id.value;link.href=PATH+'/cascade?connection='+encodeURIComponent(form.elements.id.value);queueMicrotask(()=>baseline=snapshot())}}).observe(base,{attributes:true,attributeFilter:['open']});link.addEventListener('click',event=>{if(snapshot()!==baseline){event.preventDefault();window.showPanelToast?.('Сначала сохраните основные настройки подключения.','error')}})})();</script>'''.replace('PATH',data)


def monitoring_page(connections,nodes,domain,path,csrf):
    return WORKSPACE_STYLE+'<div class="cascade-workspace-head"><div><h1>Мониторинг</h1><p>Проверки подключений, состояние серверов и уведомления Telegram</p></div></div>'+wpp_cascade_ops_ui.render(connections,nodes,domain,path,csrf)
