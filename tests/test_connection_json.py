"""Connection-pencil editor, persistence and simulated service transactions."""
import ast
import copy
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, quote, urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import wpp_connections as connections
import wpp_cdn as cdn
import wpp_cdn_doctor as doctor
import wpp_routing as routing
import wpp_ui as ui

INSTALLER = (ROOT / 'install-panel.sh').read_text(encoding='utf-8')
MANAGER = INSTALLER.split('cat > "$MANAGER" <<\'PY\'\n', 1)[1].split('\nPY\n', 1)[0]
PANEL = INSTALLER.split('cat > "$APP_FILE" <<\'PY\'\n', 1)[1].split('\nPY\n', 1)[0]
PATH = '/vless-' + 'a'*24
UUID = '11111111-1111-4111-8111-111111111111'


def inbound():
    return {'tag':'vless-xhttp', 'listen':'127.0.0.1', 'port':10000,
            'protocol':'vless', 'settings':{'clients':[{'id':UUID,'email':'panel:test:1','level':0}], 'decryption':'none'},
            'streamSettings':{'network':'xhttp','security':'none',
                              'xhttpSettings':{'path':PATH+'/stream','mode':'auto'}},
            'sniffing':{'enabled':True,'destOverride':['http','tls'],'routeOnly':True}}


def functions(source, names, namespace):
    nodes = [node for node in ast.parse(source).body
             if isinstance(node,ast.FunctionDef) and node.name in names]
    if len(nodes) != len(names): raise AssertionError('Missing extracted function')
    exec(compile(ast.Module(body=nodes,type_ignores=[]),'real-panel-functions.py','exec'),namespace)


class ConnectionJSON(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.file=self.root/'connections.json'
        self.registry=connections.save(str(self.file),{'connections':connections.defaults()})
        self.live=inbound()

    def edited(self):
        value=copy.deepcopy(self.live)
        value['tag']='my-vless'
        value['port']=11000
        value['streamSettings']['xhttpSettings']['path']='/custom/api/stream'
        value['sniffing']={'enabled':False}
        value['allocate']={'strategy':'always'}
        return value

    def advanced(self):
        return connections.advanced_from_inbound(self.edited(),current=self.live)

    def test_edit_keeps_port_path_tag_and_exact_nested_values(self):
        advanced=self.advanced()
        self.registry['connections'][0]['advanced']=advanced
        connections.save(str(self.file),self.registry)
        stored=connections.load(str(self.file))['connections'][0]['advanced']
        self.assertEqual(connections.apply_server_advanced(self.live,stored),self.edited())
        self.assertNotIn('xray_manual',connections.load(str(self.file)))

    def test_clients_remain_dynamic_after_an_edit(self):
        value=inbound(); value['settings']['clients']=[]
        self.assertEqual(connections.apply_server_advanced(value,self.advanced())['settings']['clients'],[])
        value['settings']['clients']=[{'id':'new-uuid','email':'panel:new:1'}]
        self.assertEqual(connections.apply_server_advanced(value,self.advanced())['settings']['clients'],value['settings']['clients'])

    def test_changed_clients_are_explicitly_rejected_not_silently_discarded(self):
        value=self.edited(); value['settings']['clients']=[]
        with self.assertRaisesRegex(connections.ConnectionError,'clients изменён'):
            connections.advanced_from_inbound(value,current=self.live)

    def test_global_config_and_routing_are_rejected_with_explanation(self):
        for value in ({'inbounds':[]}, {**self.live,'dns':{}}, {**self.live,'routing':{}}):
            with self.subTest(value=value), self.assertRaises(connections.ConnectionError):
                connections.advanced_from_inbound(value,current=self.live)

    def test_invalid_managed_topology_is_not_accepted(self):
        for path,field,value in [('streamSettings','network','tcp'),('streamSettings','security','tls')]:
            item=self.edited(); item[path][field]=value
            with self.assertRaisesRegex(connections.ConnectionError,'Caddy'):
                connections.advanced_from_inbound(item,current=self.live)

    def test_invalid_nested_shapes_and_caddy_injection_are_rejected(self):
        for value in (None,[],True,{'path':'/ok\n respond 200'}, {'path':'/ok{bad}'}, {'path':'/ok?param=1'}):
            item=self.edited(); item['streamSettings']['xhttpSettings']=value
            with self.subTest(value=value), self.assertRaises(connections.ConnectionError):
                connections.advanced_from_inbound(item,current=self.live)

    def test_caddy_uses_edited_path_and_port_and_is_idempotent(self):
        self.registry['connections'][0]['advanced']=self.advanced()
        original='vpn.example {\n    reverse_proxy 127.0.0.1:8080 {\n    }\n}\n'
        actual=doctor.connection_routes(original,PATH,self.registry)
        self.assertIn('@web_panel_vless_direct path /custom/api/stream /custom/api/stream/*',actual)
        self.assertIn('reverse_proxy 127.0.0.1:11000',actual)
        self.assertIn('reverse_proxy 127.0.0.1:10005',actual)
        self.assertEqual(doctor.connection_routes(actual,PATH,self.registry),actual)

    def test_caddy_detects_listener_and_path_collisions(self):
        for key,value in [('port',10001),('port',10085),('path',cdn.endpoint_path(PATH,'cdn'))]:
            edited=self.edited()
            if key=='path': edited['streamSettings']['xhttpSettings']['path']=value
            else: edited[key]=value
            self.registry['connections'][0]['advanced']=connections.advanced_from_inbound(edited,current=self.live)
            with self.subTest(key=key,value=value), self.assertRaisesRegex(ValueError,'уже используется'):
                doctor.connection_routes('vpn.example {\nreverse_proxy 127.0.0.1:8080 {\n}\n}',PATH,self.registry)

    def test_other_protocols_are_not_overwritten(self):
        before=copy.deepcopy(self.registry['connections'][1:])
        row=copy.deepcopy(self.registry['connections'][0]); row['advanced']=self.advanced()
        connections.upsert(str(self.file),row)
        self.assertEqual(connections.load(str(self.file))['connections'][1:],before)

    def test_custom_link_and_routed_json_use_edited_path(self):
        row=copy.deepcopy(self.registry['connections'][0]); row['advanced']=self.advanced()
        ns={'cdn':cdn,'connection_store':connections,'DOMAIN':'vpn.example','quote':quote,'urlencode':urlencode,
            'json':json,'proxy_user':lambda *args:{},'proxy_connection':lambda user:row,'xray_path':lambda:PATH}
        functions(PANEL,{'proxy_link'},ns)
        link=ns['proxy_link']('vless',UUID,name='🌐 Test')
        self.assertEqual(parse_qs(urlsplit(link).query)['path'],['/custom/api/stream'])
        config=routing.xray_config(link,routing.validate({}))
        outbound=next(x for x in config['outbounds'] if x['protocol']=='vless')
        self.assertEqual(outbound['streamSettings']['xhttpSettings']['path'],'/custom/api/stream')

    def test_runtime_readback_respects_changed_tag_and_inactive_templates(self):
        row=copy.deepcopy(self.registry['connections'][0]); row['advanced']=self.advanced()
        runtime=self.root/'runtime.json'; runtime.write_text(json.dumps({'inbounds':[self.edited()]}))
        ns={'copy':copy,'json':json,'XRAY_RUNTIME_CONFIG':str(runtime),'connection_store':connections,
            'routing_store':routing,'ROUTING_FILE':str(self.root/'routing.json'),'cdn':cdn,'xray_path':lambda:PATH}
        functions(PANEL,{'connections_with_runtime'},ns)
        self.assertEqual(ns['connections_with_runtime']([row])[0]['runtime_inbound'],self.edited())
        runtime.unlink()
        preview=ns['connections_with_runtime']([row])[0]['runtime_inbound']
        self.assertEqual(preview['port'],11000)
        self.assertEqual(preview['settings']['clients'],[])
        self.assertNotIn('Будет создан',json.dumps(preview,ensure_ascii=False))

    def test_ui_editor_is_inside_pencil_dialog_not_a_global_mode(self):
        markup=ui.connections_ui(self.registry['connections'],[], '/panel','csrf')
        dialog=markup[markup.index('<dialog'):markup.index('</dialog>')]
        self.assertIn('name="xray_json"',dialog)
        self.assertIn('data-edit-connection',markup)
        self.assertNotIn('fullXrayEditor',markup)
        self.assertNotIn('/xray-config',INSTALLER)
        self.assertIn('data-xray-routing-preview',dialog)
        self.assertNotIn('delete value.dns',markup)

    def test_real_generator_preserves_json_and_tracks_current_enabled_clients(self):
        row=copy.deepcopy(self.registry['connections'][0]); row['advanced']=self.advanced()
        connections.upsert(str(self.file),row)
        path_file=self.root/'path'; path_file.write_text(PATH)
        caddy=self.root/'Caddyfile'; caddy.write_text('vpn.example {\nreverse_proxy 127.0.0.1:8080 {\n}\n}')
        captures=[]
        ns={'copy':copy,'json':json,'os':os,'wpp_connections':connections,'wpp_cdn':cdn,'wpp_cdn_doctor':doctor,
            'CONNECTIONS':str(self.file),'XRAY_PATH_FILE':str(path_file),'CADDYFILE':str(caddy),
            'XRAY_API':'127.0.0.1:10085','HYSTERIA_PORT':8443,'XRAY_CERT':'unused','XRAY_KEY':'unused',
            'XRAY_VLESS_PORT':10000,'XRAY_VLESS_CDN_PORT':10001,'XRAY_VLESS_BEGET_PORT':10002,
            'XRAY_VLESS_SELECTEL_PORT':10003,'XRAY_VLESS_VK_PORT':10004,'XRAY_VLESS_TIMEWEB_PORT':10005,
            'direct_secrets':lambda user:user.get('device_secrets',[user['secret']]),
            'activate_xray_config':lambda *args:captures.append(copy.deepcopy(args))}
        functions(MANAGER,{'sync_xray'},ns)
        users=[{'id':'b'*16,'protocol':'vless','connection_id':'vless-direct','secret':UUID,'enabled':True}]
        ns['sync_xray']({'users':users})
        config,caddy_text,inactive=captures[-1]
        self.assertEqual(config['inbounds'][0]['tag'],'my-vless')
        self.assertEqual(config['inbounds'][0]['port'],11000)
        self.assertEqual(config['inbounds'][0]['settings']['clients'][0]['id'],UUID)
        self.assertIn('/custom/api/stream',caddy_text)
        users[0]['enabled']=False
        ns['sync_xray']({'users':users})
        config,_,inactive=captures[-1]
        self.assertEqual(config['inbounds'],[])
        self.assertEqual(inactive[0]['tag'],'my-vless')
        self.assertEqual(inactive[0]['settings']['clients'],[])

    def test_hysteria_port_and_tls_readback_are_consistent(self):
        row=copy.deepcopy(self.registry['connections'][1])
        row['advanced']={'inbound':{'tag':'my-hysteria','port':18443,'protocol':'hysteria','listen':'0.0.0.0',
            'settings':{'version':2},'streamSettings':{'network':'hysteria','security':'tls'}}}
        connections.validate(row,existing=row)
        self.assertEqual(connections.hysteria_port({'connections':[row]}),18443)
        markup=ui.connections_ui([row],[], '/panel','csrf')
        self.assertIn('18443',markup)
        self.assertIn('/tls/domain.crt',PANEL)
        self.assertIn('/tls/domain.key',PANEL)


class Activation(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.runtime=self.root/'config.json'; self.runtime.write_text('{"previous": true}')
        self.caddy=self.root/'Caddyfile'; self.caddy.write_text('previous caddy')
        self.running=True; self.enabled=True; self.failure=None; self.calls=[]
        self.caddy_can_reload=True
        def run(*args,check=False,**kwargs):
            self.calls.append(args); code=0
            if args[:2]==('systemctl','is-active'): code=0 if self.running else 3
            elif args[:2]==('systemctl','is-enabled'): code=0 if self.enabled else 1
            elif args[:2]==('systemctl','restart'):
                if self.failure=='restart': self.failure=None; code=1; self.running=False
                else: self.running=True
            elif args[:2]==('systemctl','enable'): self.enabled=True
            elif args[:2]==('systemctl','disable'): self.enabled=False
            elif args[:2]==('systemctl','stop'): self.running=False
            if args[0]=='xray' and self.failure=='validate': code=1
            if args[:2]==('caddy','validate') and self.failure=='caddy-validate': code=1
            if args[:3]==('systemctl','reload','caddy.service') and not self.caddy_can_reload:
                code=1
            if args[:3]==('systemctl','reload-or-restart','caddy.service') and self.failure=='reload':
                self.failure=None; code=1
            if check and code: raise RuntimeError('simulated service failure')
            return SimpleNamespace(returncode=code,stdout='',stderr='simulated rejection' if code else '')
        self.ns={'os':os,'copy':copy,'json':json,'grp':SimpleNamespace(getgrnam=lambda *a:SimpleNamespace(gr_gid=0)),
                 'shutil':shutil,'time':SimpleNamespace(sleep=lambda *a:None),'run':run,
                 'wpp_cdn_doctor':doctor,'XRAY_CONFIG':str(self.runtime),'XRAY_BIN':'xray',
                 'XRAY_SERVICE':'xray.service','CADDYFILE':str(self.caddy)}
        functions(MANAGER,{'activate_xray_config'},self.ns)
        for context in (patch.object(os,'chown',lambda *a:None,create=True),patch.object(shutil,'chown',lambda *a,**k:None)):
            context.start(); self.addCleanup(context.stop)

    def test_success_applies_both_configs(self):
        self.ns['activate_xray_config']({'inbounds':[inbound()]},'new caddy')
        self.assertEqual(json.loads(self.runtime.read_text())['inbounds'],[inbound()])
        self.assertEqual(self.caddy.read_text(),'new caddy')

    def test_unit_without_exec_reload_does_not_abort_update(self):
        self.caddy_can_reload=False
        self.assertNotEqual(self.ns['run']('systemctl','reload','caddy.service').returncode,0)
        self.calls=[]
        self.ns['activate_xray_config']({'inbounds':[inbound()]},'new caddy')
        self.assertEqual(self.caddy.read_text(),'new caddy')
        self.assertIn(('systemctl','reload-or-restart','caddy.service'),self.calls)
        self.assertNotIn(('systemctl','reload','caddy.service'),self.calls)

    def test_rollback_also_supports_unit_without_exec_reload(self):
        self.caddy_can_reload=False
        self.failure='restart'
        with self.assertRaises(RuntimeError):
            self.ns['activate_xray_config']({'inbounds':[inbound()]},'new caddy')
        self.assertEqual(self.caddy.read_text(),'previous caddy')
        self.assertEqual(self.runtime.read_text(),'{"previous": true}')
        self.assertTrue(self.running)
        self.assertIn(('systemctl','reload-or-restart','caddy.service'),self.calls)

    def test_unchanged_caddy_does_not_reload_or_restart_it(self):
        self.ns['activate_xray_config']({'inbounds':[inbound()]},'previous caddy')
        self.assertNotIn(('systemctl','reload-or-restart','caddy.service'),self.calls)

    def test_panel_and_manager_do_not_require_caddy_exec_reload(self):
        self.assertNotIn('"systemctl","reload","caddy.service"',INSTALLER)

    def test_validation_does_not_replace_or_restart(self):
        for failure in ('validate','caddy-validate'):
            self.failure=failure; self.calls=[]
            with self.subTest(failure=failure), self.assertRaises(ValueError):
                self.ns['activate_xray_config']({'inbounds':[inbound()]},'new caddy')
            self.assertEqual(self.runtime.read_text(),'{"previous": true}')
            self.assertEqual(self.caddy.read_text(),'previous caddy')
            self.assertNotIn(('systemctl','restart','xray.service'),self.calls)

    def test_start_or_caddy_reload_failure_restores_both_configs(self):
        for failure in ('restart','reload'):
            self.failure=failure
            with self.subTest(failure=failure), self.assertRaises(RuntimeError):
                self.ns['activate_xray_config']({'inbounds':[inbound()]},'new caddy')
            self.assertEqual(self.runtime.read_text(),'{"previous": true}')
            self.assertEqual(self.caddy.read_text(),'previous caddy')
            self.assertTrue(self.running)
            self.assertTrue(self.enabled)
            self.assertFalse((self.root/'config.tmp.json').exists())
            self.assertFalse((self.root/'Caddyfile.xray-tmp').exists())

    def test_json_only_edit_always_refreshes_xray(self):
        calls=[]
        registry={'connections':connections.defaults()}
        ns={'copy':copy,'json':json,'load':lambda:{'users':[],'subscriptions':[]},
            'wpp_connections':SimpleNamespace(ensure=lambda *a:registry,sync_users=lambda *a:False,sync_subscriptions=lambda *a:False),
            'CONNECTIONS':'unused','sync_xray':lambda *a:calls.append('xray'),
            'wpp_extra_protocols':SimpleNamespace(sync=lambda *a:None),'os':os,
            'XRAY_CERT':'cert','XRAY_KEY':'key','sync_firewall':lambda *a:None}
        functions(MANAGER,{'refresh_connections'},ns)
        ns['refresh_connections']()
        self.assertEqual(calls,['xray'])


class PencilHTTP(unittest.TestCase):
    """Exercise the real POST handler, with a stubbed Linux service manager."""
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name); self.file=root/'connections.json'
        connections.save(str(self.file),{'connections':connections.defaults()})
        self.current=inbound(); self.failure=None; self.calls=[]
        def manager(command,request):
            self.calls.append(command)
            if self.failure=='validation': raise ValueError('Xray не принял JSON: simulated validation')
            if self.failure=='service':
                self.failure=None
                raise RuntimeError('simulated failed service')
            return {'ok':True}
        ns={'BaseHTTPRequestHandler':BaseHTTPRequestHandler,'node_api':SimpleNamespace(API_PREFIX='/api-test'),
            'PANEL_PATH':'/panel-test','SUB_PREFIX':'/sub/','MAX_HTML_BYTES':1024*1024,'urlparse':urlparse,
            'json':json,'re':re,'parse_qs':parse_qs,'load':lambda:{},'esc':str,'sys':sys,
            'connection_store':connections,'cdn':cdn,'CONNECTIONS_FILE':str(self.file),
            'connections_with_runtime':lambda rows:[{**row,'runtime_inbound':copy.deepcopy(self.current)} for row in rows],
            'ctl_manager_json':manager,'users':lambda:[]}
        original=next(node for node in ast.parse(PANEL).body if isinstance(node,ast.ClassDef) and node.name=='Handler')
        names={'do_POST','form','send_json','send_data','redirect'}
        methods=[node for node in original.body if isinstance(node,ast.FunctionDef) and node.name in names]
        cls=ast.ClassDef(name='Handler',bases=original.bases,keywords=[],body=methods,decorator_list=[])
        exec(compile(ast.fix_missing_locations(ast.Module(body=[cls],type_ignores=[])),'real-pencil-http.py','exec'),ns)
        handler=ns['Handler']; handler.log_message=lambda *a:None
        handler.auth=lambda h:h.headers.get('X-Test-Auth')=='yes'
        handler.valid_csrf=lambda h,form:form.get('csrf')=='csrf'
        handler.send_html=lambda h,body,code=200:h.send_data(body,code,'text/html')
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler)
        self.worker=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':0.01},daemon=True)
        self.worker.start(); self.addCleanup(self.close)

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.worker.join(2)

    def post(self,value=None,auth=True,**changes):
        form={'operation':'save','id':'vless-direct','name':'Edited VLESS','protocol':'vless','route':'direct',
              'csrf':'csrf','xray_json':json.dumps(self.current if value is None else value),**changes}
        headers={'X-WPP-Async':'1','Content-Type':'application/x-www-form-urlencoded'}
        if auth: headers['X-Test-Auth']='yes'
        request=Request('http://127.0.0.1:'+str(self.server.server_port)+'/panel-test/connection-action',
                        data=urlencode(form).encode(),headers=headers)
        try: response=urlopen(request,timeout=3)
        except HTTPError as error: response=error
        with response: return response.status,response.read()

    def test_pencil_form_persists_changed_path_port_and_tag(self):
        value=copy.deepcopy(self.current); value['port']=11000; value['tag']='custom-tag'
        value['streamSettings']['xhttpSettings']['path']='/new/path'
        code,body=self.post(value)
        self.assertEqual(code,200,body)
        stored=connections.get(connections.load(str(self.file)),'vless-direct')
        self.assertEqual(connections.apply_server_advanced(self.current,stored['advanced']),value)
        self.assertEqual(self.calls,['connections-refresh'])

    def test_csrf_rejection_never_modifies_registry(self):
        before=self.file.read_bytes()
        code,_=self.post(csrf='wrong')
        self.assertEqual(code,403); self.assertEqual(self.file.read_bytes(),before); self.assertFalse(self.calls)

    def test_managed_clients_edit_is_rejected_before_service_call(self):
        value=copy.deepcopy(self.current); value['settings']['clients']=[]
        before=self.file.read_bytes()
        code,body=self.post(value)
        self.assertEqual(code,400,body); self.assertFalse(self.calls); self.assertEqual(self.file.read_bytes(),before)

    def test_invalid_json_does_not_reach_service(self):
        code,_=self.post(xray_json='{broken')
        self.assertEqual(code,400); self.assertFalse(self.calls)

    def test_service_failure_restores_registry(self):
        before=json.loads(self.file.read_text())
        for failure in ('validation','service'):
            self.failure=failure
            code,_=self.post()
            self.assertIn(code,(400,503))
            self.assertEqual(json.loads(self.file.read_text()),before)

    def test_edit_does_not_enable_a_disabled_connection(self):
        data=connections.load(str(self.file)); data['connections'][0]['enabled']=False; connections.save(str(self.file),data)
        code,body=self.post()
        self.assertEqual(code,200,body)
        self.assertFalse(connections.get(connections.load(str(self.file)),'vless-direct')['enabled'])


if __name__=='__main__': unittest.main()
