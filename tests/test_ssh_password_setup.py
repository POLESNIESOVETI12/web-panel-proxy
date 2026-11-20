import copy
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import wpp_ssh_cascade as ssh
import wpp_connections as connections

OLD='SHA256:'+'A'*43
NEW='SHA256:'+'B'*43
HOST='1.1.1.1'

class SimpleSSH(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'ssh.json';self.registry=Path(self.tmp.name)/'connections.json'
        self.state=ssh.load(self.path)
        self.nid=hashlib.sha256((HOST+':22').encode()).hexdigest()[:16]
        self.node={'host':HOST,'ssh_port':22,'fingerprint':OLD,'key':'existing-private-key'}
        self.state['servers'][self.nid]=copy.deepcopy(self.node)
        self.state['routes']={'other-profile':{'server_id':self.nid,'config':{'listen_port':8443}}}
        ssh.save(self.path,self.state);self.before=self.path.read_bytes()
        self.transport=Mock();self.actual=NEW;self.sessions=[]
        def connect(host,port):
            session=ssh.SSH(host,port);session.pin=self.actual;session.transport=self.transport
            session.command=Mock(return_value='{"ok":true,"protocol":3}')
            self.sessions.append(session)
            class Context:
                def __enter__(_):return session
                def __exit__(_, *args):pass
            return Context()
        self.connect=Mock(side_effect=connect)
        self.controller=ssh.Controller(self.path,self.registry,'exit.example',lambda:'9.9.9.9',Mock(return_value={'ok':True}),self.connect)
        self.bootstrap=Mock();self.addCleanup(patch.stopall)
        patch.object(ssh,'bootstrap',self.bootstrap).start()
        self.keys=Mock();self.keys.RSAKey.from_private_key.return_value='existing-key-object'
        self.keys.RSAKey.generate.return_value.write_private_key.side_effect=lambda stream:stream.write('new-private-key')
        patch.object(ssh,'dependency',return_value=self.keys).start()

    def request(self,**updates):return {'host':HOST,'password':'fixture-password',**updates}

    def test_reinstalled_server_needs_only_ip_and_password(self):
        self.bootstrap.side_effect=lambda *args:self.assertEqual(self.path.read_bytes(),self.before)
        nid,node=self.controller.ensure_server(self.state,self.request())
        self.assertEqual(nid,self.nid);self.assertEqual(node['fingerprint'],NEW)
        self.assertEqual(node['key'],'existing-private-key')
        self.connect.assert_called_once_with(HOST,22)
        self.transport.auth_password.assert_called_once_with('root','fixture-password')
        self.bootstrap.assert_called_once_with(self.sessions[0],self.state['panel_id'],'existing-key-object')
        saved=ssh.load(self.path)
        self.assertEqual(saved,self.state)
        self.assertEqual(saved['routes']['other-profile']['config']['listen_port'],8443)
        self.assertNotIn('fixture-password',self.path.read_text())

    def test_first_setup_needs_no_fingerprint_and_saves_after_bootstrap(self):
        self.state['servers']={};ssh.save(self.path,self.state);self.before=self.path.read_bytes()
        self.bootstrap.side_effect=lambda *args:self.assertEqual(self.path.read_bytes(),self.before)
        _,node=self.controller.ensure_server(self.state,self.request())
        self.assertEqual(node['fingerprint'],NEW);self.assertEqual(node['key'],'new-private-key')
        self.connect.assert_called_once_with(HOST,22)

    def test_first_setup_requires_password(self):
        self.state['servers']={}
        with self.assertRaisesRegex(ssh.SSHError,'пароль root'):
            self.controller.ensure_server(self.state,{'host':HOST})
        self.connect.assert_not_called()

    def test_wrong_password_keeps_old_trust_and_routes(self):
        self.transport.auth_password.side_effect=RuntimeError('wrong password')
        with self.assertRaisesRegex(ssh.SSHError,'отклонил'):
            self.controller.ensure_server(self.state,self.request())
        self.bootstrap.assert_not_called();self.assertEqual(self.path.read_bytes(),self.before)
        self.assertEqual(self.state['servers'][self.nid],self.node)

    def test_bootstrap_failure_keeps_old_trust_and_routes(self):
        self.bootstrap.side_effect=ssh.SSHError('bootstrap failed')
        with self.assertRaisesRegex(ssh.SSHError,'bootstrap failed'):
            self.controller.ensure_server(self.state,self.request())
        self.assertEqual(self.path.read_bytes(),self.before)
        self.assertEqual(self.state['servers'][self.nid],self.node)

    def test_new_server_failure_is_not_saved(self):
        self.state['servers']={};ssh.save(self.path,self.state);self.before=self.path.read_bytes()
        self.bootstrap.side_effect=ssh.SSHError('bootstrap failed')
        with self.assertRaises(ssh.SSHError):self.controller.ensure_server(self.state,self.request())
        self.assertEqual(self.path.read_bytes(),self.before);self.assertFalse(self.state['servers'])

    def test_background_rpc_still_refuses_changed_key(self):
        with self.assertRaisesRegex(ssh.SSHError,'введите пароль root заново'):
            self.controller.rpc(self.node,{'operation':'status','id':'fixture'})
        self.transport.auth_password.assert_not_called();self.transport.auth_publickey.assert_not_called()
        self.assertEqual(self.path.read_bytes(),self.before)

    def test_background_rpc_uses_saved_key_when_host_is_unchanged(self):
        self.actual=OLD
        self.assertTrue(self.controller.rpc(self.node,{'operation':'status','id':'fixture'})['ok'])
        self.transport.auth_publickey.assert_called_once_with('root','existing-key-object')
        self.transport.auth_password.assert_not_called();self.assertEqual(self.path.read_bytes(),self.before)

    def test_existing_key_only_workflow_and_retarget_need_no_password(self):
        _,node=self.controller.ensure_server(self.state,{'host':HOST})
        self.connect.assert_not_called();self.assertEqual(node,self.node)

    def test_port_change_without_password_keeps_host_verification(self):
        with self.assertRaisesRegex(ssh.SSHError,'введите пароль root заново'):
            self.controller.ensure_server(self.state,{'host':HOST,'ssh_port':2222})
        self.transport.auth_publickey.assert_not_called();self.assertEqual(self.path.read_bytes(),self.before)

    def test_port_change_with_password_keeps_server_identity(self):
        nid,node=self.controller.ensure_server(self.state,self.request(ssh_port=2222))
        self.assertEqual(nid,self.nid);self.assertEqual(node['ssh_port'],2222)
        self.assertEqual(node['fingerprint'],NEW);self.assertEqual(len(self.state['servers']),1)

    def test_old_wizard_payload_does_not_block_password_setup(self):
        _,node=self.controller.ensure_server(self.state,self.request(fingerprint=OLD,replace_fingerprint=''))
        self.assertEqual(node['fingerprint'],NEW)

    def test_invalid_password_type_does_not_connect(self):
        with self.assertRaises(ssh.SSHError):self.controller.ensure_server(self.state,self.request(password=['bad']))
        self.connect.assert_not_called()

    def test_oversized_password_is_not_sent_or_saved(self):
        with self.assertRaises(ssh.SSHError):self.controller.ensure_server(self.state,self.request(password='x'*1025))
        self.transport.auth_password.assert_not_called();self.assertEqual(self.path.read_bytes(),self.before)

    def test_full_dual_bridge_provision_for_vless_and_hysteria(self):
        for cid in ('vless-direct','hysteria-primary'):
            with self.subTest(cid=cid):
                registry=connections.load(self.registry);connections.save(self.registry,registry)
                def rpc(node,request):
                    self.assertEqual(node['fingerprint'],NEW)
                    return {'ok':True,'protocol':3,'config':None}
                with patch.object(self.controller,'rpc',side_effect=rpc) as calls:
                    result=self.controller.execute(self.request(operation='provision',id=cid,
                        middle={'host':'8.8.8.8','password':'second-password'}))
                self.assertTrue(result['ok']);self.assertEqual(result['servers'],2)
                configs=[c.args[1]['config'] for c in calls.call_args_list if c.args[1]['operation']=='configure']
                self.assertEqual(len(configs),2)
                self.assertTrue(all(c.get('transport','tcp')==('udp' if cid=='hysteria-primary' else 'tcp') for c in configs))
                saved=ssh.load(self.path)
                self.assertEqual(saved['routes']['other-profile']['server_id'],self.nid)
                for value in ('fixture-password','second-password'):self.assertNotIn(value,self.path.read_text())

if __name__=='__main__':unittest.main()
