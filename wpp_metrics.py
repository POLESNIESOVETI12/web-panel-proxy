"""Independent VPS sampler; proxy counters are read without running Xray/nft."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

STATE = Path('/var/lib/tproxy-panel/metrics.json')
TRAFFIC = Path('/var/lib/tproxy-panel/traffic.json')
ERROR = Path('/var/lib/tproxy-panel/metrics-error.json')
PROC = Path('/proc')
SERVICES = {'xray': 'web-panel-proxy-xray.service', 'panel': 'tproxy-panel.service',
            'caddy': 'caddy.service', 'relay': 'tproxy-server.service'}
OPENFLUX_PROFILES = Path('/etc/web-proxy-panel/openflux/profiles')
MTPROXY_ECONOMY = Path('/etc/web-proxy-panel/mtproxy-economy')


def read_state(path=None):
    path = STATE if path is None else path
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=True, separators=(',', ':'))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def proc_text(name):
    try:
        return (PROC / name).read_text(encoding='ascii').strip()
    except OSError:
        return ''


def _service_fields_many(units):
    """Read every service in one systemctl process instead of one fork per unit."""
    units = list(dict.fromkeys(str(unit) for unit in units if unit))
    if not units:
        return {}
    run = subprocess.run([
        'systemctl', 'show', '--no-pager', '-p', 'Id', '-p', 'ActiveState',
        '-p', 'MemoryCurrent', '-p', 'TasksCurrent',
        '-p', 'ExecMainStartTimestampMonotonic', *units,
    ], capture_output=True, text=True, timeout=5,
        env={**os.environ, 'LC_ALL':'C', 'SYSTEMD_COLORS':'0'})
    result = {}
    for block in re.split(r'\n\s*\n', run.stdout.strip()):
        fields = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
        unit = fields.get('Id', '')
        if not unit:
            continue
        def numeric(key):
            value = fields.get(key, '')
            return int(value) if value.isdigit() and int(value) < 2**63 else None
        result[unit] = {
            'state': fields.get('ActiveState', 'unknown'),
            'memory': numeric('MemoryCurrent'), 'tasks': numeric('TasksCurrent'),
            'start_us': numeric('ExecMainStartTimestampMonotonic'),
        }
    return result


def _openflux_units():
    units = ['web-panel-proxy-openflux.service']
    try:
        profile_ids = sorted(entry.name for entry in OPENFLUX_PROFILES.iterdir()
                             if entry.is_dir() and re.fullmatch(r'[0-9a-f]{16}', entry.name))
    except OSError:
        profile_ids = []
    units.extend('web-panel-proxy-openflux-' + profile_id + '.service' for profile_id in profile_ids)
    return units


def _openflux_snapshot(units, fields):
    states = [fields.get(unit, {'state': 'unknown'}) for unit in units]
    active = [value for value in states if value.get('state') == 'active']
    selected = active or states
    numbers = lambda key: [value[key] for value in selected if value.get(key) is not None]
    starts = numbers('start_us')
    memories = numbers('memory')
    tasks = numbers('tasks')
    return {'state': 'active' if active else ('inactive' if states else 'unknown'),
            'memory': sum(memories) if memories else None,
            'tasks': sum(tasks) if tasks else None,
            'start_us': min(starts) if starts else None,
            'active_profiles': len(active), 'profiles': len(units)}


def service_snapshot():
    openflux_units = _openflux_units()
    try:
        fields = _service_fields_many([*SERVICES.values(), *openflux_units])
    except (OSError, subprocess.TimeoutExpired, ValueError):
        fields = {}
    result = {name: fields.get(unit, {'state': 'unknown'}) for name, unit in SERVICES.items()}
    if MTPROXY_ECONOMY.exists() and result.get('relay', {}).get('state') != 'active':
        result['relay'] = {**result.get('relay', {}), 'state': 'economy'}
    try:
        result['openflux'] = _openflux_snapshot(openflux_units, fields)
    except (OSError, ValueError):
        result['openflux'] = {'state': 'unknown'}
    return result


def make_sample(traffic, previous, now=None):
    now = int(time.time()) if now is None else now
    mem = {}
    for line in proc_text('meminfo').splitlines():
        parts = line.split()
        if len(parts) > 1 and parts[1].isdigit(): mem[parts[0].rstrip(':')] = int(parts[1]) * 1024
    cpu = proc_text('stat').splitlines()
    raw = [int(x) for x in cpu[0].split()[1:9]] if cpu and cpu[0].startswith('cpu ') else []
    elapsed_total = sum(raw) if raw else 0
    steal = raw[7] if len(raw) > 7 else 0
    # Linux accounts time taken by the host hypervisor as ``steal``. It is
    # useful diagnostics, but it is not work performed by this VPS. Keep it
    # out of the main CPU gauge and expose it separately.
    total = elapsed_total - steal
    idle = sum(raw[3:5]) if raw else 0  # idle + iowait, excluding guest double-counting
    before = previous.get('cpu_ticks', [0, 0])
    delta = total - before[0]
    boot = proc_text('sys/kernel/random/boot_id')
    same_boot = (bool(boot) and boot == previous.get('boot_id')
                 and previous.get('cpu_ticks_version') == 2)
    percent = max(0, min(100, 100 * (1 - (idle - before[1]) / delta))) if same_boot and delta > 0 else None
    elapsed_before = int(previous.get('cpu_elapsed_ticks', 0) or 0)
    steal_before = int(previous.get('cpu_steal_ticks', 0) or 0)
    elapsed_delta = elapsed_total - elapsed_before
    steal_percent = (max(0, min(100, 100 * (steal - steal_before) / elapsed_delta))
                     if same_boot and elapsed_delta > 0 else None)
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
    service_checked = int(previous.get('services_checked', 0) or 0)
    services = previous.get('services') if isinstance(previous.get('services'), dict) else None
    # Service state changes slowly during normal operation. Reusing the last
    # snapshot avoids spawning systemctl on every metrics tick while keeping
    # the dashboard state reasonably fresh.
    if services is None or not (0 <= now - service_checked < 180):
        services = service_snapshot()
        service_checked = now
    return {'time': now, 'boot_id': boot, 'cpu_ticks': [total, idle], 'cpu_ticks_version': 2,
            'cpu_elapsed_ticks': elapsed_total, 'cpu_steal_ticks': steal,
            'cpu': percent, 'cpu_steal': steal_percent,
            'cores': os.cpu_count() or 1, 'ram_total': mem.get('MemTotal'),
            'ram_used': max(0, mem['MemTotal'] - mem.get('MemAvailable', mem.get('MemFree', 0))) if 'MemTotal' in mem else None,
            'swap_total': mem.get('SwapTotal'), 'swap_used': max(0, mem.get('SwapTotal', 0) - mem.get('SwapFree', 0)),
            'disk_total': disk.total if disk else None, 'disk_used': disk.used if disk else None,
            'uptime': uptime, 'load': load, 'up': up, 'down': down,
            'traffic_time': observed, 'traffic_fresh': traffic_fresh,
            'up_rate': (up - previous['up']) / traffic_dt if rates_valid else None,
            'down_rate': (down - previous['down']) / traffic_dt if rates_valid else None,
            'services': services, 'services_checked': service_checked}


def sample_metrics(traffic, force=False):
    import fcntl
    STATE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE.parent / 'metrics.lock').open('a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        data = read_state()
        now = int(time.time())
        if not force and 0 <= now - data.get('latest', {}).get('time', 0) < 45: return
        previous = data.get('latest', {})
        point = make_sample(traffic, previous, now)
        # First install: obtain a real CPU delta, not an invented zero.
        if point.get('cpu') is None and point.get('cpu_ticks',[0])[0] > 0:
            ticks = point['cpu_ticks']
            time.sleep(0.15)
            raw = proc_text('stat').splitlines()
            values = [int(x) for x in raw[0].split()[1:9]] if raw and raw[0].startswith('cpu ') else []
            elapsed_total=sum(values)
            steal=values[7] if len(values)>7 else 0
            total=elapsed_total-steal
            delta=total-ticks[0]
            elapsed_delta=elapsed_total-point.get('cpu_elapsed_ticks',elapsed_total)
            if delta > 0: point['cpu'] = max(0,min(100,100*(1-(sum(values[3:5])-ticks[1])/delta)))
            if elapsed_delta > 0:
                point['cpu_steal']=max(0,min(100,100*(steal-point.get('cpu_steal_ticks',steal))/elapsed_delta))
        history = [h for h in data.get('history', []) if now - 86400 <= h.get('time', 0) < now]
        history.append({k: point[k] for k in ('time', 'up_rate', 'down_rate', 'cpu')})
        # One-minute sampling keeps a full day plus one point without growing
        # the state file or waking Python thousands of times per day.
        atomic_json(STATE, {'latest': point, 'history': history[-1441:]})


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
