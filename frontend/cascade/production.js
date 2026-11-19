(() => {
  'use strict';
  const root = document.querySelector('[data-cascade-config]');
  if (!root) return;
  const data = JSON.parse(root.textContent), dialog = document.querySelector('[data-cascade-editor]');
  const original = document.getElementById('connectionEditor'), originalForm = document.getElementById('connectionForm');
  if (!dialog || !original || !originalForm) return;
  const form = dialog.querySelector('form'), modes = [...form.querySelectorAll('[name=cascadeMode]')];
  const select = Object.fromEntries([...form.querySelectorAll('[data-cascade-node]')].map(el => [el.dataset.cascadeNode, el]));
  const nodes = new Map(data.nodes.map(node => [node.id, node])), records = new Map(data.connections.map(item => [item.id, item]));
  const drafts = new Map();
  const error = form.querySelector('#cascadeError'), status = form.querySelector('.cascade-preview-note');
  const generalTab = form.querySelector('#cascadeGeneralTab'), chainTab = form.querySelector('#cascadeChainTab');
  const fingerprint = () => new URLSearchParams(new FormData(originalForm)).toString();
  const inboundLabel = () => current?.protocol==='hysteria'?'Hysteria2 · UDP 443':'VLESS XHTTP';
  let current = null, baseline = '', busy = false;
  form.noValidate = true;
  const icon = symbol => { const svg = document.createElementNS('http://www.w3.org/2000/svg','svg'); const use = document.createElementNS(svg.namespaceURI,'use'); use.setAttribute('href','#cascade-'+symbol); svg.append(use); return svg; };
  const mode = () => modes.find(input => input.checked).value;
  const roles = () => mode() === 'three' ? ['entry','middle','exit'] : mode() === 'two' ? ['entry','exit'] : ['entry'];
  const plan = () => ({mode:mode(),nodes:roles().filter(role => role !== 'entry').map(role => select[role].value)});
  Object.entries(select).forEach(([role, control]) => {
    if (role !== 'entry') { const empty = document.createElement('option'); empty.value=''; empty.textContent='Выберите ноду'; control.append(empty); }
    data.nodes.filter(node => (role === 'entry') === (node.id === 'local')).forEach(node => {
      const option=document.createElement('option'); option.value=node.id; option.textContent=node.country+' · '+node.host; control.append(option);
    });
    control.addEventListener('change', draw);
  });
  select.entry.value='local'; select.entry.disabled=true;
  select.entry.title='Входной сервер — панель, на которой создано подключение';
  function validate() {
    const ids=plan().nodes;
    const message=ids.some(id => !nodes.has(id)) ? 'Выберите доступные ноды для всех участков. Добавить сервер можно в разделе «Ноды».' : new Set(ids).size !== ids.length ? 'Каждый сервер можно использовать в цепочке только один раз.' : '';
    error.textContent=message; error.hidden=!message;
    form.querySelector('button[type=submit]').disabled=busy || Boolean(message);
    form.querySelector('[data-cascade-validate]').disabled=busy || Boolean(message) || mode()==='direct';
    return !message;
  }
  function appendFlow(flow, node, title, subtitle, symbol, last, index) {
    const li=document.createElement('li'), mark=document.createElement('span'); mark.className='cascade-flow-node'+(last?' exit':node?'':' endpoint');
    if (node) { const flag=document.createElement('img'); flag.className='cascade-flag'; flag.src=node.flag; flag.alt=node.country; mark.append(flag); } else mark.append(icon(symbol));
    const copy=document.createElement('div'); copy.className='cascade-flow-copy'; const b=document.createElement('b'), small=document.createElement('small'); b.textContent=title; small.textContent=subtitle; copy.append(b,small); li.append(mark,copy);
    if (index) { const number=document.createElement('span'); number.className='cascade-flow-number'+(last?' exit':''); number.textContent=String(index).padStart(2,'0'); li.append(number); }
    flow.append(li);
  }
  function draw() {
    const active=roles(), middle=form.querySelector('[data-cascade-middle]'), exit=form.querySelector('[data-cascade-exit]');
    middle.hidden=!active.includes('middle'); exit.hidden=!active.includes('exit');
    select.middle.disabled=busy || middle.hidden; select.exit.disabled=busy || exit.hidden;
    form.querySelector('[data-cascade-exit-number]').textContent=String(active.length);
    form.querySelector('[data-cascade-count]').textContent=active.length===1?'1 сервер':active.length+' сервера';
    form.querySelector('.cascade-transport').hidden=active.length===1;
    form.querySelector('.cascade-fail-closed').hidden=active.length===1;
    const flow=form.querySelector('[data-cascade-flow]'); flow.replaceChildren(); flow.style.setProperty('--cascade-mobile-columns',`repeat(${active.length+2},minmax(0,1fr))`);
    appendFlow(flow,null,'Ваше устройство',inboundLabel(),'device');
    active.forEach((role,index) => { const node=nodes.get(select[role].value), last=index===active.length-1; appendFlow(flow,node,node?.country||'Выберите сервер',(node?.host||'Нода не выбрана')+' · '+(last?'Выход':index?'Транзит':'Вход'),'server',last,index+1); });
    appendFlow(flow,null,'Интернет','Сайты и приложения','globe');
    const last=nodes.get(select[active.at(-1)].value);
    form.querySelector('[data-cascade-country]').textContent=last?.country||'Выберите сервер';
    form.querySelector('[data-cascade-city]').textContent=last?.host||'';
    status.textContent='Настройки только для выбранного подключения';
    validate();
  }
  function message(text, failed=false) { status.textContent=text; status.style.color=failed?'var(--red)':'var(--muted)'; }
  async function switchDialog(from,to) {
    await new Promise(resolve=>{if(!from.open)return resolve();from.addEventListener('close',resolve,{once:true});from.close();});
    if(to.showModal)to.showModal();else to.scrollIntoView({block:"start"});
  }
  async function show(from=original) {
    current=records.get(originalForm.elements.id.value);
    if (!current) return;
    if (!data.standalone && fingerprint()!==baseline) { window.showPanelToast?.('Сначала сохраните изменения основных настроек подключения.','error'); return; }
    form.querySelector('#cascadeTitle').textContent=current.name;
    const chosen=drafts.get(current.id)||current.cascade||{mode:'direct',nodes:[]}; modes.forEach(input=>{input.checked=input.value===chosen.mode});
    select.middle.value=chosen.mode==='three'?chosen.nodes[0]:data.nodes.find(node=>node.id!=='local')?.id||'';
    select.exit.value=chosen.nodes.at(-1)||data.nodes.filter(node=>node.id!=='local').at(-1)?.id||'';
    draw(); await switchDialog(from,dialog);
  }
  function back() { if (busy) return; window.wppCascadeMethods?.open(dialog,current?.id); }
  dialog.addEventListener('close',()=>{if(current&&!busy)drafts.set(current.id,plan());});
  generalTab.addEventListener('click',back);
  generalTab.addEventListener('keydown',event=>{if(event.key==='ArrowRight'){event.preventDefault();chainTab.focus();}});
  chainTab.addEventListener('keydown',event=>{if(event.key==='ArrowLeft'){event.preventDefault();generalTab.focus();}});
  dialog.addEventListener('cancel',event=>{if(busy)event.preventDefault();});
  form.querySelector('[data-cascade-close]').addEventListener('click',()=>{if(!busy)switchDialog(dialog,original);});
  modes.forEach(input=>input.addEventListener('change',draw));
  generalTab.textContent='Выбор способа';
  generalTab.setAttribute('aria-controls','cascadeMethodDialog');
  generalTab.setAttribute('role','button');generalTab.setAttribute('aria-haspopup','dialog');generalTab.removeAttribute('aria-selected');generalTab.tabIndex=0;
  generalTab.parentElement.setAttribute('role','group');
  chainTab.setAttribute('role','button');chainTab.removeAttribute('aria-selected');chainTab.setAttribute('aria-current','true');
  window.wppPanelCascade={open:show,busy:()=>busy};
  new MutationObserver(()=>{if(original.open)queueMicrotask(()=>{baseline=fingerprint();});}).observe(original,{attributes:true,attributeFilter:['open']});
  async function run(operation) {
    if (busy || !validate() || !current) return;
    busy=true; form.querySelectorAll('button,input,select').forEach(el=>{el.disabled=true;}); message(operation==='save'?'Настраиваем и проверяем цепочку…':'Проверяем выход в интернет через цепочку…');
    try {
      const body=new URLSearchParams({csrf:data.csrf,operation,id:current.id,name:current.name,cascade:JSON.stringify(plan())});
      const response=await fetch(data.path+'/cascade-action',{method:'POST',headers:{'Accept':'application/json'},body});
      if(response.redirected)throw new Error('Сессия завершена. Войдите в панель заново.');
      let result;
      try { result=await response.json(); } catch (_) { throw new Error('Не удалось получить ответ панели. Проверьте соединение и повторите попытку.'); }
      if(!response.ok||!result.ok)throw new Error(result.message||'Не удалось настроить цепочку.');
      if(operation==='save'){window.showPanelToast?.(result.cleanup_pending?'Цепочка сохранена. Старые участки будут очищены при следующей операции с мостом.':'Цепочка применена.','success'); location.reload();}
      else message(result.cleanup_pending?'Выход в интернет работает. Очистка тестовых участков ожидает повтора.':'Проверено: выход в интернет через всю цепочку работает.');
    } catch(error) { message(error.message||'Не удалось проверить цепочку.',true); window.showPanelToast?.(error.message||'Ошибка моста.','error'); }
    finally { busy=false; form.querySelectorAll('button,input,select').forEach(el=>{el.disabled=false;}); select.entry.disabled=true; select.middle.disabled=!roles().includes('middle'); select.exit.disabled=!roles().includes('exit'); validate(); }
  }
  form.addEventListener('submit',event=>{event.preventDefault();run('save');});
  form.querySelector('[data-cascade-validate]').addEventListener('click',()=>run('check'));
})();
