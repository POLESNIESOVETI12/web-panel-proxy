(() => {
  'use strict';
  const source=document.querySelector('[data-cascade-config]'),dialog=document.getElementById('cascadeMethodDialog');
  const base=document.getElementById('connectionEditor'),original=document.getElementById('connectionForm');
  if(!source||!dialog||!base||!original)return;
  const data=JSON.parse(source.textContent),records=new Map(data.connections.map(row=>[row.id,row]));
  const form=dialog.querySelector('form'),radios=[...form.querySelectorAll('[name=cascadeMethod]')],drafts=new Map();
  const warning=form.querySelector('[data-method-warning]'),next=form.querySelector('[data-method-continue]'),currentButton=form.querySelector('[data-method-current]');
  let current=null,baseline='',moving=false;
  const snapshot=()=>new URLSearchParams(new FormData(original)).toString();
  const active=()=>current?.ssh_entry?'ssh':current?.cascade?.mode!=='direct'&&current?.cascade?.mode?'panel':'direct';
  const chosen=()=>radios.find(input=>input.checked)?.value||'panel';
  async function switchDialog(from,to){
    if(moving)return;moving=true;
    try{await new Promise(resolve=>{if(!from.open)return resolve();from.addEventListener('close',resolve,{once:true});from.close();});if(to.showModal)to.showModal();else to.scrollIntoView({block:"start"});}
    finally{moving=false;}
  }
  function draw(){
    const method=chosen(),running=active(),conflict=running!=='direct'&&method!==running;
    form.querySelector('[data-method-current-label]').textContent='Текущий способ: '+({direct:'прямое подключение',panel:'мост через панели',ssh:'RU-вход по SSH'}[running]);
    form.querySelector('[data-method-flow]').textContent=method==='ssh'?'Клиент → RU-сервер → этот зарубежный VPS → интернет':'Клиент → эта панель на входе → ноды с панелью → интернет';
    form.querySelector('[data-method-explanation]').textContent=method==='ssh'?'На RU достаточно IP и SSH-доступа. Выход в интернет выполняет зарубежный сервер с этой панелью.':'Откройте эту настройку на входном сервере. Добавьте выходную и, при необходимости, промежуточную ноду в разделе «Ноды».';
    const unavailable=method==='ssh'&&current?.protocol==='vless'&&current.route!=='direct';
    warning.hidden=!conflict&&!unavailable;
    warning.textContent=unavailable?'SSH-вход доступен для прямого VLESS-подключения.':conflict?'Сначала отключите текущий мост в его настройках, затем выберите другой способ.':'';
    currentButton.hidden=!conflict;next.disabled=conflict||unavailable;
    drafts.set(current.id,method);
  }
  async function open(from=base,id=original.elements.id.value){
    if(moving)return;current=records.get(id);
    if(!current)return;
    if(!data.standalone&&from===base&&snapshot()!==baseline){window.showPanelToast?.('Сначала сохраните основные настройки подключения.','error');return;}
    dialog.querySelector('[data-method-connection]').textContent=current.name;
    const method=drafts.get(id)||(active()==='direct'?'panel':active());
    radios.forEach(input=>{input.checked=input.value===method;});draw();await switchDialog(from,dialog);
  }
  async function enter(method){
    if(moving)return;
    const controller=method==='ssh'?window.wppSSHCascade:window.wppPanelCascade;
    if(!controller)return;
    moving=true;
    try{await controller.open(dialog);}finally{moving=false;}
  }
  window.wppCascadeMethods={open};
  if(!data.standalone){
  const launch=document.createElement('div');launch.className='cascade-tabs cascade-launch-tabs';launch.setAttribute('role','group');launch.setAttribute('aria-label','Разделы настроек подключения');
  const main=document.createElement('button');main.type='button';main.className='selected';main.textContent='Основное';
  const button=document.createElement('button');button.type='button';button.textContent='Мост';button.addEventListener('click',()=>open(base));
  launch.append(main,button);base.querySelector('.dialog-head').insertAdjacentElement('afterend',launch);
  new MutationObserver(()=>{if(base.open){launch.hidden=!records.has(original.elements.id.value);queueMicrotask(()=>{baseline=snapshot();});}}).observe(base,{attributes:true,attributeFilter:['open']});
  }else{
    const launch=id=>{const record=records.get(id);if(!record)return;original.elements.id.value=id;original.elements.protocol.value=record.protocol;open(base,id)};
    document.querySelectorAll('[data-open-cascade]').forEach(button=>button.addEventListener('click',()=>launch(button.dataset.openCascade)));
    const selected=new URL(location.href).searchParams.get('connection');if(selected)queueMicrotask(()=>launch(selected));
  }
  radios.forEach(input=>input.addEventListener('change',draw));
  form.addEventListener('submit',event=>{event.preventDefault();if(!next.disabled)enter(chosen());});
  currentButton.addEventListener('click',()=>enter(active()));
  form.querySelector('[data-method-back]').addEventListener('click',()=>switchDialog(dialog,base));
  dialog.querySelector('[data-method-close]').addEventListener('click',()=>switchDialog(dialog,base));
})();
