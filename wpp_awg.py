#!/usr/bin/env python3
"""Independent AmneziaWG 2.0/3.1 profiles for WEB PANEL PROXY.

Every WPP profile owns a userspace interface, UDP port, address block, key set
and obfuscation fingerprint.  Keeping device-level AWG parameters per profile
avoids the shared-fingerprint limitation of a conventional multi-peer server.
"""

import ipaddress
import json
import os
import re
import secrets
import subprocess
import tempfile

AWG = "/usr/local/bin/awg"
CONFIG_DIR = "/etc/web-proxy-panel/awg"
SERVICE_TEMPLATE = "web-panel-proxy-awg@{}.service"
# AWG 3.1 can add IPv6/UDP + WireGuard transport framing, S4 padding and up to
# 64 bytes of content padding. 1100 keeps the complete outer datagram below the
# IPv6 minimum path MTU (1280) even in the worst generated profile. AWG 2.0 has
# no 3.1 content padding and can retain the less conservative value.
AWG20_MTU = 1280
AWG31_MTU = 1100
PORT_FIRST = 52000
PORT_LAST = 52999
PROTOCOLS = ("awg20", "awg31")
PROFILE_SCHEMA = 2


def _mtu(user):
    return AWG31_MTU if user.get("protocol") == "awg31" else AWG20_MTU


def _run(*args, input_text=None, check=True, timeout=30):
    proc = subprocess.run(args, input=input_text, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=timeout)
    if check and proc.returncode:
        raise RuntimeError((proc.stderr or proc.stdout or "AWG command failed").strip())
    return proc


def _key(command="genkey"):
    value = _run(AWG, command).stdout.strip()
    if len(value) != 44:
        raise RuntimeError("AWG generated an invalid key")
    return value


def _public(private_key):
    value = _run(AWG, "pubkey", input_text=private_key + "\n").stdout.strip()
    if len(value) != 44:
        raise RuntimeError("AWG generated an invalid public key")
    return value


def _interface(uid):
    if not re.fullmatch(r"[a-f0-9]{16}", str(uid)):
        raise RuntimeError("Invalid AWG profile id")
    return "wa" + str(uid)[:11]  # Linux IFNAMSIZ: at most 15 bytes.


def _occupied_udp_ports():
    proc = _run("ss", "-Hlun", check=False, timeout=10)
    return {int(m.group(1)) for m in re.finditer(r"[:.](\d{1,5})\s", proc.stdout or "")}


def _allocate_port(users):
    used = {int(u.get("backend_port", 0)) for u in users if u.get("protocol") in PROTOCOLS}
    occupied = _occupied_udp_ports()
    for port in range(PORT_FIRST, PORT_LAST + 1):
        if port not in used and port not in occupied:
            return port
    raise RuntimeError("No free UDP port is available for AWG")


def _allocate_network(users):
    used = {str(u.get("awg_network", "")) for u in users if u.get("protocol") in PROTOCOLS}
    # One /30 per profile: .1 server and .2 client. This intentionally has no
    # dependency on the profile protocol, so AWG 2.0 and 3.1 cannot collide.
    for second in range(80, 84):
        for third in range(1, 255):
            network = "10.%d.%d.0/30" % (second, third)
            if network not in used:
                return network
    raise RuntimeError("No free AWG address block is available")


def _parameters(protocol):
    rng = secrets.SystemRandom()
    # Match the constraints used by 3x-ui 3.8.5's GenerateObfuscation31.
    # Each fixed H value gets its own band: H ranges must never overlap and
    # values 1-4 are the recognisable vanilla WireGuard message types.
    h_low = 5
    h_band = (2147483647 - h_low + 1) // 4
    headers = [rng.randint(h_low + i * h_band,
                           h_low + (i + 1) * h_band - 1) for i in range(4)]
    jmin = rng.randint(40, 89)
    s1 = rng.randint(15, 150)
    s2 = rng.randint(15, 150)
    while s1 + 56 == s2:
        s2 = rng.randint(15, 150)
    values = {
        "Jc": rng.randint(3, 6),
        "Jmin": jmin,
        "Jmax": jmin + rng.randint(50, 250),
        "S1": s1,
        "S2": s2,
        "S3": rng.randint(12, 55),
        "S4": rng.randint(12, 27),
        "H1": headers[0], "H2": headers[1],
        "H3": headers[2], "H4": headers[3],
    }
    if protocol == "awg31":
        cp_low = rng.randint(8, 24)
        rekey_low = rng.randint(100, 120)
        rekey_high = rekey_low + rng.randint(10, 40)
        reject_low = rekey_high + rng.randint(30, 60)
        timeout_low = rng.randint(3, 6)
        keepalive_low = rng.randint(8, 12)
        attempts_low = rng.randint(15, 25)
        values.update({
            "I1": "<r %d>" % rng.randint(32, 256),
            "HeaderProtectionKey": _key(),
            "ContentPaddingAddition": "%d-%d" % (cp_low, cp_low + rng.randint(8, 40)),
            "RekeyAfterTime": "%d-%d" % (rekey_low, rekey_high),
            "RekeyTimeout": "%d-%d" % (timeout_low, timeout_low + rng.randint(1, 4)),
            "RejectAfterTime": "%d-%d" % (reject_low, reject_low + rng.randint(30, 90)),
            "KeepaliveTimeout": "%d-%d" % (keepalive_low, keepalive_low + rng.randint(2, 8)),
            "MaxHandshakeAttempts": "%d-%d" % (attempts_low, attempts_low + rng.randint(5, 25)),
            "RandomTrailers": "on",
            "DisableCookies": "on",
        })
    return values


def new_user(protocol, users, uid):
    if protocol not in PROTOCOLS:
        raise RuntimeError("Unsupported AWG protocol")
    network = ipaddress.ip_network(_allocate_network(users))
    client_private = _key()
    server_private = _key()
    existing_fingerprints = {
        json.dumps(u.get("awg_parameters", {}), sort_keys=True)
        for u in users if u.get("protocol") in PROTOCOLS
    }
    parameters = _parameters(protocol)
    while json.dumps(parameters, sort_keys=True) in existing_fingerprints:
        parameters = _parameters(protocol)
    return {
        "secret": client_private,
        "public_key": _public(client_private),
        "preshared_key": _key("genpsk"),
        "server_private_key": server_private,
        "server_public_key": _public(server_private),
        "backend_port": _allocate_port(users),
        "awg_network": str(network),
        "address": str(list(network.hosts())[1]) + "/32",
        "server_address": str(list(network.hosts())[0]) + "/30",
        "awg_interface": _interface(uid),
        "awg_parameters": parameters,
        "awg_schema": PROFILE_SCHEMA,
    }


def upgrade_users(users):
    """Migrate the withdrawn shared-interface preview to independent profiles."""
    required = ("secret", "public_key", "preshared_key", "server_private_key",
                "server_public_key", "backend_port", "awg_network", "address",
                "server_address", "awg_interface", "awg_parameters")
    changed = False
    for user in users:
        if user.get("protocol") not in PROTOCOLS:
            continue
        complete = all(user.get(key) for key in required)
        try:
            valid_port = PORT_FIRST <= int(user.get("backend_port", 0)) <= PORT_LAST
            valid_interface = user.get("awg_interface") == _interface(user.get("id", ""))
        except (TypeError, ValueError, RuntimeError):
            valid_port = valid_interface = False
        if complete and valid_port and valid_interface:
            if user.get("awg_schema") != PROFILE_SCHEMA:
                other = {json.dumps(u.get("awg_parameters", {}), sort_keys=True)
                         for u in users if u is not user and u.get("protocol") in PROTOCOLS}
                parameters = _parameters(user["protocol"])
                while json.dumps(parameters, sort_keys=True) in other:
                    parameters = _parameters(user["protocol"])
                user["awg_parameters"] = parameters
                user["awg_schema"] = PROFILE_SCHEMA
                changed = True
            continue
        fresh = new_user(user["protocol"], users, user["id"])
        for key in list(user):
            if key.startswith("awg_") or key in ("secret", "public_key", "preshared_key",
                    "server_private_key", "server_public_key", "backend_port", "address", "server_address"):
                user.pop(key, None)
        user.update(fresh)
        changed = True
    return changed


def _parameter_lines(parameters):
    order = ("Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4", "H1", "H2", "H3", "H4",
             "I1", "I2", "I3", "I4", "I5",
             "HeaderProtectionKey", "ContentPaddingAddition", "RekeyAfterTime", "RekeyTimeout",
             "RejectAfterTime", "KeepaliveTimeout", "MaxHandshakeAttempts", "RandomTrailers",
             "DisableCookies")
    return ["%s = %s" % (key, parameters[key]) for key in order if key in parameters]


def _server_config(user):
    client_ip = str(ipaddress.ip_interface(user["address"]).ip) + "/32"
    lines = ["[Interface]", "PrivateKey = " + user["server_private_key"],
             "ListenPort = " + str(int(user["backend_port"]))]
    lines.extend(_parameter_lines(user["awg_parameters"]))
    lines.extend(["", "[Peer]", "PublicKey = " + user["public_key"],
                  "PresharedKey = " + user["preshared_key"], "AllowedIPs = " + client_ip, ""])
    return "\n".join(lines)


def _atomic_text(path, value):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    old = None
    try:
        with open(path, encoding="utf-8") as handle:
            old = handle.read()
    except FileNotFoundError:
        pass
    if old == value:
        return False
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value); handle.flush(); os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return True


def _validate_user(user):
    protocol = user.get("protocol")
    if protocol not in PROTOCOLS:
        raise RuntimeError("Unsupported AWG protocol")
    if not re.fullmatch(r"[a-f0-9]{16}", str(user.get("id", ""))):
        raise RuntimeError("Invalid AWG profile id")
    for key in ("secret", "public_key", "preshared_key", "server_private_key", "server_public_key",
                "address", "server_address", "awg_network", "awg_interface", "awg_parameters"):
        if not user.get(key):
            raise RuntimeError("AWG profile is incomplete: " + key)
    if user["awg_interface"] != _interface(user["id"]):
        raise RuntimeError("Invalid AWG interface name")
    if user.get("awg_schema") != PROFILE_SCHEMA:
        raise RuntimeError("Outdated AWG profile schema")
    port = int(user.get("backend_port", 0))
    if not PORT_FIRST <= port <= PORT_LAST:
        raise RuntimeError("Invalid AWG UDP port")
    parameters = user["awg_parameters"]
    try:
        if not 3 <= int(parameters["Jc"]) <= 6:
            raise ValueError
        if not 40 <= int(parameters["Jmin"]) <= int(parameters["Jmax"]) <= 339:
            raise ValueError
        if not 12 <= int(parameters["S3"]) <= 55 or not 12 <= int(parameters["S4"]) <= 27:
            raise ValueError
        if int(parameters["S1"]) + 56 == int(parameters["S2"]):
            raise ValueError
        if len({int(parameters[k]) for k in ("H1", "H2", "H3", "H4")}) != 4:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("Invalid AWG obfuscation parameters")
    if protocol == "awg31":
        if parameters.get("RandomTrailers") != "on" or parameters.get("DisableCookies") != "on":
            raise RuntimeError("Incomplete AWG 3.1 protection settings")
        if not str(parameters.get("I1", "")).startswith("<r "):
            raise RuntimeError("Invalid AWG 3.1 signature packet")


def service_for(user):
    return SERVICE_TEMPLATE.format(user.get("id", "")) if user.get("protocol") in PROTOCOLS else ""


def sync(users):
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    selected = {u["id"]: u for u in users if u.get("protocol") in PROTOCOLS and u.get("enabled", True)}
    changed = set()
    for uid, user in selected.items():
        _validate_user(user)
        config = os.path.join(CONFIG_DIR, uid + ".conf")
        meta = os.path.join(CONFIG_DIR, uid + ".json")
        changed_config = _atomic_text(config, _server_config(user))
        changed_meta = _atomic_text(meta, json.dumps({"interface": user["awg_interface"],
            "address": user["server_address"], "mtu": _mtu(user)}, sort_keys=True) + "\n")
        if changed_config or changed_meta:
            changed.add(uid)
    for name in os.listdir(CONFIG_DIR):
        match = re.fullmatch(r"([a-f0-9]{16})\.(?:conf|json)", name)
        if not match or match.group(1) in selected:
            continue
        uid = match.group(1)
        _run("systemctl", "disable", "--now", SERVICE_TEMPLATE.format(uid), check=False)
        for suffix in (".conf", ".json"):
            try: os.unlink(os.path.join(CONFIG_DIR, uid + suffix))
            except FileNotFoundError: pass
    _run("systemctl", "daemon-reload", check=True)
    for uid, user in selected.items():
        unit = SERVICE_TEMPLATE.format(uid)
        active = _run("systemctl", "is-active", "--quiet", unit, check=False).returncode == 0
        _run("systemctl", "enable", unit, check=True)
        _run("systemctl", "restart" if uid in changed or not active else "start", unit, check=True)
        if _run("systemctl", "is-active", "--quiet", unit, check=False).returncode:
            status = _run("systemctl", "status", unit, "--no-pager", "--full", check=False)
            raise RuntimeError("AWG profile failed: " + (status.stdout or status.stderr)[-2500:])
        _run(AWG, "show", user["awg_interface"], check=True)
        link = _run("/usr/sbin/ip", "-o", "link", "show", "dev", user["awg_interface"], check=True)
        if not re.search(r"\bmtu\s+%d\b" % _mtu(user), link.stdout or ""):
            raise RuntimeError("AWG interface started with an unsafe MTU")


def client_config(user, endpoint, name="AWG"):
    _validate_user(user)
    lines = ["# " + str(name), "[Interface]", "PrivateKey = " + user["secret"],
             "Address = " + user["address"], "DNS = 1.1.1.1, 1.0.0.1", "MTU = " + str(_mtu(user))]
    lines.extend(_parameter_lines(user["awg_parameters"]))
    lines.extend(["", "[Peer]", "PublicKey = " + user["server_public_key"],
                  "PresharedKey = " + user["preshared_key"],
                  "Endpoint = %s:%d" % (endpoint, int(user["backend_port"])),
                  "AllowedIPs = 0.0.0.0/0", "PersistentKeepalive = 25", ""])
    return "\n".join(lines)


def traffic(users):
    by_key = {u.get("public_key"): u.get("id") for u in users
              if u.get("protocol") in PROTOCOLS and u.get("public_key")}
    result = {}
    proc = _run(AWG, "show", "all", "dump", check=False, timeout=10)
    if proc.returncode:
        return result
    for line in proc.stdout.splitlines():
        columns = line.split("\t")
        if len(columns) < 9 or columns[1] not in by_key:
            continue
        try:
            result[by_key[columns[1]]] = {"up": int(columns[6]), "down": int(columns[7])}
        except ValueError:
            continue
    return result
