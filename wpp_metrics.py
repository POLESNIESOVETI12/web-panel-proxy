"""Independent VPS sampler; proxy counters are read without running Xray/nft."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import traceback

STATE = Path('/var/lib/tproxy-panel/metrics.json')
TRAFFIC = Path('/var/lib/tproxy-panel/traffic.json')
ERROR = Path('/var/lib/tproxy-panel/metrics-error.json')
PROC = Path('/proc')
SERVICES = {'xray': 'web-panel-proxy-xray.service', 'panel': 'tproxy-panel.service',
            'caddy': 'caddy.service', 'relay': 'tproxy-server.service'}


def read_state(path=None):
    path = STATE if path is None else path
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix('.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=True, separators=(',', ':'))
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def proc_text(name):
    try:
        return (PROC / name).read_text(encoding='ascii').strip()
    except OSError:
        return ''


def service_snapshot():
    result = {}
    for name, unit in SERVICES.items():
        try:
            run = subprocess.run(['systemctl', 'show', unit, '--no-pager',
                '-p', 'ActiveState', '-p', 'MemoryCurrent', '-p', 'TasksCurrent',
                '-p', 'ExecMainStartTimestampMonotonic'], capture_output=True, text=True, timeout=3,
                env={**os.environ, 'LC_ALL':'C', 'SYSTEMD_COLORS':'0'})
            fields = dict(line.split('=', 1) for line in run.stdout.splitlines() if '=' in line)
            numeric = lambda key: int(fields[key]) if fields.get(key, '').isdigit() and int(fields[key]) < 2**63 else None
            start = numeric('ExecMainStartTimestampMonotonic')
            result[name] = {'state': fields.get('ActiveState', 'unknown'), 'memory': numeric('MemoryCurrent'),
                            'tasks': numeric('TasksCurrent'), 'start_us': start}
        except (OSError, subprocess.TimeoutExpired, ValueError):
            result[name] = {'state': 'unknown'}
    return result


def make_sample(traffic, previous, now=None):
    now = int(time.time()) if now is None else now
    mem = {}
    for line in proc_text('meminfo').splitlines():
        parts = line.split()
        if len(parts) > 1 and parts[1].isdigit(): mem[parts[0].rstrip(':')] = int(parts[1]) * 1024
    cpu = proc_text('stat').splitlines()
    raw = [int(x) for x in cpu[0].split()[1:9]] if cpu and cpu[0].startswith('cpu ') else []
    total = sum(raw) if raw else 0
    idle = sum(raw[3:5]) if raw else 0  # idle + iowait, excluding guest double-counting
    before = previous.get('cpu_ticks', [0, 0])
    delta = total - before[0]
    boot = proc_text('sys/kernel/random/boot_id')
    same_boot = bool(boot) and boot == previous.get('boot_id')
    percent = max(0, min(100, 100 * (1 - (idle - before[1]) / delta))) if same_boot and delta > 0 else None
    up = sum(max(0, int(v.get('up', 0))) for v in traffic.values() if isinstance(v, dict))
    down = sum(max(0, int(v.get('down', 0))) for v in traffic.values() if isinstance(v, dict))
    dt = now - previous.get('time', now)
    # Gaps/reboots/resets are not interpolated into fabricated measurements.
    observed = max((v.get('updated_at',0) for v in traffic.values() if isinstance(v,dict)), default=0)
    traffic_fresh = 0 <= now-observed <= 90 and observed > 0
    old_observed = previous.get('traffic_time',0)
    traffic_dt = observed-old_observed
    rates_valid = (same_boot and traffic_fresh and old_observed > 0 and 0 < traffic_dt <= 120
                   and 10 <= dt <= 120 and up >= previous.get('up',up) and down >= previous.get('down',down))
    try: disk = shutil.disk_usage('/')
    except OSError: disk = None
    try: uptime = float(proc_text('uptime').split()[0])
    except (ValueError, IndexError): uptime = None
    try: load = [float(x) for x in proc_text('loadavg').split()[:3]]
    except ValueError: load = []
    return {'time': now, 'boot_id': boot, 'cpu_ticks': [total, idle], 'cpu': percent,
            'cores': os.cpu_count() or 1, 'ram_total': mem.get('MemTotal'),
            'ram_used': max(0, mem['MemTotal'] - mem.get('MemAvailable', mem.get('MemFree', 0))) if 'MemTotal' in mem else None,
            'swap_total': mem.get('SwapTotal'), 'swap_used': max(0, mem.get('SwapTotal', 0) - mem.get('SwapFree', 0)),
            'disk_total': disk.total if disk else None, 'disk_used': disk.used if disk else None,
            'uptime': uptime, 'load': load, 'up': up, 'down': down,
            'traffic_time': observed, 'traffic_fresh': traffic_fresh,
            'up_rate': (up - previous['up']) / traffic_dt if rates_valid else None,
            'down_rate': (down - previous['down']) / traffic_dt if rates_valid else None,
            'services': service_snapshot()}


def sample_metrics(traffic, force=False):
    import fcntl
    STATE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE.parent / 'metrics.lock').open('a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        data = read_state()
        now = int(time.time())
        if not force and 0 <= now - data.get('latest', {}).get('time', 0) < 25: return
        previous = data.get('latest', {})
        point = make_sample(traffic, previous, now)
        # First install: obtain a real CPU delta, not an invented zero.
        if point.get('cpu') is None and point.get('cpu_ticks',[0])[0] > 0:
            ticks = point['cpu_ticks']
            time.sleep(0.15)
            raw = proc_text('stat').splitlines()
            values = [int(x) for x in raw[0].split()[1:9]] if raw and raw[0].startswith('cpu ') else []
            delta = sum(values)-ticks[0]
            if delta > 0: point['cpu'] = max(0,min(100,100*(1-(sum(values[3:5])-ticks[1])/delta)))
        history = [h for h in data.get('history', []) if now - 86400 <= h.get('time', 0) < now]
        history.append({k: point[k] for k in ('time', 'up_rate', 'down_rate', 'cpu')})
        # Ten-second sampling keeps a full day plus one point.
        atomic_json(STATE, {'latest': point, 'history': history[-8641:]})


def collect_once():
    try:
        sample_metrics(read_state(TRAFFIC), force=True)
        value = read_state().get('latest',{})
        if not value.get('ram_total') or not value.get('cpu_ticks',[0])[0]:
            raise RuntimeError('Required Linux /proc metrics are unavailable')
        if ERROR.exists(): ERROR.unlink()
    except Exception as exc:
        # No secrets or raw command output in the HTTP response.
        atomic_json(ERROR, {'time':int(time.time()),'kind':type(exc).__name__})
        raise


def dashboard_data(hours=1):
    data = read_state()
    cutoff = int(time.time()) - hours * 3600
    return {'latest': data.get('latest', {}), 'history': [p for p in data.get('history', []) if p.get('time', 0) >= cutoff],
            'collector_error': read_state(ERROR)}


if __name__ == '__main__':
    if sys.argv[1:] != ['collect']: raise SystemExit('Usage: wpp_metrics.py collect')
    try: collect_once()
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
