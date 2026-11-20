"""The SSH ingress wizard exposes only public host and port data to the browser."""
import json
import wpp_ssh_cascade as ssh


def inline_fields():
    return '''<style>
#connectionForm [data-ssh-inline-box]{margin:18px 0 0;padding:18px;border:1px solid var(--line);border-radius:12px}
#connectionForm [data-ssh-inline-box][hidden],#connectionForm [data-ssh-inline-middle][hidden]{display:none}
#connectionForm [data-ssh-inline-box] h3{margin:0 0 10px}#connectionForm [data-ssh-inline-box] input{box-sizing:border-box;width:100%;min-width:0}
#connectionForm [data-ssh-inline-box] details{margin-top:14px}#connectionForm [data-ssh-inline-box] summary{cursor:pointer;color:var(--muted)}
#connectionForm [data-ssh-inline-box] details .connection-form-grid{margin-top:12px}
#connectionForm [data-ssh-inline-middle]{margin-top:14px;border:1px solid var(--line);border-radius:10px;padding:14px;min-width:0}
#connectionForm [data-ssh-inline-status]{white-space:pre-wrap;margin:14px 0 0;font-size:14px;line-height:1.5}
</style><section data-ssh-inline-box hidden>
<input type="hidden" name="ssh_inline" value="1" disabled>
<h3>Входной RU-сервер</h3><p class="note">Для моста укажите IP и пароль root сервера. Настройка и проверка интернета выполнятся при сохранении. Пустой IP — прямое подключение.</p>
<div class="connection-form-grid">
<label>IP сервера<input name="ssh_host" placeholder="Например, 203.0.113.10" autocomplete="off"></label>
<label>Пароль root<input name="ssh_password" type="password" autocomplete="new-password" placeholder="Пароль RU-сервера"></label>
</div><details><summary>Дополнительно</summary><div class="connection-form-grid">
<label>Порт SSH<input name="ssh_port" type="number" min="1" max="65535" value="22"></label>
<label>Входной порт<input name="ssh_listen_port" type="number" min="1" max="65535" value="443"></label>
<label>IP выходного VPS<input name="ssh_exit_ip" placeholder="Определится автоматически" autocomplete="off"></label>
</div></details>
<label style="display:flex;gap:10px;align-items:center;margin-top:16px"><input name="ssh_double_hop" type="checkbox" value="1" style="width:auto"> Добавить второй RU-сервер</label>
<fieldset data-ssh-inline-middle hidden><legend>Второй RU-сервер</legend><div class="connection-form-grid">
<label>IP второго сервера<input name="ssh_middle_host" autocomplete="off"></label>
<label>Пароль root второго сервера<input name="ssh_middle_password" type="password" autocomplete="new-password"></label>
</div><details><summary>Дополнительно для второго сервера</summary><div class="connection-form-grid">
<label>Порт SSH<input name="ssh_middle_port" type="number" min="1" max="65535" value="22"></label>
<label>Входной порт<input name="ssh_middle_listen_port" type="number" min="1" max="65535" value="443"></label>
</div></details></fieldset>
<p class="note">Пароли не сохраняются. После переустановки сервера введите новый пароль и сохраните подключение ещё раз.</p>
<p data-ssh-inline-status role="status" hidden></p></section>'''


def inline_script(connections,path):
    try:state=ssh.load('/etc/web-proxy-panel/ssh-cascades.json')
    except ssh.SSHError:state={'servers':{},'routes':{}}
    records=[]
    for item in connections:
        if item.get('protocol') not in ('vless','hysteria'):continue
        entry=item.get('config',{}).get('ssh_entry',{})
        node=state['servers'].get(entry.get('server_id'),{})
        route=state['routes'].get(item['id'],{})
        hops=ssh.Controller.hops(route) if route else []
        middle=hops[1] if len(hops)>1 else {}
        second=state['servers'].get(middle.get('server_id'),{})
        records.append({'id':item['id'],'config':item.get('config',{}),
            'host':entry.get('host',''),'port':node.get('ssh_port',22),'listen_port':entry.get('port',443),
            'exit_ip':route.get('exit_ip',''),'middle_host':second.get('host',''),
            'middle_port':second.get('ssh_port',22),'middle_listen_port':middle.get('config',{}).get('listen_port',443)})
    data=json.dumps(records,ensure_ascii=False).replace('<','\\u003c')
    return '''<script>(()=>{
'use strict';
const records=new Map(RECORDS.map(x=>[x.id,x])),form=document.getElementById('connectionForm'),dialog=document.getElementById('connectionEditor');
if(!form||!dialog)return;
const box=form.querySelector('[data-ssh-inline-box]'),middle=box.querySelector('[data-ssh-inline-middle]'),status=box.querySelector('[data-ssh-inline-status]');
let busy=false,active=false,current=null,createId='';
const clearPasswords=()=>{form.elements.ssh_password.value='';form.elements.ssh_middle_password.value=''};
function sync(){
  const p=form.elements.protocol.value,config=current?.config||{},panels=(config.cascade?.mode||'direct')!=='direct';
  active=!panels&&(p==='hysteria'||(p==='vless'&&form.elements.route.value==='direct'));
  box.hidden=!active;
  const host=form.elements.ssh_host.value.trim(),two=active&&!!host&&form.elements.ssh_double_hop.checked;
  box.querySelectorAll('input').forEach(x=>x.disabled=!active||busy);
  form.elements.ssh_double_hop.disabled=!active||busy||!host;
  middle.hidden=!two;middle.querySelectorAll('input').forEach(x=>x.disabled=!two||busy);
  form.elements.ssh_password.required=active&&!!host&&host!==(current?.host||'');
  form.elements.ssh_middle_host.required=two;
  form.elements.ssh_middle_password.required=two&&form.elements.ssh_middle_host.value.trim()!==(current?.middle_host||'');
}
function fill(){
  createId='';
  current=records.get(form.elements.id.value)||null;
  for(const name of ['host','port','listen_port','exit_ip','middle_host','middle_port','middle_listen_port']){
    form.elements['ssh_'+name].value=current?.[name]??(name.endsWith('listen_port')?443:name.endsWith('port')?22:'');
  }
  clearPasswords();form.elements.ssh_double_hop.checked=!!current?.middle_host;
  box.querySelectorAll('details').forEach(x=>x.open=false);status.hidden=true;status.textContent='';sync();
}
new MutationObserver(()=>{if(dialog.open)fill()}).observe(dialog,{attributes:true,attributeFilter:['open']});
for(const name of ['protocol','route','ssh_host','ssh_middle_host','ssh_double_hop'])form.elements[name].addEventListener('input',sync);
form.addEventListener('reset',()=>setTimeout(()=>{if(!busy)fill()},0));
dialog.addEventListener('close',clearPasswords);
dialog.addEventListener('cancel',e=>{if(busy)e.preventDefault();else clearPasswords()});
form.addEventListener('submit',async event=>{
  if(event.defaultPrevented||form.elements.operation.value!=='save'||!form.action.endsWith('/connection-action'))return;
  if(!active)return;
  event.preventDefault();if(busy||!form.reportValidity())return;
  if(!form.elements.id.value&&!createId)createId=Array.from(crypto.getRandomValues(new Uint8Array(8)),x=>x.toString(16).padStart(2,'0')).join('');
  const payload=new URLSearchParams(new FormData(form));payload.set('protocol',form.elements.protocol.value);
  payload.set('id',form.elements.id.value||createId);
  busy=true;form.querySelectorAll('input,select,textarea,button').forEach(x=>x.disabled=true);
  status.hidden=false;status.style.color='';status.textContent=payload.get('ssh_host')?.trim()?'Настраиваем сервер и проверяем интернет. Это может занять несколько минут…':'Сохраняем подключение…';
  try{
    const response=await fetch(form.action,{method:'POST',headers:{Accept:'application/json','X-WPP-Async':'1'},body:payload});
    if(response.redirected)throw new Error('Сессия завершена. Войдите в панель заново.');
    let result;try{result=await response.json()}catch(_){throw new Error('Нет ответа панели. Повторите проверку состояния.')}
    if(!response.ok||!result.ok)throw new Error(result.message||'Не удалось сохранить подключение.');
    status.textContent=result.bridge?.internet?'Подключение настроено. Выход в интернет проверен.':'Подключение сохранено.';
    window.showPanelToast?.(status.textContent,'success');location.reload();
  }catch(error){status.style.color='var(--red)';status.textContent=error.message;status.scrollIntoView({block:'nearest'});window.showPanelToast?.(error.message,'error')}
  finally{
    clearPasswords();busy=false;form.querySelectorAll('input,select,textarea,button').forEach(x=>x.disabled=false);
    form.elements.protocol.disabled=!!current;sync();
    form.elements.protocol.dispatchEvent(new Event('change'));form.elements.route.dispatchEvent(new Event('change'));
  }
});
sync();
})();</script>'''.replace('RECORDS',data)


def render(connections,domain,path,csrf,standalone=False):
    try:state=ssh.load('/etc/web-proxy-panel/ssh-cascades.json');servers=state['servers']
    except ssh.SSHError:state={};servers={}
    rows=[]
    for item in connections:
        if not ssh.supports_connection(item):continue
        try:entry=ssh.client_entry(item.get('config',{}).get('ssh_entry')) if item.get('config',{}).get('ssh_entry') else {}
        except ssh.SSHError:entry={}
        node=servers.get(entry.get('server_id'),{})
        route=state.get('routes',{}).get(item['id'],{});hops=ssh.Controller.hops(route) if route else []
        middle=hops[1] if len(hops)>1 else {};second=servers.get(middle.get('server_id'),{})
        rows.append({'id':item['id'],'name':item['name'],'protocol':item['protocol'],
                     'entry':entry,'ssh_port':node.get('ssh_port',22),'exit_ip':route.get('exit_ip',''),
                     'middle':{'host':second.get('host',''),'ssh_port':second.get('ssh_port',22),'listen_port':middle.get('config',{}).get('listen_port',443)}})
    data=json.dumps({'connections':rows,'domain':domain,'path':path,'csrf':csrf,'standalone':standalone},ensure_ascii=False).replace('<','\\u003c')
    return '''<style>
#sshCascadeDialog{width:min(780px,calc(100vw - 28px));max-height:92vh;padding:0;overflow:auto}
#sshCascadeDialog>.dialog-head{margin:0;padding:20px 22px;border-bottom:1px solid var(--line);background:var(--surface)}
#sshCascadeDialog>.dialog-head p{margin:6px 0 0;font-size:14px}
#sshCascadeDialog form{padding:22px}#sshCascadeDialog .ssh-intro{padding:16px;border:1px solid var(--line);border-radius:12px;background:var(--input);margin-bottom:18px;font-size:15px;line-height:1.65}
#sshCascadeDialog .ssh-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
#sshCascadeDialog label{display:grid;gap:8px;margin:0;font-size:14px}#sshCascadeDialog input{width:100%;font-size:16px;box-sizing:border-box}
#sshCascadeDialog fieldset{border:1px solid var(--line);border-radius:12px;margin:18px 0 0;padding:18px;min-width:0}
#sshCascadeDialog [hidden]{display:none}#sshCascadeDialog details{margin-top:18px}#sshCascadeDialog summary{cursor:pointer;color:var(--muted)}
#sshCascadeDialog details .ssh-grid{margin-top:14px}#sshCascadeDialog .ssh-status{font-size:15px;line-height:1.6;white-space:pre-wrap}
#sshCascadeDialog .ssh-actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}
#sshCascadeDialog .ssh-note{font-size:14px;line-height:1.6;color:var(--muted);margin:14px 0}
@media(max-width:600px){#sshCascadeDialog .ssh-grid{grid-template-columns:1fr}}
</style>
<dialog id="sshCascadeDialog" aria-labelledby="sshCascadeTitle">
<div class="dialog-head"><div><h2 id="sshCascadeTitle">RU-вход по SSH</h2><p data-ssh-connection></p></div><button type="button" data-ssh-close aria-label="Закрыть">×</button></div>
<form autocomplete="off">
<div class="ssh-intro"><b data-ssh-scheme>Клиент → RU-1 → зарубежный VPS → интернет</b><br>Панель остаётся на зарубежном сервере. <span data-ssh-transport></span></div>
<div class="ssh-grid">
<label>IPv4 RU-сервера<input name="host" required placeholder="Публичный IP RU-сервера" autocomplete="off"></label>
<label>Пароль root<input name="password" type="password" required autocomplete="new-password" placeholder="Пароль RU-сервера"></label>
</div>
<details><summary>Дополнительно</summary><div class="ssh-grid">
<label>Порт SSH<input name="ssh_port" type="number" min="1" max="65535" value="22" required></label>
<label>Порт подключения на RU<input name="listen_port" type="number" min="1" max="65535" value="443" required></label>
<label>IPv4 зарубежного VPS<input name="exit_ip" placeholder="Определится автоматически" autocomplete="off"></label>
</div></details>
<label style="display:flex;align-items:center;gap:12px;margin-top:20px"><input type="checkbox" name="double_hop" style="width:auto"> Добавить второй RU-сервер</label>
<fieldset data-ssh-middle hidden><legend>Второй сервер · RU-2</legend>
<div class="ssh-grid">
<label>IPv4 RU-2<input name="middle_host" placeholder="Публичный IP второго RU-сервера" autocomplete="off"></label>
<label>Пароль root RU-2<input name="middle_password" type="password" autocomplete="new-password" placeholder="Пароль второго RU-сервера"></label>
</div><details><summary>Дополнительно для RU-2</summary><div class="ssh-grid">
<label>Порт SSH RU-2<input name="middle_ssh_port" type="number" min="1" max="65535" value="22"></label>
<label>Порт подключения RU-2<input name="middle_listen_port" type="number" min="1" max="65535" value="443"></label>
</div></details></fieldset>
<p class="ssh-note">Введите IP и пароль root, затем нажмите «Настроить RU-вход». Пароль не сохраняется. После переустановки RU-сервера повторите настройку с новым паролем.</p>
<p class="ssh-note" data-ssh-firewall></p>
<p role="status" class="ssh-status" data-ssh-status></p>
<div class="ssh-actions"><button class="primary" type="submit" data-ssh-install>Настроить RU-вход</button><button type="button" data-ssh-check>Проверить интернет</button><button type="button" class="danger" data-ssh-remove>Отключить RU-вход</button></div>
<p class="ssh-note">После изменения входного адреса обновите подписку либо заново добавьте отдельное подключение в приложении.</p>
</form></dialog><script type="application/json" id="sshCascadeData">'''+data+'''</script><script>(()=>{
'use strict';
const data=JSON.parse(document.getElementById('sshCascadeData').textContent),dialog=document.getElementById('sshCascadeDialog'),form=dialog.querySelector('form'),base=document.getElementById('connectionEditor'),original=document.getElementById('connectionForm');
if(!base||!original)return;
const records=new Map(data.connections.map(c=>[c.id,c])),status=form.querySelector('[data-ssh-status]');
let current=null,busy=false,baseline='';
const snapshot=()=>new URLSearchParams(new FormData(original)).toString();
const clearPasswords=()=>{form.elements.password.value='';form.elements.middle_password.value=''};
const switchDialog=async(from,to)=>{await new Promise(resolve=>{if(!from.open)return resolve();from.addEventListener('close',resolve,{once:true});from.close()});if(to.showModal)to.showModal();else to.scrollIntoView({block:'start'})};
function syncMiddle(){
  const enabled=form.elements.double_hop.checked;
  form.querySelector('[data-ssh-middle]').hidden=!enabled;
  for(const key of ['host','ssh_port','listen_port','password']){const input=form.elements['middle_'+key];input.disabled=busy||!enabled;input.required=enabled}
  form.querySelector('[data-ssh-scheme]').textContent=enabled?'Клиент → RU-1 → RU-2 → зарубежный VPS → интернет':'Клиент → RU-1 → зарубежный VPS → интернет';
}
function setBusy(value){
  busy=value;for(const input of dialog.querySelectorAll('button,input'))input.disabled=value;
  syncMiddle();form.querySelector('[data-ssh-check]').disabled=busy||!current?.entry.host;form.querySelector('[data-ssh-remove]').disabled=busy||!current?.entry.host;
}
const back=document.createElement('button');back.type='button';back.className='ssh-method-back';back.textContent='← Выбор способа';form.prepend(back);
back.addEventListener('click',()=>{if(!busy){clearPasswords();window.wppCascadeMethods?.open(dialog,current?.id)}});
new MutationObserver(()=>{if(base.open)queueMicrotask(()=>{baseline=snapshot()})}).observe(base,{attributes:true,attributeFilter:['open']});
async function show(from=base){
  if(busy)return;current=records.get(original.elements.id.value);if(!current)return;
  if(!data.standalone&&snapshot()!==baseline){window.showPanelToast?.('Сначала сохраните основные настройки подключения.','error');return}
  form.reset();clearPasswords();dialog.querySelectorAll('details').forEach(d=>d.open=false);
  const isHysteria=current.protocol==='hysteria';
  form.querySelector('[data-ssh-transport]').textContent=isHysteria?'RU-сервер пересылает Hysteria2 по UDP.':'RU-сервер пересылает VLESS по TCP.';
  form.querySelector('[data-ssh-firewall]').textContent=isHysteria?'Откройте UDP 443 на зарубежном VPS и выбранные UDP-порты на RU.':'Откройте выбранные TCP-порты на RU.';
  form.elements.host.value=current.entry.host||'';form.elements.ssh_port.value=current.ssh_port;form.elements.exit_ip.value=current.exit_ip;form.elements.listen_port.value=current.entry.port||443;
  for(const key of ['host','ssh_port','listen_port'])form.elements['middle_'+key].value=current.middle[key];
  form.elements.double_hop.checked=!!current.middle.host;
  dialog.querySelector('[data-ssh-connection]').textContent=current.name+' · выход: '+data.domain;
  status.textContent=current.entry.host?'RU-вход настроен: '+current.entry.host+':'+current.entry.port:'Укажите IP и пароль root RU-сервера.';
  setBusy(false);await switchDialog(from,dialog);
}
window.wppSSHCascade={open:show,busy:()=>busy};
dialog.querySelector('[data-ssh-close]').addEventListener('click',async()=>{if(!busy){clearPasswords();await switchDialog(dialog,base)}});
dialog.addEventListener('cancel',e=>{if(busy)e.preventDefault();else clearPasswords()});dialog.addEventListener('close',clearPasswords);
form.elements.double_hop.addEventListener('change',syncMiddle);
async function run(operation){
  if(busy||!current)return;if(operation==='provision'&&!form.reportValidity())return;
  if(operation==='remove'&&!confirm('Отключить RU-вход? После этого обновите клиентский профиль для прямого подключения к зарубежному серверу.'))return;
  const values={csrf:data.csrf,operation,id:current.id};
  if(operation==='provision'){
    for(const name of ['host','ssh_port','listen_port','exit_ip','password'])values[name]=form.elements[name].value;
    if(form.elements.double_hop.checked){const middle={};for(const name of ['host','ssh_port','listen_port','password'])middle[name]=form.elements['middle_'+name].value;values.middle=JSON.stringify(middle)}
  }
  setBusy(true);status.style.color='';status.textContent=operation==='check'?'Проверяем интернет через RU и зарубежный сервер…':operation==='remove'?'Отключаем RU-вход…':'Настраиваем RU-сервер и проверяем интернет. Это может занять несколько минут…';
  try{
    const response=await fetch(data.path+'/ssh-cascade-action',{method:'POST',headers:{Accept:'application/json'},body:new URLSearchParams(values)});
    if(response.redirected)throw new Error('Войдите в панель заново.');
    let result;try{result=await response.json()}catch(_){throw new Error('Нет ответа панели. Повторите проверку состояния.')}
    if(!response.ok||!result.ok)throw new Error(result.message||'Не удалось настроить RU-вход.');
    if(operation==='check')status.textContent='Выход в интернет через RU и зарубежный сервер работает.';
    else{window.showPanelToast?.(operation==='remove'?'RU-вход отключён. Обновите клиентский профиль.':'RU-вход настроен. Обновите подписку или отдельное подключение.','success');location.reload()}
  }catch(e){status.style.color='var(--red)';status.textContent=e.message;window.showPanelToast?.(e.message,'error')}
  finally{if(operation==='provision')clearPasswords();setBusy(false)}
}
form.querySelector('[data-ssh-check]').addEventListener('click',()=>run('check'));
form.querySelector('[data-ssh-remove]').addEventListener('click',()=>run('remove'));
form.addEventListener('submit',e=>{e.preventDefault();run('provision')});
})();</script>'''
