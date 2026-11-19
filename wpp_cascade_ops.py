"""Cascade health, diagnostics, constrained failover and opt-in Telegram alerts."""
import copy
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import socket
import ssl
import time
import uuid
from urllib.parse import urlsplit
import urllib.request

import wpp_cascade as cascade
import wpp_connections as connections
import wpp_nodes as nodes_api
import wpp_ssh_cascade as ssh

SETTINGS='/etc/web-proxy-panel/cascade-ops.json'
CACHE='/var/lib/tproxy-panel/cascade-monitor.json'


class OperationsError(ValueError):pass


def read_json(path,default):
    try:value=json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:return copy.deepcopy(default)
    except (OSError,ValueError):raise OperationsError('Не удалось прочитать настройки мониторинга.') from None
    if not isinstance(value,dict):raise OperationsError('Настройки мониторинга повреждены.')
    return value


def defaults():
    return {'schema':1,'monitor_enabled':True,'failures':3,'cooldown':300,
            'notifications':{'enabled':False,'token':'','chat_id':'','cert_days':14},'profiles':{}}


def settings(path=SETTINGS):
    value=read_json(path,defaults())
    if value.get('schema')!=1 or not isinstance(value.get('profiles'),dict) or not isinstance(value.get('notifications'),dict):
        raise OperationsError('Некорректный формат настроек мониторинга.')
    for key in ('watched','excluded'):
        value.setdefault(key,[])
        if not isinstance(value[key],list) or any(not isinstance(cid,str) for cid in value[key]):
            raise OperationsError('Некорректный список мониторинга.')
    return value


def cached(path=CACHE):
    value=read_json(path,{'schema':1,'reports':{},'counters':{},'events':[],'queue':[],'cursor':0})
    if value.get('schema')!=1 or not isinstance(value.get('reports'),dict) or not isinstance(value.get('counters'),dict):
        raise OperationsError('Кэш мониторинга повреждён.')
    return value


def clean_int(value,low,high,label):
    if isinstance(value,bool):raise OperationsError(label+': неверное значение.')
    try:result=int(value)
    except (ValueError,TypeError):raise OperationsError(label+': неверное значение.') from None
    if not low<=result<=high:raise OperationsError(label+': значение вне допустимого диапазона.')
    return result


def clean_notifications(raw,previous=None):
    previous=previous or {};result={}
    result['enabled']=raw.get('enabled') is True
    token=str(raw.get('token') or previous.get('token') or '').strip()
    if raw.get('clear_token') is True:token=''
    chat=str(raw.get('chat_id',previous.get('chat_id',''))).strip()
    if token and not re.fullmatch(r'[0-9]{5,20}:[A-Za-z0-9_-]{20,128}',token):raise OperationsError('Некорректный токен Telegram-бота.')
    if chat and not re.fullmatch(r'-?[0-9]{1,22}|@[A-Za-z][A-Za-z0-9_]{0,63}',chat):raise OperationsError('Укажите Chat ID или @имя канала Telegram.')
    if result['enabled'] and (not token or not chat):raise OperationsError('Для уведомлений укажите токен бота и Chat ID.')
    result.update(token=token,chat_id=chat,cert_days=clean_int(raw.get('cert_days',14),1,60,'Срок сертификата'))
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def send_telegram(config,text):
    config=clean_notifications(config)
    if not config.get('token') or not config.get('chat_id'):raise OperationsError('Сначала настройте Telegram-бота и Chat ID.')
    request=urllib.request.Request('https://api.telegram.org/bot'+config['token']+'/sendMessage',
        data=json.dumps({'chat_id':config['chat_id'],'text':str(text)[:3500]},ensure_ascii=False).encode(),
        headers={'Content-Type':'application/json'},method='POST')
    try:
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        with opener.open(request,timeout=8) as response:
            value=json.loads(response.read(65537))
        if not isinstance(value,dict) or value.get('ok') is not True:raise ValueError('Rejected')
    except Exception:
        # HTTPError includes the bot token in its URL. Never pass it to logs or UI.
        raise OperationsError('Telegram не принял сообщение. Проверьте токен, Chat ID и доступ сервера к api.telegram.org.') from None
    return {'ok':True}


def transport_status(host,port=443,connect_to=None):
    result={'host':host,'port':port,'dns':False,'tcp':False,'tls':False,'latency_ms':None,'cert_days':None}
    try:
        if connect_to:addresses=[connect_to];result['dns']=True
        else:
            addresses=list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host,port,type=socket.SOCK_STREAM)))
            if not addresses or any(not ipaddress.ip_address(ip).is_global or ipaddress.ip_address(ip).is_multicast for ip in addresses):
                result['problem']='DNS не указывает на публичный сервер.';return result
            result['dns']=True
    except OSError:result['problem']='Не удалось определить адрес сервера (DNS).';return result
    for address in sorted(addresses,key=lambda ip:':' in ip)[:2]:
        started=time.monotonic()
        try:
            with socket.create_connection((address,port),timeout=3) as raw:
                result.update(tcp=True,latency_ms=round((time.monotonic()-started)*1000,1))
                with ssl.create_default_context().wrap_socket(raw,server_hostname=host) as secure:
                    certificate=secure.getpeercert()
                    until=ssl.cert_time_to_seconds(certificate['notAfter'])
                    result.update(tls=True,cert_days=int((until-time.time())/86400))
            return result
        except ssl.SSLError:result['problem']='TLS-сертификат не прошёл проверку.'
        except OSError:result['problem']='TCP-порт '+str(port)+' недоступен.'
    return result


def route_kind(connection):
    config=connection.get('config',{})
    if config.get('ssh_entry'):return 'ssh'
    return 'panel' if config.get('cascade',{}).get('mode','direct')!='direct' else 'direct'


def monitored(row,config,cache):
    return (row.get('protocol') in cascade.ENTRY_PROTOCOLS and row['id'] not in config['excluded'] and
            (route_kind(row)!='direct' or row['id'] in config['watched'] or row['id'] in cache['reports']))


def server_key(node):
    host=str(node.get('host',''))
    return hashlib.sha256((host+':'+str(node.get('port',443))).encode()).hexdigest()[:24] if host else ''


def server_problem(node):
    if node.get('enabled') is False:return 'отключён в панели'
    for key,label in (('dns','не определяется адрес DNS'),('tcp','порт недоступен'),('tls','ошибка TLS'),
                      ('service_ok','служба остановлена'),('management_ok','нет связи с панелью управления'),
                      ('next_ok','недоступен следующий участок маршрута')):
        if node.get(key) is False:return node.get('problem') or label
    return ''


def stable_probe(state_path,connection,domain,description,apply_runtime,binary,host,port):
    """Reuse a private health identity; routine checks must not reload Xray."""
    protocol=connection['protocol']
    is_hysteria=protocol=='hysteria'
    prefix='health-hysteria:' if is_hysteria else 'health:'
    chain=hashlib.sha256((prefix+domain+':'+connection['id']).encode()).hexdigest()[:32]
    revision='0000000000000001';rid=chain+'-'+revision+'-1'
    state=cascade.load(state_path);record=state['relays'].get(rid)
    if not record:
        request={'chain_id':chain,'revision':revision,'hosts':['health.invalid',domain],
                 'position':1,'uuid':str(uuid.uuid4()),'next':None}
        cascade.mutate_relay(state_path,request,domain,apply_runtime,
                            local_endpoint=description.get('endpoint'),local_listener_hash=description.get('listener_hash'),
                            probe_connection=connection['id'],probe_protocol=protocol)
        state=cascade.load(state_path);record=state['relays'][rid]
    if (not record.get('probe_only') or record.get('probe_connection')!=connection['id'] or
            record.get('probe_protocol','vless')!=protocol):
        record.update(probe_only=True,probe_connection=connection['id'],probe_protocol=protocol)
        cascade.save(state_path,state)
        apply_runtime()
    if is_hysteria:
        outbound=cascade.hysteria_outbound('probe-current-route',host,port,record['uuid'],server_name=domain)
        return cascade.probe(binary,None,client_outbound=outbound,details=True)
    target={**description['endpoint'],'uuid':record['uuid']}
    outbound=cascade.outbound('probe-current-route',target)
    outbound['settings']['vnext'][0].update(address=host,port=port)
    return cascade.probe(binary,target,client_outbound=outbound,details=True)


def tentative_hysteria_probe(state_path, connection, domain, apply_runtime, binary, target):
    """The controller journals and removes this one-use key after the test."""
    key=str(uuid.uuid4())
    request={'chain_id':uuid.uuid4().hex,'revision':'0000000000000001',
             'hosts':['health.invalid',domain],'position':1,'uuid':key,'next':None}
    cascade.mutate_relay(state_path,request,domain,apply_runtime,probe_connection=connection['id'],
                         probe_protocol='hysteria',probe_target=target)
    outbound=cascade.hysteria_outbound('probe-candidate-route',domain,443,key)
    return cascade.probe(binary,None,client_outbound=outbound,details=True)


class Inspector:
    def __init__(self,registry_path,api,ssh_controller,probe,local_status=lambda:{},transport=transport_status,clock=time.time):
        self.registry_path=registry_path;self.api=api;self.ssh=ssh_controller;self.probe=probe
        self.local_status=local_status;self.transport=transport;self.clock=clock

    def inspect(self,cid):
        registry=connections.load(self.registry_path);connection=connections.get(registry,cid)
        if not connection or connection.get('protocol') not in cascade.ENTRY_PROTOCOLS:raise OperationsError('Выберите VLESS или Hysteria2.')
        kind=route_kind(connection);nodes=[];started=self.clock()
        if not connection.get('enabled',True):
            return {'id':cid,'name':connection['name'],'kind':kind,'enabled':False,'healthy':False,'nodes':[],
                    'problem':'Подключение отключено в панели.','checked_at':int(started),'duration_ms':0}
        local=self.local_status()
        if kind=='ssh':
            state=ssh.load(self.ssh.path);route=state['routes'].get(cid)
            if not route:raise OperationsError('SSH-маршрут не сохранён. Повторите настройку RU-входа.')
            rid=hashlib.sha256(cid.encode()).hexdigest()[:24]
            for index,hop in enumerate(self.ssh.hops(route)):
                node=state['servers'][hop['server_id']]
                report={'name':'RU-'+str(index+1),'host':node['host'],'port':hop['config']['listen_port'],'role':'Вход' if index==0 else 'Транзит'}
                if connection['protocol']=='hysteria':
                    report['transport']='UDP'
                mark=time.monotonic()
                try:
                    health=self.ssh.health(node,rid)
                    report.update(management_ok=True,latency_ms=round((time.monotonic()-mark)*1000,1),
                                  service_ok=health.get('active'),next_ok=health.get('next_reachable'),next_ms=health.get('next_ms'),legacy_agent=health.get('legacy_agent',False))
                    if report['service_ok'] is False:report['problem']='Служба RU-пересылки остановлена.'
                    elif report['next_ok'] is False:report['problem']='Следующий участок цепочки недоступен.'
                except Exception:report.update(management_ok=False,service_ok=None,problem='SSH-управление недоступно. Проверьте порт и SSH-ключ.')
                nodes.append(report)
            nodes.append({'name':'Зарубежный выход','host':self.api.host,'role':'Выход',**local})
        else:
            # Test the public entry as well as its local services: an outbound
            # server alone does not prove the user's entry is reachable.
            public=self.transport(self.api.host)
            entry={'name':'Эта панель','host':self.api.host,'role':'Вход' if kind=='panel' else 'Выход',**local,**public}
            if connection['protocol']=='hysteria':
                # HTTPS is the management plane. The end-to-end probe tests the UDP entrance.
                entry.pop('tcp',None)
                entry.pop('tls',None)
                entry.pop('problem',None)
                entry.pop('latency_ms',None)
                entry['transport']='UDP'
            nodes.append(entry)
            stored={node['id']:node for node in nodes_api.load_nodes(self.api.nodes_path)}
            for index,nid in enumerate(connection.get('config',{}).get('cascade',{}).get('nodes',[])):
                node=stored.get(nid)
                if not node:nodes.append({'name':'Удалённая нода','role':'Участок','management_ok':False,'service_ok':False,'problem':'Нода удалена из панели.'});continue
                host=urlsplit(node['url']).hostname
                if not node.get('enabled',True):
                    nodes.append({'name':node.get('name') or host,'host':host,'port':443,'enabled':False,
                                  'problem':'Сервер отключён в панели.'});continue
                report={'name':node.get('country_name') or node.get('name') or host,'role':'Выход' if index==len(connection['config']['cascade']['nodes'])-1 else 'Транзит',**self.transport(host)}
                if report.get('tls'):
                    try:
                        value=self.api.request(node,'GET',nodes_api.API_PREFIX+'/cascade/health',timeout=6)
                        report.update(management_ok=True,service_ok=value.get('service_ok'))
                    except Exception:
                        report.update(management_ok=False,service_ok=None)
                        report['management_problem']='API недоступен или на ноде установлен прежний агент.'
                nodes.append(report)
        try:
            result=self.probe(connection)
            healthy=isinstance(result,dict) and result.get('ok') is True
        except Exception:result={};healthy=False
        if connection['protocol']=='hysteria':
            nodes[0]['route_ok']=healthy
        # The entry service matters even when an outbound-only probe succeeds.
        service_checks=('service_ok',) if connection['protocol']=='hysteria' else ('service_ok','tcp','tls')
        if any(local.get(key) is False for key in service_checks):healthy=False
        if kind!='ssh' and any(nodes[0].get(key) is False for key in ('dns','tcp','tls')):healthy=False
        if any(node.get('enabled') is False for node in nodes):healthy=False
        failed=next((node for node in nodes if node.get('enabled') is False or node.get('service_ok') is False or node.get('tcp') is False or node.get('tls') is False or node.get('next_ok') is False),None)
        if not healthy and failed is None:failed=next((node for node in nodes if node.get('management_ok') is False),None)
        problem='' if healthy else ((failed.get('name','Сервер')+': '+failed.get('problem','проверка служб не прошла')) if failed else
            'Hysteria2 не прошёл проверку маршрута. Проверьте UDP 443 на входе и доступ в интернет с последнего VPS.' if connection['protocol']=='hysteria' else
            'Нет выхода в интернет через всю цепочку. Проверьте DNS и исходящие соединения выходного VPS.')
        public={key:result.get(key) for key in ('exit_ip','country_code','exit_checked_at','exit_error','route_ms') if key in result}
        return {'id':cid,'name':connection['name'],'kind':kind,'enabled':True,'healthy':healthy,'nodes':nodes,'problem':problem,
                'checked_at':int(started),'duration_ms':round((self.clock()-started)*1000,1),**public}


class Operations:
    def __init__(self,inspector,settings_path=SETTINGS,cache_path=CACHE,notify=send_telegram,clock=time.time):
        self.inspector=inspector;self.path=settings_path;self.cache_path=cache_path;self.notify=notify;self.clock=clock

    def specification(self,cid):
        connection=connections.get(connections.load(self.inspector.registry_path),cid)
        if not connection or connection.get('protocol') not in cascade.ENTRY_PROTOCOLS:raise OperationsError('Подключение не найдено.')
        kind=route_kind(connection)
        if kind=='ssh':return self.inspector.ssh.specification(cid)
        if kind=='panel':return {'kind':'panel','plan':cascade.clean_plan(connection['config'].get('cascade'))}
        raise OperationsError('Сначала настройте мост у этого подключения.')

    def reserve(self,cid,raw):
        config=settings(self.path);current=self.specification(cid);candidate=copy.deepcopy(raw.get('reserve'))
        if not isinstance(candidate,dict) or candidate.get('kind')!=current['kind']:raise OperationsError('Резерв должен использовать тот же способ моста.')
        if current['kind']=='panel':
            plan=cascade.clean_plan(candidate.get('plan'))
            if plan['mode']=='direct':raise OperationsError('Резервом должна быть цепочка с выходной нодой.')
            known={node['id']:node for node in nodes_api.load_nodes(self.inspector.api.nodes_path)}
            if any(nid not in known or not known[nid].get('enabled',True) for nid in plan['nodes']):raise OperationsError('Выберите доступные ноды резервной цепочки.')
            if known[plan['nodes'][-1]].get('country_code')=='RU':raise OperationsError('Выберите зарубежную выходную ноду для резерва.')
            candidate={'kind':'panel','plan':plan}
        else:
            candidate={'kind':'ssh','entry':current['entry'],'exit_ip':current['exit_ip'],'middle':candidate.get('middle')}
            if candidate['middle']:
                middle=candidate['middle']
                state=ssh.load(self.inspector.ssh.path)
                if not isinstance(middle,dict) or middle.get('server_id') not in state['servers'] or middle['server_id']==current['entry']['server_id']:
                    raise OperationsError('Выберите другой подготовленный RU-сервер.')
                candidate['middle']={'server_id':middle['server_id'],'listen_port':ssh.port(middle.get('listen_port'),443)}
        if candidate==current:raise OperationsError('Основная и резервная цепочки должны различаться.')
        config['profiles'][cid]={'primary':current,'reserve':candidate,'automatic':raw.get('automatic') is True,'active':'primary','switched_at':0}
        cascade.save(self.path,config)
        return {'ok':True}

    def switch(self,cid,spec):
        def verify(report):
            if not isinstance(report,dict) or not report.get('country_code') or report.get('country_code')=='RU':
                raise OperationsError('Не подтверждён зарубежный выход резервной цепочки.')
            return report
        if spec.get('kind')=='panel':
            # Controller tests the prepared chain before its atomic runtime commit.
            original=self.inspector.api.probe
            def verified(target):
                return verify(original(target))
            self.inspector.api.probe=verified
            try:return self.inspector.api.execute({'id':cid,'cascade':spec['plan']})
            finally:self.inspector.api.probe=original
        if spec.get('kind')=='ssh':
            original=self.inspector.ssh.probe
            self.inspector.ssh.probe=lambda *args:verify(original(*args))
            try:return self.inspector.ssh.retarget(cid,spec)
            finally:self.inspector.ssh.probe=original
        raise OperationsError('Недопустимый резервный маршрут.')

    def event(self,cache,cid,kind,text):
        now=int(self.clock());event={'id':hashlib.sha256((cid+':'+kind+':'+str(now)+':'+str(text)).encode()).hexdigest()[:20],
                                  'connection_id':cid,'kind':kind,'text':str(text)[:1200],'at':now}
        cache.setdefault('events',[]).append(event);cache['events']=cache['events'][-100:]
        cache.setdefault('queue',[]).append({**event,'attempts':0,'next_attempt':now});cache['queue']=cache['queue'][-50:]

    def deliver(self,config,cache):
        notifications=config['notifications']
        if not notifications.get('enabled'):cache['queue']=[];return
        pending=[];sent=0;now=self.clock()
        for event in cache.get('queue',[]):
            if sent>=2 or event.get('next_attempt',0)>now:pending.append(event);continue
            sent+=1
            try:self.notify(notifications,event['text']);cache['telegram_error']=''
            except Exception:
                event['attempts']=event.get('attempts',0)+1
                event['next_attempt']=now+min(3600,60*2**min(event['attempts'],6))
                pending.append(event);cache['telegram_error']='Уведомление Telegram ожидает повтора: проверьте доступ к боту.'
        cache['queue']=pending

    def server_events(self,cache,report,threshold):
        """One transition per server/port, even when multiple profiles use it."""
        observed=cache.setdefault('servers',{})
        for node in report['nodes']:
            key=server_key(node)
            if not key:continue
            state=observed.setdefault(key,{'failures':0,'successes':0,'announced':'unknown'})
            reason=server_problem(node);disabled=node.get('enabled') is False
            identity='«'+str(node.get('name') or node['host'])+'» ('+str(node['host'])+':'+str(node.get('port',443))+')'
            if reason:
                state['successes']=0;state['failures']+=1;status='disabled' if disabled else 'down'
                if (disabled or state['failures']>=threshold) and state['announced']!=status:
                    self.event(cache,report['id'],'server_disabled' if disabled else 'server_down',
                               'WPP: сервер '+identity+' — '+reason+'. Подключение: «'+report['name']+'».')
                    state['announced']=status
            else:
                # Old SSH agents may not report service health. A successful
                # management/transport check is sufficient for availability.
                known=any(node.get(k) is True for k in ('tcp','tls','service_ok','management_ok'))
                if not known:continue
                state['failures']=0;state['successes']+=1
                if state['announced'] in ('down','disabled') and state['successes']>=2:
                    self.event(cache,report['id'],'server_restored','WPP: сервер '+identity+' снова доступен. Подключение: «'+report['name']+'».')
                    state['announced']='up'
                elif state['announced']=='unknown':state['announced']='up'

    def forget(self,cid):
        config=settings(self.path);cache=cached(self.cache_path)
        row=connections.get(connections.load(self.inspector.registry_path),cid)
        if not row and cid not in cache['reports']:raise OperationsError('Подключение не найдено в мониторинге.')
        if cid not in config['excluded']:config['excluded'].append(cid)
        config['watched']=[key for key in config['watched'] if key!=cid]
        cache['reports'].pop(cid,None);cache['counters'].pop(cid,None)
        cache['queue']=[event for event in cache.get('queue',[]) if event.get('connection_id')!=cid]
        remaining={server_key(node) for report in cache['reports'].values() for node in report.get('nodes',[])}
        cache['servers']={key:state for key,state in cache.get('servers',{}).items() if key in remaining}
        cascade.save(self.path,config);cascade.save(self.cache_path,cache)
        return {'ok':True,'removed':cid}

    def check(self,cid,automatic=False):
        config=settings(self.path);cache=cached(self.cache_path);report=self.inspector.inspect(cid)
        if not automatic:
            config['excluded']=[key for key in config['excluded'] if key!=cid]
            if cid not in config['watched']:config['watched'].append(cid)
        counter=cache['counters'].setdefault(cid,{'failures':0,'successes':0,'announced':'unknown'})
        disabled=report.get('enabled') is False
        server_fault=any(server_problem(node) for node in report['nodes'])
        if disabled:
            if counter['announced']!='disabled':self.event(cache,cid,'connection_disabled','WPP: подключение «'+report['name']+'» отключено в панели.')
            counter.update(announced='disabled',failures=0,successes=0,generic_alert=True)
        elif report['healthy']:
            counter['failures']=0;counter['successes']+=1
            if counter['announced'] in ('down','disabled') and counter['successes']>=2:
                if counter.get('generic_alert',True):self.event(cache,cid,'restored','WPP: подключение «'+report['name']+'» снова работает.')
                counter['announced']='up'
            elif counter['announced']=='unknown':counter['announced']='up'
        else:
            counter['failures']+=1;counter['successes']=0
            if counter['failures']>=config['failures'] and counter['announced']!='down':
                if not server_fault:self.event(cache,cid,'down','WPP: подключение «'+report['name']+'» недоступно. '+report['problem'])
                counter.update(announced='down',generic_alert=not server_fault)
        if not disabled:self.server_events(cache,report,config['failures'])
        profile=config['profiles'].get(cid)
        if profile:
            try:
                actual=self.specification(cid);matches=actual in (profile['primary'],profile['reserve'])
                if matches:
                    # Reconcile a crash after runtime commit but before settings save.
                    profile['active']='primary' if actual==profile['primary'] else 'reserve'
            except Exception:matches=False
            if not matches:
                profile['automatic']=False;report['reserve_warning']='Цепочка изменена вручную. Сохраните резерв заново.'
            elif not disabled and automatic and profile.get('automatic') and not report['healthy'] and profile['active']=='primary' and counter['failures']>=config['failures'] and self.clock()-profile.get('switched_at',0)>=config['cooldown']:
                try:
                    result=self.switch(cid,profile['reserve'])
                    profile.update(active='reserve',switched_at=int(self.clock()))
                    cascade.save(self.path,config)
                    counter.update(failures=0,successes=0)
                    self.event(cache,cid,'failover','WPP: «'+report['name']+'» переключено на резервный маршрут.')
                    try:report=self.inspector.inspect(cid)
                    except Exception:report['reserve_warning']='Резерв применён; повторная диагностика недоступна.'
                except Exception:report['reserve_warning']='Резерв не прошёл проверку. Предыдущий маршрут сохранён.'
            report['active_route']=profile['active']
        for node in report['nodes']:
            days=node.get('cert_days')
            key='cert:'+str(node.get('host'))+':'+str(int(self.clock())//86400)
            if isinstance(days,int) and days<=config['notifications'].get('cert_days',14) and key not in cache.get('cert_alerts',[]):
                self.event(cache,cid,'certificate','WPP: сертификат '+str(node.get('host'))+' истекает через '+str(days)+' дн.')
                cache.setdefault('cert_alerts',[]).append(key);cache['cert_alerts']=cache['cert_alerts'][-100:]
        cache['reports'][cid]=report;self.deliver(config,cache)
        cascade.save(self.path,config);cascade.save(self.cache_path,cache)
        return {'ok':True,'report':report}

    def tick(self):
        config=settings(self.path)
        if not config.get('monitor_enabled'):return {'ok':True,'paused':True}
        cache=cached(self.cache_path)
        candidates=[row['id'] for row in connections.load(self.inspector.registry_path)['connections'] if monitored(row,config,cache)]
        cursor=int(cache.get('cursor',0));chosen=[]
        for offset in range(min(2,len(candidates))):chosen.append(candidates[(cursor+offset)%len(candidates)])
        failures=0
        for cid in chosen:
            try:self.check(cid,automatic=True)
            except Exception:failures+=1
        cache=cached(self.cache_path);cache['cursor']=cursor+len(chosen);self.deliver(settings(self.path),cache);cascade.save(self.cache_path,cache)
        return {'ok':True,'checked':len(chosen),'errors':failures}

    def execute(self,raw):
        operation=raw.get('operation');cid=str(raw.get('id',''))
        if operation=='inspect':return self.check(cid)
        if operation=='forget':return self.forget(cid)
        if operation=='reserve':return self.reserve(cid,raw)
        if operation=='restore':
            config=settings(self.path);profile=config['profiles'].get(cid)
            if not profile:raise OperationsError('Основная цепочка не сохранена.')
            current=self.specification(cid)
            if current not in (profile['primary'],profile['reserve']):raise OperationsError('Цепочка изменена вручную. Сохраните резерв заново.')
            self.switch(cid,profile['primary']);profile.update(active='primary',switched_at=int(self.clock()))
            cascade.save(self.path,config);return self.check(cid)
        if operation=='settings':
            config=settings(self.path);config['monitor_enabled']=raw.get('monitor_enabled') is True
            config['failures']=clean_int(raw.get('failures',3),2,10,'Количество сбоев')
            config['cooldown']=clean_int(raw.get('cooldown',300),60,3600,'Интервал переключения')
            config['notifications']=clean_notifications(raw.get('notifications',{}),config['notifications'])
            cascade.save(self.path,config);return {'ok':True}
        if operation=='test':
            config=settings(self.path);value=clean_notifications(raw.get('notifications',{}),config['notifications'])
            self.notify(value,'WPP: тестовое уведомление мониторинга подключений.');return {'ok':True}
        if operation=='tick':return self.tick()
        raise OperationsError('Неизвестная операция мониторинга.')


def public_view(settings_path=SETTINGS,cache_path=CACHE):
    config=settings(settings_path);value=cached(cache_path)
    return {'monitor_enabled':bool(config.get('monitor_enabled')),'failures':config['failures'],'cooldown':config['cooldown'],
            'watched':config['watched'],'excluded':config['excluded'],
            'notifications':{'enabled':bool(config['notifications'].get('enabled')),'token_configured':bool(config['notifications'].get('token')),
                             'chat_id':config['notifications'].get('chat_id',''),'cert_days':config['notifications'].get('cert_days',14)},
            'profiles':{cid:{'primary':profile.get('primary'),'reserve':profile.get('reserve'),'automatic':bool(profile.get('automatic')),'active':profile.get('active','primary')} for cid,profile in config['profiles'].items()},
            'reports':value['reports'],'events':value.get('events',[])[-20:],'telegram_error':value.get('telegram_error','')}
