"""SSH-managed RU ingress for a panel that remains on the foreign exit server.

Password-based setup accepts the host key obtained in that same SSH session.
Ongoing management pins the saved host key and uses a key restricted on the
RU server to our isolated relay agent. Root passwords are never persisted.
"""
import base64
import copy
import hashlib
import io
import ipaddress
import json
from pathlib import Path
import re
import secrets
import socket

from wpp_cascade import save


class SSHError(ValueError):pass


def supports_connection(connection):
    if not connection:
        return False
    protocol=connection.get('protocol')
    return protocol=='hysteria' or (protocol=='vless' and connection.get('config',{}).get('route')=='direct')


def public_ip(value):
    try:address=ipaddress.ip_address(str(value).strip())
    except ValueError:raise SSHError('Укажите публичный IPv4 RU-сервера.') from None
    if address.version!=4 or not address.is_global or address.is_multicast:raise SSHError('Нужен публичный IPv4 сервера.')
    return str(address)


def port(value, default=22):
    if isinstance(value,bool):raise SSHError('Некорректный порт.')
    try:result=int(default if value is None or value=='' else value)
    except (ValueError,TypeError):raise SSHError('Некорректный порт.') from None
    if not 1<=result<=65535:raise SSHError('Порт должен быть от 1 до 65535.')
    return result


def inline_request(form,connection):
    """Translate connection editor fields without persisting SSH credentials."""
    if form.get('ssh_inline')!='1':return None
    host=str(form.get('ssh_host','')).strip()
    password=form.get('ssh_password','')
    if not supports_connection(connection) or connection.get('config',{}).get('cascade',{}).get('mode','direct')!='direct':
        if host or password:raise SSHError('RU-сервер можно добавить к Hysteria2 или прямому VLESS без моста между панелями.')
        return None
    if not host:
        if password or form.get('ssh_double_hop')=='1':raise SSHError('Укажите IP первого RU-сервера.')
        return {'operation':'remove'} if connection.get('config',{}).get('ssh_entry') else None
    def hop(prefix):
        value={'host':public_ip(form.get(prefix+'host')),'ssh_port':port(form.get(prefix+'port')),
               'listen_port':port(form.get(prefix+'listen_port'),443),'password':form.get(prefix+'password','')}
        if not isinstance(value['password'],str) or len(value['password'])>1024:raise SSHError('Некорректный пароль root RU-сервера.')
        return value
    request={'operation':'provision',**hop('ssh_')}
    exit_ip=str(form.get('ssh_exit_ip','')).strip()
    if exit_ip:request['exit_ip']=public_ip(exit_ip)
    if form.get('ssh_double_hop')=='1':
        request['middle']=hop('ssh_middle_')
        if request['middle']['host']==request['host']:raise SSHError('Для второго RU-сервера укажите другой IP.')
    if exit_ip and exit_ip in {request['host'],request.get('middle',{}).get('host')}:
        raise SSHError('RU-сервер и зарубежный выход должны быть разными VPS.')
    return request


def client_entry(raw):
    if not isinstance(raw,dict) or not re.fullmatch(r'[a-f0-9]{16}',str(raw.get('server_id',''))):
        raise SSHError('Некорректный SSH-сервер моста.')
    return {'server_id':raw['server_id'],'host':public_ip(raw.get('host')),'port':port(raw.get('port'),443)}


def apply_client(route,connection):
    result=copy.deepcopy(route)
    raw=connection.get('config',{}).get('ssh_entry') if connection else None
    if raw:
        value=client_entry(raw);result.update(address=value['host'],port=value['port'])
    return result


def load(path):
    try:state=json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:return {'schema':1,'panel_id':secrets.token_hex(8),'servers':{},'routes':{}}
    except (ValueError,OSError):raise SSHError('Не удалось прочитать настройки SSH-моста.') from None
    if not isinstance(state,dict) or state.get('schema')!=1 or not re.fullmatch(r'[a-f0-9]{16}',str(state.get('panel_id',''))) or not isinstance(state.get('servers'),dict) or not isinstance(state.get('routes'),dict):
        raise SSHError('Настройки SSH-моста повреждены.')
    return state


def dependency():
    try:import paramiko
    except ImportError:raise SSHError('Установите обновление с SSH-мостом: отсутствует python3-paramiko.') from None
    return paramiko


def fingerprint(key):return 'SHA256:'+base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')


class SSH:
    def __init__(self,host,ssh_port):
        self.host=public_ip(host);self.port=port(ssh_port)
        self.transport=None

    def __enter__(self):
        paramiko=dependency()
        try:
            sock=socket.create_connection((self.host,self.port),timeout=10)
            self.transport=paramiko.Transport(sock);self.transport.banner_timeout=10;self.transport.auth_timeout=15
            self.transport.start_client(timeout=10)
            self.pin=fingerprint(self.transport.get_remote_server_key())
            return self
        except Exception:
            self.__exit__(None,None,None)
            raise SSHError('Не удалось подключиться к SSH. Проверьте IP, порт и доступность сервера.') from None

    def auth(self,pin,password=None,private_key=None):
        if not isinstance(pin,str) or not secrets.compare_digest(self.pin,pin):
            raise SSHError('SSH-ключ RU-сервера изменился. Откройте настройку моста и введите пароль root заново.')
        try:
            if private_key:self.transport.auth_publickey('root',dependency().RSAKey.from_private_key(io.StringIO(private_key)))
            else:
                if not isinstance(password,str) or not password or len(password)>1024:raise SSHError('Введите пароль root.')
                self.transport.auth_password('root',password)
        except SSHError:raise
        except Exception:raise SSHError('SSH отклонил вход root. Проверьте пароль и разрешение SSH-входа.') from None

    def command(self,command,payload,timeout=35):
        channel=self.transport.open_session(timeout=10);channel.settimeout(timeout)
        try:
            channel.exec_command(command)
            channel.sendall(payload.encode('utf-8'));channel.shutdown_write()
            out=bytearray();err_size=0
            import time
            end=time.monotonic()+timeout
            while True:
                if channel.recv_ready():out.extend(channel.recv(32768))
                if channel.recv_stderr_ready():err_size+=len(channel.recv_stderr(32768))
                if len(out)>65536 or err_size>65536:raise SSHError('Слишком большой ответ SSH.')
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():break
                if time.monotonic()>end:raise SSHError('SSH не завершил настройку вовремя.')
                time.sleep(.02)
            if channel.recv_exit_status()!=0:raise SSHError('Не удалось выполнить настройку RU-сервера.')
            return out.decode('utf-8')
        except (OSError,ValueError):raise SSHError('SSH-соединение прервалось. Повторите проверку.') from None
        finally:channel.close()

    def __exit__(self,*_):
        if self.transport:self.transport.close()


def bootstrap(session,panel,key):
    agent=Path(__file__).with_name('wpp_ssh_relay_agent.py').read_text(encoding='utf-8')
    public='ssh-rsa '+key.get_base64()
    # JSON is embedded as a Python string literal, never interpolated into shell commands.
    payload=json.dumps({'panel':panel,'agent':agent,'key':public})
    code='''import json,os,pathlib,tempfile
payload=json.loads(PAYLOAD)
assert os.geteuid()==0
parent=pathlib.Path('/opt/web-panel-proxy-ssh')
assert not parent.is_symlink()
parent.mkdir(mode=0o755,exist_ok=True);os.chmod(parent,0o755)
base=parent/payload['panel']
if base.exists():
    assert not base.is_symlink() and (base/'owner').read_text()==payload['panel']
else:
    base.mkdir(mode=0o755);os.chmod(base,0o755)
    (base/'owner').write_text(payload['panel'])
agent=base/'agent.py'
assert not agent.is_symlink()
fd,tmp=tempfile.mkstemp(dir=base)
with os.fdopen(fd,'w') as stream:stream.write(payload['agent'])
os.chmod(tmp,0o644);os.replace(tmp,agent)
ssh=pathlib.Path('/root/.ssh');ssh.mkdir(mode=0o700,exist_ok=True)
assert not ssh.is_symlink()
os.chmod(ssh,0o700)
authorized=ssh/'authorized_keys';assert not authorized.is_symlink()
old=authorized.read_text() if authorized.exists() else ''
comment='wpp-ssh-cascade:'+payload['panel']
line='restrict,command="/usr/bin/python3 '+str(agent)+' rpc" '+payload['key']+' '+comment
lines=[value for value in old.splitlines() if not value.endswith(' '+comment)]
lines.append(line)
fd,tmp=tempfile.mkstemp(dir=ssh)
with os.fdopen(fd,'w') as stream:stream.write('\\n'.join(lines)+'\\n')
os.chmod(tmp,0o600);os.replace(tmp,authorized)
print('WPP_READY')
'''.replace('PAYLOAD',repr(payload),1)
    # Minimal Ubuntu/Debian images may lack Python. No panel, web server or Xray is installed here.
    session.command("sh -c 'command -v python3 >/dev/null || { export DEBIAN_FRONTEND=noninteractive; apt-get -o DPkg::Lock::Timeout=30 update </dev/null >&2 && apt-get -o DPkg::Lock::Timeout=30 install -y --no-install-recommends python3 </dev/null >&2; }; exec python3 -'",code,timeout=180)


class Controller:
    def __init__(self,state_path,registry_path,exit_domain,exit_ip,probe,connect=SSH):
        self.path=state_path;self.registry_path=registry_path;self.domain=exit_domain
        self.exit_ip=exit_ip;self.probe=probe;self.connect=connect

    def commit(self,registry,state):
        import wpp_connections as store
        journal=Path(str(self.path)+'.transaction')
        save(journal,{'registry':registry,'state':state})
        store.save(self.registry_path,registry);save(self.path,state)
        journal.unlink()

    def recover(self):
        journal=Path(str(self.path)+'.transaction')
        if journal.exists():
            value=json.loads(journal.read_text(encoding='utf-8'))
            self.commit(value['registry'],value['state'])

    def cleanup(self):
        state=load(self.path);pending=state.get('garbage',[]);kept=[]
        for index,peer in enumerate(pending):
            if index>=4:kept.append(peer);continue
            try:self.rpc(state['servers'][peer['server_id']],{'operation':'remove','id':peer['id']})
            except (SSHError,KeyError):kept.append(peer)
        if kept!=pending:state['garbage']=kept;save(self.path,state)
        return len(kept)

    @staticmethod
    def hops(route):
        return route.get('hops') or [{'server_id':route['server_id'],'config':route['config']}]

    def detach(self,cid):
        state=load(self.path);route=state['routes'].pop(cid,None)
        if not route:return False
        rid=hashlib.sha256(cid.encode()).hexdigest()[:24]
        pending=False
        for hop in self.hops(route):
            try:self.rpc(state['servers'][hop['server_id']],{'operation':'remove','id':rid})
            except SSHError:
                state.setdefault('garbage',[]).append({'server_id':hop['server_id'],'id':rid});pending=True
        save(self.path,state)
        return pending

    def rpc(self,node,request):
        with self.connect(node['host'],node['ssh_port']) as session:
            session.auth(node['fingerprint'],private_key=node['key'])
            try:result=json.loads(session.command('wpp-cascade',json.dumps(request)))
            except ValueError:raise SSHError('Некорректный ответ службы RU-сервера.') from None
        if not isinstance(result,dict) or not result.get('ok'):
            if isinstance(result,dict) and result.get('message')=='Port is occupied':raise SSHError('Выбранный порт RU-сервера занят. Укажите свободный порт.')
            raise SSHError('Не удалось применить службу на RU-сервере.')
        return result

    def ensure_server(self,state,raw):
        host=public_ip(raw.get('host'));ssh_port=port(raw.get('ssh_port'))
        nid=hashlib.sha256((host+':'+str(ssh_port)).encode()).hexdigest()[:16]
        node=state['servers'].get(nid)
        if node is None:
            match=next(((key,value) for key,value in state['servers'].items() if value.get('host')==host),None)
            if match:nid,node=match
        password=raw.get('password')
        if password is not None and not isinstance(password,str):
            raise SSHError('Введите пароль root RU-сервера.')
        has_password=bool(password)
        if node is None:
            if len(state['servers'])>=64:raise SSHError('Достигнут лимит SSH-серверов моста.')
            if not has_password:raise SSHError('Введите пароль root RU-сервера.')
            key=dependency().RSAKey.generate(3072);private=io.StringIO();key.write_private_key(private)
            with self.connect(host,ssh_port) as session:
                # The administrator explicitly supplied a password to set up
                # this endpoint. Learn its key and authenticate in one session.
                session.auth(session.pin,password=password)
                bootstrap(session,state['panel_id'],key)
                node={'host':host,'ssh_port':ssh_port,'fingerprint':session.pin,'key':private.getvalue()}
            state['servers'][nid]=node;save(self.path,state)
        elif has_password or node['ssh_port']!=ssh_port:
            key=dependency().RSAKey.from_private_key(io.StringIO(node['key']))
            with self.connect(host,ssh_port) as session:
                expected=session.pin if has_password else node['fingerprint']
                session.auth(expected,password=password,private_key=None if has_password else node['key'])
                if has_password:bootstrap(session,state['panel_id'],key)
            node={**node,'ssh_port':ssh_port,'fingerprint':expected}
            state['servers'][nid]=node;save(self.path,state)
        return nid,node

    def health(self,node,rid):
        try:return self.rpc(node,{'operation':'health','id':rid})
        except SSHError:
            result=self.rpc(node,{'operation':'status','id':rid})
            return {**result,'active':None,'next_reachable':None,'next_ms':None,'legacy_agent':True}

    def specification(self,cid):
        state=load(self.path);route=state['routes'].get(cid)
        if not route:raise SSHError('RU-вход ещё не настроен.')
        hops=self.hops(route)
        return {'kind':'ssh','entry':{'server_id':hops[0]['server_id'],'listen_port':hops[0]['config']['listen_port']},
                'middle':{'server_id':hops[1]['server_id'],'listen_port':hops[1]['config']['listen_port']} if len(hops)>1 else None,
                'exit_ip':route.get('exit_ip') or hops[-1]['config']['target_ip']}

    def retarget(self,cid,spec):
        state=load(self.path);current=self.specification(cid)
        if spec.get('kind')!='ssh' or spec.get('entry')!=current['entry'] or spec.get('exit_ip')!=current['exit_ip']:
            raise SSHError('Резерв должен сохранять входной адрес, порт и зарубежный выход.')
        def request(hop):
            node=state['servers'].get(hop.get('server_id'))
            if not node:raise SSHError('SSH-сервер резерва не найден.')
            return {'host':node['host'],'ssh_port':node['ssh_port'],'listen_port':port(hop.get('listen_port'),443)}
        raw={'operation':'provision','id':cid,'exit_ip':current['exit_ip'],**request(spec['entry'])}
        if spec.get('middle'):raw['middle']=request(spec['middle'])
        return self.execute(raw)

    def execute(self,raw):
        import wpp_connections as store
        operation=raw.get('operation')
        if operation=='inspect':
            with self.connect(raw.get('host'),raw.get('ssh_port')) as session:
                return {'ok':True,'fingerprint':session.pin,'host':session.host,'ssh_port':session.port}
        if operation not in ('provision','check','remove'):raise SSHError('Неизвестная операция SSH-моста.')
        registry=store.load(self.registry_path);connection=store.get(registry,raw.get('id'))
        if not supports_connection(connection):
            raise SSHError('Выберите Hysteria2 или прямое VLESS-подключение зарубежной панели.')
        if connection.get('config',{}).get('cascade',{}).get('mode','direct')!='direct':
            raise SSHError('Сначала отключите мост между панелями у этого подключения.')
        state=load(self.path);cid=connection['id'];old=copy.deepcopy(state['routes'].get(cid))
        rid=hashlib.sha256(cid.encode()).hexdigest()[:24]
        if operation in ('check','remove'):
            if not old:raise SSHError('RU-вход для этого подключения ещё не настроен.')
            node=state['servers'][old['server_id']]
            if operation=='check':
                self.rpc(node,{'operation':'status','id':rid})
                report=self.probe(connection,node['host'],old['config']['listen_port'])
                if not isinstance(report,dict) or not report.get('ok'):raise SSHError('Нет выхода в интернет через RU-вход.')
                return {**report,'ok':True}
            pending=self.detach(cid);state=load(self.path)
            connection['config'].pop('ssh_entry',None);self.commit(registry,state)
            return {'ok':True,'cleanup_pending':pending}
        inputs=[raw]
        if raw.get('middle'):
            if not isinstance(raw['middle'],dict):raise SSHError('Некорректный второй RU-сервер.')
            inputs.append(raw['middle'])
        target=public_ip(raw.get('exit_ip') or self.exit_ip())
        addresses=[public_ip(hop.get('host')) for hop in inputs]
        if len(set(addresses+[target]))!=len(addresses)+1:raise SSHError('Каждый RU-сервер и зарубежный выход должны быть разными VPS.')
        ports=[port(hop.get('listen_port'),443) for hop in inputs]
        udp=connection['protocol']=='hysteria'
        nodes=[self.ensure_server(state,hop) for hop in inputs]
        hops=[]
        for index,((nid,node),listen) in enumerate(zip(nodes,ports)):
            config={'listen_port':listen,'target_ip':nodes[index+1][1]['host'] if index+1<len(nodes) else target,
                    'target_port':ports[index+1] if index+1<len(nodes) else 443}
            if udp:
                config['transport']='udp'
            hops.append({'server_id':nid,'config':config})
        statuses=[]
        for hop in hops:
            node=state['servers'][hop['server_id']]
            status=self.rpc(node,{'operation':'status','id':rid})
            if udp and status.get('protocol',0)<3:
                raise SSHError('На RU-сервере '+node['host']+' установлена старая служба. Введите пароль root этого сервера, чтобы обновить её для UDP.')
            statuses.append((node,status.get('config'),hop['config']))
        touched=[]
        try:
            for node,previous,config in reversed(statuses):
                # The command can apply remotely before its SSH reply is lost.
                touched.append((node,previous))
                self.rpc(node,{'operation':'configure','id':rid,'config':config})
            first=state['servers'][hops[0]['server_id']]
            report=self.probe(connection,first['host'],ports[0])
            if not isinstance(report,dict) or not report.get('ok'):raise SSHError('Нет выхода в интернет через RU-вход.')
            connection['config']['ssh_entry']={'server_id':hops[0]['server_id'],'host':first['host'],'port':ports[0]}
            desired=copy.deepcopy(state)
            desired['routes'][cid]={'server_id':hops[0]['server_id'],'config':hops[0]['config'],'hops':hops,'exit_ip':target}
            if old:
                for previous in self.hops(old):
                    old_node=state['servers'][previous['server_id']]
                    if old_node['host'] not in addresses:desired.setdefault('garbage',[]).append({'server_id':previous['server_id'],'id':rid})
            self.commit(registry,desired)
        except Exception:
            if Path(str(self.path)+'.transaction').exists():
                raise SSHError('RU-вход настроен, но сохранение прервалось. Повторите проверку: панель завершит сохранение.') from None
            failed=False
            for node,previous in reversed(touched):
                try:self.rpc(node,{'operation':'configure','id':rid,'config':previous} if previous else {'operation':'remove','id':rid})
                except SSHError:failed=True
            if failed:raise SSHError('Не удалось восстановить часть RU-служб. Откройте диагностику моста.') from None
            raise
        pending=self.cleanup()>0
        return {**report,'ok':True,'internet':True,'cleanup_pending':pending,'entry':client_entry(connection['config']['ssh_entry']),'servers':len(hops)}
