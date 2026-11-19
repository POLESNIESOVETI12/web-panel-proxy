"""Headless TCP and QUIC ingress managed by a restricted SSH command."""
import asyncio
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
from collections import deque


UDP_IDLE_SECONDS = 180
UDP_MAX_SESSIONS = 2048


def route_config(raw):
    if not isinstance(raw, dict): raise ValueError('Invalid relay configuration')
    address=ipaddress.ip_address(str(raw.get('target_ip', '')))
    if not address.is_global or address.is_multicast: raise ValueError('A public exit address is required')
    ports=[raw.get(key) for key in ('listen_port', 'target_port')]
    if any(type(port) is not int or not 1 <= port <= 65535 for port in ports):
        raise ValueError('Invalid relay port')
    transport=raw.get('transport','tcp')
    if transport not in ('tcp','udp'):
        raise ValueError('Invalid relay transport')
    value=dict(target_ip=str(address),listen_port=ports[0],target_port=ports[1])
    if transport=='udp':
        value['transport']='udp'
    return value


def write_file(path, data, mode=0o644):
    path=Path(path)
    if path.is_symlink(): raise ValueError('Symlinks are not allowed')
    fd,temporary=tempfile.mkstemp(prefix='.wpp-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.chmod(temporary,mode);os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


class Agent:
    def __init__(self, base=None, systemd='/etc/systemd/system', run=None):
        self.base=Path(base or Path(__file__).resolve().parent)
        self.panel=self.base.name
        if not re.fullmatch(r'[a-f0-9]{16}',self.panel):raise ValueError('Invalid owner')
        self.systemd=Path(systemd)
        self.run=run or self._run

    @staticmethod
    def _run(*args):
        result=subprocess.run(args,capture_output=True,text=True,timeout=25)
        if result.returncode:raise RuntimeError('Relay service operation failed')
        return result.stdout

    def paths(self, rid):
        if not isinstance(rid,str) or not re.fullmatch(r'[a-f0-9]{24}',rid):raise ValueError('Invalid relay ID')
        unit='wpp-ssh-'+self.panel+'-'+rid+'.service'
        return self.base/(rid+'.json'),self.systemd/unit,unit

    def dispatch(self, raw):
        if not isinstance(raw,dict):raise ValueError('Invalid request')
        config,unit_path,unit=self.paths(raw.get('id'))
        if config.is_symlink() or unit_path.is_symlink():raise ValueError('Symlinks are not allowed')
        operation=raw.get('operation')
        if operation=='status':
            value=route_config(json.loads(config.read_text())) if config.exists() else None
            return {'ok':True,'protocol':3,'config':value}
        if operation=='health':
            value=route_config(json.loads(config.read_text())) if config.exists() else None
            active=False;reachable=False;elapsed=None
            if value:
                try:self.run('systemctl','is-active','--quiet',unit);active=True
                except (RuntimeError,OSError,subprocess.TimeoutExpired):pass
                if value.get('transport')=='udp':
                    # Only the panel's authenticated QUIC probe can verify reachability.
                    reachable=None
                else:
                    start=time.monotonic()
                    try:
                        with socket.create_connection((value['target_ip'],value['target_port']),timeout=3):pass
                        reachable=True;elapsed=round((time.monotonic()-start)*1000,1)
                    except OSError:pass
            return {'ok':True,'protocol':3,'active':active,'next_reachable':reachable,'next_ms':elapsed,'config':value}
        if operation=='remove':
            if unit_path.exists():self.run('systemctl','disable','--now',unit)
            for path in (unit_path,config):
                if path.is_symlink():raise ValueError('Symlinks are not allowed')
                path.unlink(missing_ok=True)
            self.run('systemctl','daemon-reload')
            return {'ok':True}
        if operation!='configure':raise ValueError('Unsupported operation')
        value=route_config(raw.get('config'))
        before=config.read_bytes() if config.exists() else None
        old_unit=unit_path.read_bytes() if unit_path.exists() else None
        if (before is None)!=(old_unit is None):raise ValueError('Incomplete managed service; repair before reconfiguring')
        transport=value.get('transport','tcp')
        previous=route_config(json.loads(before)) if before is not None else {}
        if previous.get('listen_port')!=value['listen_port'] or previous.get('transport','tcp')!=transport:
            kind=socket.SOCK_DGRAM if transport=='udp' else socket.SOCK_STREAM
            with socket.socket(socket.AF_INET,kind) as probe:
                if transport=='tcp':
                    probe.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
                probe.bind(('0.0.0.0',value['listen_port']))
        service=f'''[Unit]
Description=WPP SSH {transport.upper()} ingress {raw['id']}
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
DynamicUser=yes
LimitNOFILE=8192
ExecStart=/usr/bin/python3 {self.base}/agent.py serve {config}
Restart=on-failure
RestartSec=2
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
UMask=0077
[Install]
WantedBy=multi-user.target
'''.encode()
        try:
            write_file(config,json.dumps(value).encode());write_file(unit_path,service)
            self.run('systemctl','daemon-reload')
            self.run('systemctl','enable',unit)
            self.run('systemctl','restart',unit)
            self.run('systemctl','is-active','--quiet',unit)
        except Exception:
            if before is None:
                try:self.run('systemctl','disable','--now',unit)
                except Exception:pass
                config.unlink(missing_ok=True);unit_path.unlink(missing_ok=True)
            else:
                write_file(config,before);write_file(unit_path,old_unit)
            self.run('systemctl','daemon-reload')
            if before is not None:self.run('systemctl','restart',unit)
            raise
        return {'ok':True,'config':value}


async def forward(reader, writer):
    while True:
        chunk=await asyncio.wait_for(reader.read(65536),timeout=600)
        if not chunk:
            if writer.can_write_eof():writer.write_eof();await writer.drain()
            return
        writer.write(chunk);await writer.drain()


async def relay(reader,writer,target,slots):
    remote=None;tasks=[]
    try:
        if slots.locked():return
        async with slots:
            upstream,remote=await asyncio.wait_for(asyncio.open_connection(target['target_ip'],target['target_port']),timeout=10)
            for stream in (writer,remote):stream.get_extra_info('socket').setsockopt(socket.SOL_SOCKET,socket.SO_KEEPALIVE,1)
            tasks=[asyncio.create_task(forward(reader,remote)),asyncio.create_task(forward(upstream,writer))]
            await asyncio.gather(*tasks)
    except (OSError,asyncio.TimeoutError,ConnectionError):pass
    finally:
        for task in tasks:
            if not task.done():task.cancel()
        if tasks:await asyncio.gather(*tasks,return_exceptions=True)
        for stream in (writer,remote):
            if stream:
                stream.close()
                try:await asyncio.wait_for(stream.wait_closed(),timeout=3)
                except (OSError,asyncio.TimeoutError):pass


class UdpSession(asyncio.DatagramProtocol):
    def __init__(self, relay, peer):
        self.relay=relay
        self.peer=peer
        self.last_seen=relay.loop.time()
        self.transport=None
        self.pending=deque(maxlen=4)
        self.task=None

    async def connect(self):
        try:
            await self.relay.loop.create_datagram_endpoint(
                lambda:self, remote_addr=self.relay.target, family=socket.AF_INET)
        except OSError:
            self.relay.remove(self)

    def connection_made(self, transport):
        if self.relay.sessions.get(self.peer) is not self:
            transport.close()
            return
        self.transport=transport
        while self.pending:
            transport.sendto(self.pending.popleft())

    def datagram_received(self, data, addr):
        if self.relay.sessions.get(self.peer) is self:
            self.relay.transport.sendto(data,self.peer)

    def connection_lost(self, exc):
        self.relay.remove(self)


class UdpRelay(asyncio.DatagramProtocol):
    def __init__(self, target, idle_seconds=UDP_IDLE_SECONDS):
        self.loop=asyncio.get_running_loop()
        self.target=target
        self.idle_seconds=idle_seconds
        self.sessions={}
        self.transport=None
        self.timer=None

    def connection_made(self, transport):
        self.transport=transport
        self.timer=self.loop.call_later(min(15,self.idle_seconds),self.expire)

    def datagram_received(self, data, peer):
        session=self.sessions.get(peer)
        if session is None:
            # QUIC clients start with a padded long-header datagram.
            if len(data)<1200 or not data[0]&0x80 or len(self.sessions)>=UDP_MAX_SESSIONS:
                return
            session=UdpSession(self,peer)
            self.sessions[peer]=session
            session.task=self.loop.create_task(session.connect())
        session.last_seen=self.loop.time()
        if session.transport is None:
            if len(data)<=2048 and len(session.pending)<session.pending.maxlen:
                session.pending.append(data)
        else:
            session.transport.sendto(data)

    def remove(self, session):
        if self.sessions.get(session.peer) is session:
            del self.sessions[session.peer]

    def close_session(self, session):
        self.remove(session)
        if session.transport is not None:
            session.transport.close()
        elif session.task is not None:
            session.task.cancel()

    def expire(self):
        cutoff=self.loop.time()-self.idle_seconds
        for session in list(self.sessions.values()):
            if session.last_seen<=cutoff:
                self.close_session(session)
        self.timer=self.loop.call_later(min(15,self.idle_seconds),self.expire)

    def connection_lost(self, exc):
        self.timer.cancel()
        for session in list(self.sessions.values()):
            self.close_session(session)


async def serve(path):
    config=route_config(json.loads(Path(path).read_text()))
    if config.get('transport')=='udp':
        target=(config['target_ip'],config['target_port'])
        transport,_=await asyncio.get_running_loop().create_datagram_endpoint(
            lambda:UdpRelay(target),local_addr=('0.0.0.0',config['listen_port']))
        try:
            await asyncio.Future()
        finally:
            transport.close()
        return
    slots=asyncio.Semaphore(2048)
    server=await asyncio.start_server(lambda r,w:relay(r,w,config,slots),'0.0.0.0',config['listen_port'])
    async with server:await server.serve_forever()


if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1]=='serve':asyncio.run(serve(sys.argv[2]))
    elif len(sys.argv)==2 and sys.argv[1]=='rpc':
        try:
            if os.geteuid()!=0:raise ValueError('Root required')
            data=sys.stdin.buffer.read(16385)
            if len(data)>16384:raise ValueError('Request too large')
            result=Agent().dispatch(json.loads(data))
        except Exception as exc:
            result={'ok':False,'message':'Port is occupied' if isinstance(exc,OSError) and exc.errno==98 else 'Relay operation failed'}
        print(json.dumps(result))
    else:raise SystemExit('Invalid invocation')
