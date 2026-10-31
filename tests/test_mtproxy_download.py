"""Bounded Telegram downloads and authenticated public offline data, no VPS changes."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
BASH='C:/Program Files/Git/bin/bash.exe' if os.name=='nt' else '/bin/bash'


class MTProxyBootstrap(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)
        self.bin=self.folder/'bin'; self.bin.mkdir()
        self.log=self.folder/'curl-arguments.txt'; self.output=self.folder/'result'
        fake=self.bin/'curl'
        fake.write_text('''#!/usr/bin/env bash
printf '%s\\n' "$@" > "$MOCK_LOG"
[[ "$MOCK_MODE" != fail ]] || exit 28
out=""; next=0
for arg in "$@"; do
  if (( next )); then out="$arg"; next=0; fi
  case "$arg" in --output|-o) next=1;; esac
done
[[ -z "$out" ]] || cp -- "$MOCK_RESPONSE" "$out"
''',encoding='utf-8',newline='\n')
        fake.chmod(0o755)
        self.env={**os.environ,'MOCK_LOG':self.log.as_posix(),'MOCK_MODE':'good',
                  'MOCK_RESPONSE':(ROOT/'assets/mtproxy-bootstrap/proxy-secret').as_posix(),
                  'WEB_PANEL_PROXY_MTPROXY_DATA_DIR':(ROOT/'assets/mtproxy-bootstrap').as_posix(),
                  'WEB_PANEL_PROXY_MTPROXY_OFFLINE':'0'}

    def run_shell(self,command):
        code='test_bin="$1"; if command -v cygpath >/dev/null; then test_bin="$(cygpath -u "$1")"; fi; export PATH="$test_bin:$PATH"; hash -r; [[ "$(type -P curl)" == "$test_bin/curl" ]] || exit 99; source ./mtproxy-download.sh; '+command
        return subprocess.run([BASH,'-c',code,'test',self.bin.as_posix(),self.output.as_posix()],
                              cwd=ROOT,env=self.env,capture_output=True,text=True,timeout=10)

    def test_offline_files_work_through_nested_bash_without_network(self):
        self.env['WEB_PANEL_PROXY_MTPROXY_OFFLINE']='1'
        for endpoint,name in [('getProxySecret','proxy-secret'),('getProxyConfig','proxy-multi.conf')]:
            result=self.run_shell('bash -c \'curl --output "$1" https://core.telegram.org/'+endpoint+'\' child "$2"')
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(self.output.read_bytes(),(ROOT/'assets/mtproxy-bootstrap'/name).read_bytes())
            self.assertFalse(self.log.exists())

    def test_online_fetch_keeps_certificate_checks_and_adds_ipv4_timeouts(self):
        result=self.run_shell('curl --fail --proto =https --tlsv1.2 --output "$2" https://core.telegram.org/getProxySecret')
        self.assertEqual(result.returncode,0,result.stderr)
        args=self.log.read_text().splitlines()
        for option in ('-4','--connect-timeout','--max-time','--retry','--proto','--tlsv1.2'):
            self.assertIn(option,args)
        self.assertNotIn('-k',args)
        self.assertNotIn('--insecure',args)
        self.assertEqual(args[args.index('--max-time')+1],'30')

    def test_offline_handler_survives_all_installer_subprocess_levels(self):
        self.env['WEB_PANEL_PROXY_MTPROXY_OFFLINE']='1'
        stage=self.folder/'stage.sh'
        stage.write_text('''#!/usr/bin/env bash
if (( $1 > 0 )); then bash "$0" "$(($1-1))" "$2"; else curl --output "$2" https://core.telegram.org/getProxySecret; fi
''',encoding='utf-8',newline='\n')
        self.env['MOCK_STAGE']=stage.as_posix()
        result=self.run_shell('bash "$MOCK_STAGE" 5 "$2"')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(len(self.output.read_bytes()),128)
        self.assertFalse(self.log.exists())

    def test_timeout_uses_verified_snapshot(self):
        self.env['MOCK_MODE']='fail'
        result=self.run_shell('curl --output "$2" https://core.telegram.org/getProxySecret')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('curl 28',result.stderr)
        self.assertEqual(len(self.output.read_bytes()),128)

    def test_core_installer_uses_fallback_only_inside_mtproxy_step(self):
        package=self.folder/'package'; package.mkdir()
        shutil.copyfile(ROOT/'mtproxy-download.sh',package/'mtproxy-download.sh')
        shutil.copytree(ROOT/'assets/mtproxy-bootstrap',package/'assets/mtproxy-bootstrap')
        upstream=package/'upstream.sh'
        upstream.write_text('''#!/usr/bin/env bash
set -euo pipefail
curl --fail --proto =https --tlsv1.2 --output "$RESULT_SECRET" https://core.telegram.org/getProxySecret
curl --fail --proto =https --tlsv1.2 --output "$RESULT_CONFIG" https://core.telegram.org/getProxyConfig
''',encoding='utf-8',newline='\n')
        upstream.chmod(0o755)
        core=(ROOT/'install-webproxy-core.sh').read_text(encoding='utf-8')
        block=core.split('    MT_DOWNLOAD_HELPER=',1)[1].split('\nelse\n',1)[0]
        runner=package/'core-step.sh'
        runner.write_text('''#!/usr/bin/env bash
set -euo pipefail
die() { echo "$*" >&2; exit 1; }
MT_INSTALLER="$(cd "$(dirname "$0")" && pwd)/upstream.sh"
MT_DOWNLOAD_HELPER='''+block+'''
[[ "$(type -t curl)" == file ]]
''',encoding='utf-8',newline='\n')
        self.env.pop('WEB_PANEL_PROXY_MTPROXY_DATA_DIR')
        self.env.update(MOCK_MODE='fail',RESULT_SECRET=self.output.as_posix(),
                        RESULT_CONFIG=(self.folder/'config').as_posix())
        command='test_bin="$1"; if command -v cygpath >/dev/null; then test_bin="$(cygpath -u "$1")"; fi; export PATH="$test_bin:$PATH"; hash -r; [[ "$(type -P curl)" == "$test_bin/curl" ]] || exit 99; bash "$2"'
        result=subprocess.run([BASH,'-c',command,'test',self.bin.as_posix(),runner.as_posix()],
                              cwd=ROOT,env=self.env,capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.output.read_bytes(),(ROOT/'assets/mtproxy-bootstrap/proxy-secret').read_bytes())
        self.assertEqual((self.folder/'config').read_bytes(),(ROOT/'assets/mtproxy-bootstrap/proxy-multi.conf').read_bytes())

    def test_invalid_http_body_is_rejected_and_uses_verified_snapshot(self):
        response=self.folder/'html'; response.write_text('<html>not Telegram</html>')
        self.env['MOCK_RESPONSE']=response.as_posix()
        result=self.run_shell('curl --output "$2" https://core.telegram.org/getProxySecret')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.output.read_bytes(),(ROOT/'assets/mtproxy-bootstrap/proxy-secret').read_bytes())

    def test_tampered_snapshot_does_not_replace_destination(self):
        self.env['WEB_PANEL_PROXY_MTPROXY_OFFLINE']='1'
        bad=self.folder/'bad'; bad.mkdir(); (bad/'proxy-secret').write_bytes(b'x'*128)
        self.env['WEB_PANEL_PROXY_MTPROXY_DATA_DIR']=bad.as_posix()
        self.output.write_bytes(b'previous')
        result=self.run_shell('curl --output "$2" https://core.telegram.org/getProxySecret')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('checksum mismatch',result.stderr)
        self.assertEqual(self.output.read_bytes(),b'previous')

    def test_tampered_config_snapshot_is_rejected(self):
        self.env['WEB_PANEL_PROXY_MTPROXY_OFFLINE']='1'
        bad=self.folder/'bad'; bad.mkdir()
        source=(ROOT/'assets/mtproxy-bootstrap/proxy-multi.conf').read_bytes()
        (bad/'proxy-multi.conf').write_bytes(source+b'\n# modified\n')
        self.env['WEB_PANEL_PROXY_MTPROXY_DATA_DIR']=bad.as_posix()
        result=self.run_shell('curl --output "$2" https://core.telegram.org/getProxyConfig')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('checksum mismatch',result.stderr)
        self.assertFalse(self.output.exists())

    def test_missing_output_is_rejected(self):
        result=self.run_shell('curl https://core.telegram.org/getProxySecret')
        self.assertEqual(result.returncode,2)
        self.assertFalse(self.log.exists())

    def test_unrelated_large_downloads_are_unchanged(self):
        result=self.run_shell('curl --fail --output "$2" https://example.org/large-archive.tar.gz')
        self.assertEqual(result.returncode,0,result.stderr)
        args=self.log.read_text().splitlines()
        self.assertNotIn('--max-time',args)
        self.assertNotIn('-4',args)


if __name__=='__main__': unittest.main()
