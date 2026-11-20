import copy
import hashlib
import json
from pathlib import Path
import re
import sys
from types import ModuleType
import unittest
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import wpp_ssh_cascade as ssh
import wpp_connections as connections

import importlib.util
spec=importlib.util.spec_from_file_location('_ssh_http_base',ROOT/'tests/test_connection_json.py')
base=importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)

HOST='1.1.1.1';PIN='SHA256:'+'B'*43

class IntegratedSSH(base.PencilHTTP):
    def setUp(self):
        super().setUp()
        self.state_path=self.file.with_name('ssh.json');self.remote={};self.failure_probe=False
        self.transport=Mock()
        self.keys=Mock();self.keys.RSAKey.generate.return_value.write_private_key.side_effect=lambda stream:stream.write('fixture-private-key')
        self.addCleanup(patch.stopall)
        patch.object(ssh,'dependency',return_value=self.keys).start()
        self.bootstrap=patch.object(ssh,'bootstrap',Mock()).start()
        def connect(host,port):
            session=ssh.SSH(host,port);session.pin=PIN;session.transport=self.transport
            class Context:
                def __enter__(_):return session
                def __exit__(_, *args):pass
            return Context()
        self.controller=ssh.Controller(self.state_path,self.file,'exit.example',lambda:'9.9.9.9',
            lambda *args:{'ok':not self.failure_probe},connect)
        def rpc(node,request):
            key=(node['host'],request['id'])
            if request['operation']=='status':return {'ok':True,'protocol':3,'config':self.remote.get(key)}
            if request['operation']=='configure':self.remote[key]=copy.deepcopy(request['config'])
            if request['operation']=='remove':self.remote.pop(key,None)
            return {'ok':True}
        self.controller.rpc=rpc
        def manager(command,request):
            self.calls.append(command)
            self.controller.recover()
            if command=='ssh-cascade':
                try:return self.controller.execute(request)
                except ssh.SSHError as error:return {'ok':False,'message':str(error),
                    'pending_commit':Path(str(self.state_path)+'.transaction').exists()}
            if self.failure=='validation':raise ValueError('Xray не принял JSON: simulated validation')
            if self.failure=='service':self.failure=None;raise RuntimeError('simulated failed service')
            return {'ok':True}
        namespace=self.server.RequestHandlerClass.do_POST.__globals__
        namespace['ctl_manager_json']=manager;namespace['ssh_store']=ssh

    def create(self,**changes):
        return self.post(id='',xray_json='',ssh_inline='1',ssh_host=HOST,
                         ssh_password='fixture-password',**changes)

    def test_create_configures_and_checks_in_one_http_save(self):
        code,body=self.create()
        self.assertEqual(code,200,body)
        result=json.loads(body);self.assertTrue(result['bridge']['internet'])
        item=next(row for row in connections.load(self.file)['connections'] if row['name']=='Edited VLESS')
        self.assertEqual(item['config']['ssh_entry']['host'],HOST)
        self.assertEqual(self.calls,['connections-refresh','ssh-cascade'])
        self.assertTrue(self.remote)
        for path in (self.file,self.state_path):self.assertNotIn('fixture-password',path.read_text())
        self.assertNotIn('fixture-password',body.decode())

    def test_new_hysteria_with_two_servers_needs_no_separate_wizard(self):
        code,body=self.create(protocol='hysteria',ssh_double_hop='1',ssh_middle_host='8.8.8.8',
                              ssh_middle_password='second-password')
        self.assertEqual(code,200,body);self.assertEqual(json.loads(body)['bridge']['servers'],2)
        self.assertEqual(len(self.remote),2)
        self.assertTrue(all(config['transport']=='udp' for config in self.remote.values()))

    def test_probe_failure_restores_registry_and_remote_config(self):
        before=connections.load(self.file);self.failure_probe=True
        code,body=self.create()
        self.assertEqual(code,400,body);self.assertIn('Нет выхода',json.loads(body)['message'])
        self.assertEqual(connections.load(self.file),before);self.assertFalse(self.remote)
        self.assertEqual(self.calls,['connections-refresh','ssh-cascade','connections-refresh'])
        self.assertFalse(ssh.load(self.state_path)['routes'])

    def test_existing_connection_failure_keeps_old_name_and_bridge(self):
        code,body=self.post(xray_json='',ssh_inline='1',ssh_host=HOST,ssh_password='fixture-password')
        self.assertEqual(code,200,body)
        before=connections.load(self.file);remote=copy.deepcopy(self.remote)
        self.failure_probe=True
        code,body=self.post(name='Failed edit',xray_json='',ssh_inline='1',ssh_host=HOST,
                            ssh_password='fixture-password',ssh_listen_port='8443')
        self.assertEqual(code,400,body)
        self.assertEqual(connections.load(self.file),before);self.assertEqual(self.remote,remote)

    def test_wrong_password_does_not_leave_a_created_connection(self):
        before=connections.load(self.file)
        self.transport.auth_password.side_effect=ValueError('wrong password')
        code,body=self.create()
        self.assertEqual(code,400,body);self.assertIn('отклонил',json.loads(body)['message'])
        self.assertEqual(connections.load(self.file),before);self.assertFalse(self.remote)
        self.bootstrap.assert_not_called()

    def test_clearing_ip_removes_bridge_through_the_same_editor(self):
        code,body=self.post(xray_json='',ssh_inline='1',ssh_host=HOST,ssh_password='fixture-password')
        self.assertEqual(code,200,body)
        code,body=self.post(xray_json='',ssh_inline='1',ssh_host='',ssh_password='')
        self.assertEqual(code,200,body)
        self.assertNotIn('ssh_entry',connections.get(connections.load(self.file),'vless-direct')['config'])
        self.assertFalse(self.remote)

    def test_reinstalled_server_is_rebound_by_password_from_ordinary_editor(self):
        code,body=self.post(xray_json='',ssh_inline='1',ssh_host=HOST,ssh_password='fixture-password')
        self.assertEqual(code,200,body)
        state=ssh.load(self.state_path);nid=next(iter(state['servers']))
        state['servers'][nid]['fingerprint']='SHA256:'+'A'*43;ssh.save(self.state_path,state)
        code,body=self.post(xray_json='',ssh_inline='1',ssh_host=HOST,ssh_password='new-password')
        self.assertEqual(code,200,body)
        self.assertEqual(ssh.load(self.state_path)['servers'][nid]['fingerprint'],PIN)

    def test_invalid_ip_rejected_before_manager_or_registry_change(self):
        before=connections.load(self.file)
        code,body=self.post(xray_json='',ssh_inline='1',ssh_host='127.0.0.1',ssh_password='fixture-password')
        self.assertEqual(code,400,body);self.assertFalse(self.calls)
        self.assertEqual(connections.load(self.file),before)

    def test_invalid_csrf_does_not_send_credentials(self):
        code,_=self.create(csrf='wrong')
        self.assertEqual(code,403);self.assertFalse(self.calls);self.transport.auth_password.assert_not_called()

    def test_direct_connection_without_ip_keeps_existing_save_behavior(self):
        code,body=self.post(xray_json='',ssh_inline='1',ssh_host='',ssh_password='')
        self.assertEqual(code,200,body);self.assertIsNone(json.loads(body)['bridge'])
        self.assertEqual(self.calls,['connections-refresh']);self.bootstrap.assert_not_called()

    def test_duplicate_ru_addresses_are_rejected_before_any_ssh(self):
        code,body=self.create(ssh_double_hop='1',ssh_middle_host=HOST,ssh_middle_password='second-password')
        self.assertEqual(code,400,body);self.assertFalse(self.calls);self.transport.auth_password.assert_not_called()

    def test_cdns_cannot_silently_gain_an_ssh_bridge(self):
        before=connections.load(self.file)
        code,body=self.create(route='cdn',provider='custom',public_host='cdn.example')
        self.assertEqual(code,400,body);self.assertFalse(self.calls)
        self.assertEqual(connections.load(self.file),before)

    def test_interrupted_commit_is_recovered_without_overwriting_its_journal(self):
        original_save=ssh.save
        journal=Path(str(self.state_path)+'.transaction')
        def fail_state_save(path,value):
            if Path(path)==self.state_path and journal.exists():raise OSError('simulated interrupted write')
            return original_save(path,value)
        with patch.object(ssh,'save',side_effect=fail_state_save):code,body=self.create()
        self.assertEqual(code,400,body);self.assertTrue(journal.exists());self.assertTrue(self.remote)
        self.assertEqual(self.calls,['connections-refresh','ssh-cascade'])
        pending=json.loads(journal.read_text())
        item=next(row for row in pending['registry']['connections'] if row['name']=='Edited VLESS')
        code,body=self.post(id=item['id'],xray_json='',ssh_inline='1',ssh_host=HOST,ssh_password='fixture-password')
        self.assertEqual(code,200,body);self.assertFalse(journal.exists())
        self.assertEqual(connections.get(connections.load(self.file),item['id'])['config']['ssh_entry']['host'],HOST)

if __name__=='__main__':
    # Include the inherited editor/CSRF/rollback cases against the same handler.
    unittest.main()
