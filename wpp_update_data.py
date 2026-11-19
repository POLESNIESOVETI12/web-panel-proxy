"""Prepare a usable old-version view while retaining newer settings privately.

The one recovery file is part of the updater's existing transactional snapshot.
It is not an executable compatibility shim and never changes protocol types.
"""
import ast
import copy
import json
import os
from pathlib import Path
import re
import sys
import tempfile

RECOVERY = 'version-data.json'
MAX_BYTES = 32 * 1024 * 1024
MISSING = object()


def read(path, default):
    if not path.exists():
        return copy.deepcopy(default)
    if path.is_symlink() or path.stat().st_size > MAX_BYTES:
        raise ValueError('Invalid settings file.')
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError('Cannot read settings JSON.') from exc


def atomic(path, value):
    payload=(json.dumps(value,ensure_ascii=True,indent=2)+'\n').encode('utf-8')
    if len(payload)>MAX_BYTES:
        raise ValueError('Settings exceed the recovery limit.')
    fd,temporary=tempfile.mkstemp(prefix='.version-data-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            os.chmod(temporary,0o600)
            stream.write(payload);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def version(value):
    if not isinstance(value,str):raise ValueError('Invalid release version.')
    match=re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)(?:[.-](alpha|beta|rc)(\d+))?',value.strip())
    if not match:raise ValueError('Invalid release version.')
    return tuple(map(int,match.group(1,2,3)))+({'alpha':0,'beta':1,'rc':2,None:3}[match[4]],int(match[5] or 0))


def legacy_capabilities(folder):
    """Read the embedded manager used before connection registries existed."""
    source=(folder/'install-panel.sh').read_text(encoding='utf-8')
    managers=[]
    for match in re.finditer(r"<<'PY'[^\n]*\n(.*?)\nPY(?:\n|$)",source,re.S):
        try:tree=ast.parse(match[1])
        except SyntaxError:continue
        functions={node.name:node for node in tree.body if isinstance(node,ast.FunctionDef)}
        if {'load','save','add','init'}<=functions.keys():managers.append(functions)
    if len(managers)!=1:raise ValueError('Unknown release data format.')
    functions=managers[0];allowed=set()
    for node in ast.walk(functions['add']):
        if isinstance(node,ast.Compare) and isinstance(node.left,ast.Name) and node.left.id=='protocol' and len(node.ops)==1 and isinstance(node.ops[0],ast.NotIn):
            try:value=ast.literal_eval(node.comparators[0])
            except (ValueError,TypeError):continue
            if isinstance(value,(list,tuple,set)) and value and all(isinstance(item,str) and re.fullmatch(r'[a-z0-9_-]{1,32}',item) for item in value):allowed.add(frozenset(value))
    if len(allowed)!=1:raise ValueError('Unknown release data format.')
    protocols=set(next(iter(allowed)))
    if protocols & {'vless','hysteria'} and 'sync_xray' not in functions:raise ValueError('Invalid release capabilities.')
    if protocols & {'awg20','awg31'} and not (folder/'wpp_awg.py').is_file():raise ValueError('Invalid release capabilities.')
    port=re.search(r'(?m)^HYSTERIA_PORT=(\d+)\s*$',source)
    return {'protocols':protocols,'providers':{'custom'},'schema':1,'registry':False,
            'cascade':False,'ssh':False,'cdn':False,'access':False,'name_limit':80,
            'traffic': 'enforce_traffic_limits' in functions,
            'hysteria_port':int(port[1]) if port else 8443}


def capabilities(folder):
    path=folder/'wpp_connections.py'
    source=path.read_text(encoding='utf-8') if path.exists() else ''
    if not source:
        return legacy_capabilities(folder)
    if len(source)>2*1024*1024:raise ValueError('Invalid release module.')
    tree=ast.parse(source);values={}
    for node in tree.body:
        if isinstance(node,ast.Assign):
            for target in node.targets:
                if isinstance(target,ast.Name) and target.id in ('PROTOCOLS','PROVIDERS','SCHEMA'):
                    value=node.value
                    if isinstance(value,ast.Call) and isinstance(value.func,ast.Name) and value.func.id in ('set','frozenset') and len(value.args)==1 and not value.keywords:value=value.args[0]
                    values[target.id]=ast.literal_eval(value)
    for key in ('PROTOCOLS','PROVIDERS'):
        if not isinstance(values.get(key),(set,list,tuple)) or not all(isinstance(item,str) and re.fullmatch(r'[a-z0-9_-]{1,32}',item) for item in values[key]):
            raise ValueError('Invalid release capabilities.')
    strings={node.value for node in ast.walk(tree) if isinstance(node,ast.Constant) and isinstance(node.value,str)}
    # Older validators limit names to 80 characters; keep longer originals in
    # recovery rather than rename a user's connection.
    limit=80
    for node in ast.walk(tree):
        if isinstance(node,ast.Compare) and isinstance(node.left,ast.Call) and isinstance(node.left.func,ast.Name) and node.left.func.id=='len' and len(node.left.args)==1 and isinstance(node.left.args[0],ast.Name) and node.left.args[0].id=='name':
            for value in node.comparators:
                if isinstance(value,ast.Constant) and isinstance(value.value,int):limit=value.value
    manager=(folder/'install-panel.sh').read_text(encoding='utf-8')
    return {'protocols':set(values['PROTOCOLS']),'providers':set(values['PROVIDERS']),
            'schema':int(values.get('SCHEMA',1)),'registry':True,'cdn':True,'cascade':'cascade' in strings,'ssh':'ssh_entry' in strings,'access':(folder/'wpp_access.py').exists(),'name_limit':limit,
            'traffic': 'def enforce_traffic_limits(' in manager and 'traffic_limit_bytes' in manager}


def collection(document,key):
    if not isinstance(document,dict) or not isinstance(document.get(key,[]),list):raise ValueError('Invalid saved data.')
    rows=document.get(key,[]);ids=set()
    for row in rows:
        if not isinstance(row,dict) or not isinstance(row.get('id'),str) or not row['id'] or row['id'] in ids:raise ValueError('Invalid or duplicate record ID.')
        ids.add(row['id'])
    return rows


def merge(original,baseline,current):
    """Restore old fields only where the operator has not changed the old view."""
    if current==baseline:return copy.deepcopy(original)
    if all(isinstance(value,dict) for value in (original,baseline,current)):
        result=copy.deepcopy(current)
        for key in original:
            old=baseline.get(key,MISSING);now=current.get(key,MISSING)
            if old is MISSING:
                if now is MISSING:result[key]=copy.deepcopy(original[key])
            elif now is not MISSING:result[key]=merge(original[key],old,now)
        return result
    return copy.deepcopy(current)


def merge_document(original,baseline,current,keys,conflicts):
    result=merge({key:value for key,value in original.items() if key not in keys},
                 {key:value for key,value in baseline.items() if key not in keys},
                 {key:value for key,value in current.items() if key not in keys})
    for key in keys:
        old={row['id']:row for row in collection(original,key)}
        base={row['id']:row for row in collection(baseline,key)}
        now={row['id']:row for row in collection(current,key)}
        rows=[]
        for rid,row in now.items():
            if rid in old and rid in base:row=merge(old[rid],base[rid],row)
            elif rid in old and rid not in base and row!=old[rid]:
                conflicts.append({'collection':key,'record':copy.deepcopy(old[rid])})
            rows.append(row)
        # A record absent in the old view was suspended. A record present in
        # baseline and deleted by the operator must stay deleted.
        rows.extend(copy.deepcopy(row) for rid,row in old.items() if rid not in base and rid not in now)
        positions={rid:index for index,rid in enumerate(old)}
        rows.sort(key=lambda row:positions.get(row['id'],len(positions)))
        result[key]=rows
    return result


def supported(row,caps,connection=False):
    if not caps.get('client_moves') and (row.get('delivery_node') or row.get('move_owner')):
        return False
    if not connection and not policy_supported(row,caps):return False
    if row.get('protocol','web') not in caps['protocols']:return False
    config=row.get('config',{}) if connection else row.get('cdn',{})
    if not isinstance(config,dict):
        if connection or row.get('protocol')=='vless':return False
        config={}
    if connection and row.get('protocol')=='hysteria':
        if config.get('ssh_entry') and not caps.get('hysteria_ssh'):return False
        plan=config.get('cascade') or {}
        if not isinstance(plan,dict):return False
        if plan.get('mode','direct')!='direct' and not caps.get('hysteria_cascade'):return False
    if row.get('protocol')=='vless':
        if not caps.get('cdn',True) and (config.get('route')=='cdn' if connection else config.get('enabled')):return False
        if connection or config.get('enabled'):
            if caps['registry'] and config.get('provider','custom') not in caps['providers']:return False
        plan=config.get('cascade') or {}
        if not isinstance(plan,dict):return False
        if not caps['cascade'] and plan.get('mode','direct')!='direct':return False
        if not caps['ssh'] and config.get('ssh_entry'):return False
    if connection and not caps['registry']:
        if row.get('advanced'):return False
        if row.get('protocol')=='hysteria' and config.get('port') not in (None,0,caps.get('hysteria_port',8443)):return False
    return not connection or len(str(row.get('name','')))<=caps['name_limit']


def policy_supported(row,caps):
    if caps['access']:return True
    if row.get('expires_at') or row.get('start_on_first_use') or row.get('traffic_reset','never')!='never':
        return False
    quota=bool(row.get('traffic_limit_bytes') or row.get('traffic_limit_gb'))
    return not quota or (caps.get('traffic',False) and not row.get('protocols') and not row.get('subscription_id'))


def subscription_specs(sub,rows,users):
    """Resolve legacy subscription references without selecting a different route."""
    by_id={row['id']:row for row in rows};result=[];repaired=0;unresolved=0
    for raw in sub['connections']:
        if not isinstance(raw,dict):unresolved+=1;continue
        spec=copy.deepcopy(raw)
        rid=spec.get('id') or spec.get('connection_id')
        protocol=spec.get('protocol')
        if isinstance(rid,str) and rid in by_id:
            actual=by_id[rid].get('protocol')
            if protocol is None:protocol=actual
            if protocol!=actual:unresolved+=1;continue
        if protocol not in ('vless','hysteria') and (not isinstance(rid,str) or rid not in by_id):
            unresolved+=1;continue
        if not rid:
            cdn=spec.get('cdn') or {}
            if not isinstance(cdn,dict):unresolved+=1;continue
            def matches(row):
                if row.get('protocol')!=protocol:return False
                config=row.get('config') or {}
                if not isinstance(config,dict):return False
                if protocol!='vless':return True
                if not cdn.get('enabled'):return config.get('route','direct')=='direct'
                return config.get('route')=='cdn' and all(not cdn.get(key) or cdn[key]==config.get(key) for key in ('provider','public_host','sni','host_header'))
            candidates={row['id'] for row in rows if matches(row)}
            references={user.get('connection_id') for user in users if user.get('subscription_id')==sub['id']
                        and user.get('protocol')==protocol and isinstance(user.get('connection_id'),str)
                        and user['connection_id'] in by_id and by_id[user['connection_id']].get('protocol')==protocol}
            issued=references & candidates
            if len(issued)==1:rid=next(iter(issued))
            elif references and 'cdn' not in spec and len(references)==1:
                rid=next(iter(references));config=by_id[rid].get('config') or {}
                if protocol=='vless' and config.get('route')=='cdn':
                    spec['cdn']={key:value for key,value in config.items() if key in ('provider','public_host','sni','host_header','fingerprint','mode','uplink_method','padding','xmux','no_sse_header')}
                    spec['cdn']['enabled']=True
            elif references:unresolved+=1;continue
            elif len(candidates)==1:rid=next(iter(candidates))
            else:unresolved+=1;continue
        if not isinstance(rid,str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{2,63}',rid):unresolved+=1;continue
        spec.update(id=rid,protocol=protocol)
        if spec!=raw:repaired+=1
        result.append(spec)
    return result,repaired,unresolved


def prepare(folder,state,current_version,target_version):
    caps=capabilities(folder)
    caps['client_moves']=(folder/'wpp_client_moves.py').is_file()
    cascade_module=folder/'wpp_cascade.py'
    ssh_module=folder/'wpp_ssh_relay_agent.py'
    caps['hysteria_cascade']=cascade_module.is_file() and b'ENTRY_PROTOCOLS' in cascade_module.read_bytes()
    caps['hysteria_ssh']=ssh_module.is_file() and b'class UdpRelay(' in ssh_module.read_bytes()
    current_key=version(current_version);target_key=version(target_version)
    documents={'registry':read(state/'connections.json',{'schema':1,'connections':[]}),
               'accounts':read(state/'users.json',{'users':[],'subscriptions':[]})}
    for name,keys in (('registry',('connections',)),('accounts',('users','subscriptions'))):
        for key in keys:collection(documents[name],key)
    saved=read(state/RECOVERY,None);conflicts=[]
    if saved is not None:
        if not isinstance(saved,dict) or saved.get('schema')!=1 or not isinstance(saved.get('original'),dict) or not isinstance(saved.get('baseline'),dict):raise ValueError('Invalid recovery data.')
        conflicts=copy.deepcopy(saved.get('conflicts',[]))
        recovered={name:merge_document(saved['original'][name],saved['baseline'][name],documents[name],keys,conflicts)
                   for name,keys in (('registry',('connections',)),('accounts',('users','subscriptions')))}
        # Recover by capabilities, including a return to 2.9.0 from a snapshot
        # first created by 3.x. Filtering below keeps unsupported records private.
        saved['original']=recovered
        documents=copy.deepcopy(recovered)
    original=copy.deepcopy(documents)
    registry=documents['registry'];accounts=documents['accounts']
    rows=collection(registry,'connections')
    blocked={row['id'] for row in rows if not supported(row,caps,True)}
    registry['connections']=[row for row in rows if row['id'] not in blocked]
    registry['schema']=caps['schema']
    # Prevent a newer ensure() from recreating deliberately suspended defaults.
    if blocked:registry['deleted']=sorted(set(registry.get('deleted',[]))|blocked)
    def allowed(row):
        unmanaged=target_key>=current_key and row.get('protocol','web') not in caps['protocols']
        return policy_supported(row,caps) and (supported(row,caps) or unmanaged) and str(row.get('connection_id',row.get('id',''))) not in blocked
    subscription_protocols={'vless','hysteria'}
    subscription_module=folder/'wpp_subscriptions.py'
    if subscription_module.is_file() and subscription_module.stat().st_size<2*1024*1024:
        for statement in ast.parse(subscription_module.read_text(encoding='utf-8')).body:
            if isinstance(statement,ast.Assign) and any(isinstance(target,ast.Name) and target.id=='MANUAL_PROTOCOLS'
                                                         for target in statement.targets):
                manual=ast.literal_eval(statement.value)
                if isinstance(manual,(tuple,list)):
                    subscription_protocols.update(protocol for protocol in manual if isinstance(protocol,str))
    subscription_protocols &= caps['protocols']
    external_supported=(folder/'wpp_external_subscriptions.py').is_file()
    subs=collection(accounts,'subscriptions');removed_subs=set();repaired_specs=0;unresolved_specs=0
    for sub in subs:
        if sub.get('delivery_node') and not caps.get('client_moves'):removed_subs.add(sub['id'])
        if not policy_supported(sub,caps):removed_subs.add(sub['id'])
        specs=sub.get('connections')
        if isinstance(specs,list):
            specs,repaired,unresolved=subscription_specs(sub,rows,collection(accounts,'users'))
            repaired_specs+=repaired;unresolved_specs+=unresolved
            sub['connections']=[spec for spec in specs if isinstance(spec,dict) and spec.get('protocol') in subscription_protocols
                                and supported(spec,caps) and spec.get('id') not in blocked]
            sub['protocols']=list(dict.fromkeys(spec['protocol'] for spec in sub['connections']))
            has_sources=bool(sub.get('external_links') or sub.get('external_feeds'))
            if not sub['connections'] and not (external_supported and has_sources):
                removed_subs.add(sub['id'])
        else:
            sub['protocols']=[item for item in sub.get('protocols',['vless','hysteria']) if item in subscription_protocols]
            if not sub['protocols']:removed_subs.add(sub['id'])
        if not external_supported:
            sub.pop('external_links',None)
            sub.pop('external_feeds',None)
    accounts['subscriptions']=[sub for sub in subs if sub['id'] not in removed_subs]
    selections={sub['id']:{spec['id'] for spec in sub['connections']} for sub in accounts['subscriptions'] if isinstance(sub.get('connections'),list)}
    accounts['users']=[row for row in collection(accounts,'users') if allowed(row) and row.get('subscription_id') not in removed_subs
        and (row.get('subscription_id') not in selections or row.get('connection_id') in selections[row['subscription_id']])]
    live_ids={row['id'] for row in accounts['users']}
    for sub in accounts['subscriptions']:
        if isinstance(sub.get('profile_ids'),list):sub['profile_ids']=[rid for rid in sub['profile_ids'] if rid in live_ids]
    suspended=len(blocked)+len(removed_subs)+unresolved_specs+len(original['accounts'].get('users',[]))-len(accounts['users'])
    changed=documents!=original
    recovering=saved is not None
    needs_snapshot=changed or target_key<current_key
    if needs_snapshot or (saved is not None and not recovering) or conflicts:
        source_version=max((current_version,target_version) if saved is None else (saved['source_version'],current_version),key=version)
        recovery={'schema':1,'source_version':source_version,'original':original,'baseline':copy.deepcopy(documents),'conflicts':conflicts}
        # Write the recovery copy first. The enclosing shell transaction restores
        # all three files on any error, including a partial write or failed install.
        atomic(state/RECOVERY,recovery)
    elif saved is not None:
        (state/RECOVERY).unlink()
    if (state/'connections.json').exists() or registry['connections']:atomic(state/'connections.json',registry)
    if (state/'users.json').exists() or accounts['users']:atomic(state/'users.json',accounts)
    summary={'suspended_records':suspended,'recovered':recovering,'conflicts':len(conflicts),
             'legacy_subscription_specs_repaired':repaired_specs,'unresolved_subscription_specs':unresolved_specs,
             'unsupported_protocols':sorted({row.get('protocol','web') for row in rows if row['id'] in blocked}),
             'smartdns_supported':(folder/'wpp_smartdns.py').exists(),'cascade_supported':caps['cascade'],
             'protocols':sorted(caps['protocols'])}
    print('WPP_VERSION_DATA '+json.dumps(summary,ensure_ascii=True))
    return summary


def finalize(state):
    saved=read(state/RECOVERY,None)
    if saved is None:return
    saved['baseline']={'registry':read(state/'connections.json',{'schema':1,'connections':[]}),
                       'accounts':read(state/'users.json',{'users':[],'subscriptions':[]})}
    atomic(state/RECOVERY,saved)


def prepare_caddy(folder,path):
    """Remove only our SmartDNS gateway settings before an older install.

    Leaving the managed https_port/default_bind after stopping SmartDNS would
    strand the older panel on a private TLS port. The shell snapshot owns undo.
    """
    if (folder/'wpp_smartdns.py').exists() or not path.exists():return False
    if path.is_symlink() or path.stat().st_size>MAX_BYTES:raise ValueError('Invalid Caddy file.')
    text=path.read_text(encoding='utf-8')
    result=re.sub(r'(?m)\n[ \t]*# WPP SMARTDNS ([A-Z]+) BEGIN\n.*?^[ \t]*# WPP SMARTDNS \1 END\n','',text,flags=re.S)
    if '# WPP SMARTDNS ' in result:raise ValueError('Incomplete managed Caddy section.')
    if result==text:return False
    metadata=path.stat();fd,temporary=tempfile.mkstemp(prefix='.version-caddy-',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as stream:
            stream.write(result);stream.flush();os.fsync(stream.fileno())
        if hasattr(os,'chown'):os.chown(temporary,metadata.st_uid,metadata.st_gid)
        os.chmod(temporary,metadata.st_mode & 0o777)
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)
    print('Managed SmartDNS gateway removed for the selected release; direct HTTPS restored.')
    return True


def error_summary(error):
    stages={'read','atomic','version','capabilities','legacy_capabilities','collection','merge','merge_document','supported','policy_supported','subscription_specs','prepare','finalize','prepare_caddy'}
    stage='prepare';trace=error.__traceback__
    while trace is not None:
        name=trace.tb_frame.f_code.co_name
        if name in stages:stage=name
        trace=trace.tb_next
    known=(OSError,ValueError,KeyError,TypeError,SyntaxError,IndexError)
    kind=next((item.__name__ for item in known if isinstance(error,item)),'Error')
    return {'stage':stage,'error':kind}


if __name__=='__main__':
    try:
        if sys.argv[1]=='prepare':prepare(Path(sys.argv[2]),Path(sys.argv[3]),sys.argv[4],sys.argv[5])
        elif sys.argv[1]=='finalize':finalize(Path(sys.argv[2]))
        elif sys.argv[1]=='prepare-caddy':prepare_caddy(Path(sys.argv[2]),Path(sys.argv[3]))
        else:raise ValueError('Unknown operation.')
    except (OSError,ValueError,KeyError,TypeError,SyntaxError,IndexError) as error:
        print('WPP_DATA_ERROR '+json.dumps(error_summary(error)),file=sys.stderr,flush=True)
        print('Could not prepare saved settings for the selected version. The previous state will be restored.',file=sys.stderr)
        raise SystemExit(1)
