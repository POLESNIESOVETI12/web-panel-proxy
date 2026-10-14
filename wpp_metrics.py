#!/usr/bin/env python3
"""Reconcile only the UFW rules owned by WEB PANEL PROXY.

The module deliberately never enables/disables UFW and never removes an
administrator-created rule.  It works from /etc/ufw/ufw.conf instead of the
localized human-readable output of ``ufw status``.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

STATE = Path("/etc/web-proxy-panel/ufw-owned.json")
UFW_CONFIG = Path("/etc/ufw/ufw.conf")
COMMENT = "WEB PANEL PROXY"


class FirewallError(RuntimeError):
    pass


def _run(*args):
    try:
        return subprocess.run(args, text=True, capture_output=True, timeout=25, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise FirewallError(str(exc)) from exc


def enabled():
    if not shutil.which("ufw"):
        return False
    try:
        value = UFW_CONFIG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return re.search(r"(?m)^\s*ENABLED\s*=\s*yes\s*$", value, re.I) is not None


def _load():
    try:
        value = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Import ownership records used by WPP 2.4.0.  This migration prevents
        # stale dynamic ports after the corresponding client is deleted.
        rules, routes = [], []
        for name, proto in (("mtproto-ufw-owned", "tcp"), ("hysteria-ufw-owned", "udp"),
                            ("awg-ufw-owned", "udp")):
            try:
                raw = (STATE.parent / name).read_text(encoding="ascii")
            except OSError:
                continue
            rules.extend(f"{port}/{proto}" for port in re.findall(r"\b\d{1,5}\b", raw)
                         if 1 <= int(port) <= 65535)
        try:
            raw = (STATE.parent / "awg-route-ufw-owned").read_text(encoding="ascii")
            routes = [line.split() for line in raw.splitlines() if len(line.split()) == 2]
        except OSError:
            pass
        return {"rules": rules, "routes": routes}
    return {
        "rules": [x for x in value.get("rules", []) if isinstance(x, str)],
        "routes": [x for x in value.get("routes", []) if isinstance(x, list) and len(x) == 2],
    }


def _save(value):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=STATE.name + ".", dir=str(STATE.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        os.replace(name, STATE)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _existing():
    result = _run("ufw", "show", "added")
    if result.returncode:
        return ""
    return result.stdout.lower()


def _present(spec, added):
    port, proto = spec.split("/", 1)
    return re.search(r"(?m)^ufw\s+allow\s+(?:in\s+)?%s/%s(?:\s|$)" % (re.escape(port), proto), added) is not None


def _command_ok(result, description):
    if result.returncode:
        detail = (result.stderr or result.stdout or description).strip()[-1200:]
        raise FirewallError(detail)


def reconcile(*, tcp=(), udp=(), routes=()):
    """Make WPP-owned UFW rules equal to the requested rules.

    tcp/udp are port iterables. routes contains (input_interface,
    output_interface) tuples used by AWG forwarding.
    """
    desired_rules = {f"{int(port)}/tcp" for port in tcp} | {f"{int(port)}/udp" for port in udp}
    desired_routes = {(str(a), str(b)) for a, b in routes if a and b}
    if any(not 1 <= int(item.split("/", 1)[0]) <= 65535 for item in desired_rules):
        raise FirewallError("Invalid firewall port")
    previous = _load()
    owned_rules = set(previous["rules"])
    owned_routes = {tuple(item) for item in previous["routes"]}
    if not enabled():
        # Keep ownership state: if UFW is enabled later, the next reconciliation
        # restores missing WPP rules without taking ownership of admin rules.
        _save({"rules": sorted(owned_rules & desired_rules),
               "routes": [list(x) for x in sorted(owned_routes & desired_routes)]})
        return {"enabled": False, "rules": len(desired_rules), "routes": len(desired_routes)}

    for spec in sorted(owned_rules - desired_rules):
        _run("ufw", "--force", "delete", "allow", spec)
        owned_rules.discard(spec)
    for iface, output in sorted(owned_routes - desired_routes):
        _run("ufw", "--force", "route", "delete", "allow", "in", "on", iface, "out", "on", output)
        owned_routes.discard((iface, output))

    added = _existing()
    for spec in sorted(desired_rules):
        if _present(spec, added):
            continue
        result = _run("ufw", "allow", spec, "comment", COMMENT)
        _command_ok(result, "Could not add UFW rule " + spec)
        owned_rules.add(spec)
    for iface, output in sorted(desired_routes):
        if (iface, output) in owned_routes:
            continue
        result = _run("ufw", "route", "allow", "in", "on", iface, "out", "on", output,
                      "comment", COMMENT + " AWG")
        _command_ok(result, "Could not add UFW route")
        owned_routes.add((iface, output))
    _save({"rules": sorted(owned_rules & desired_rules),
           "routes": [list(x) for x in sorted(owned_routes & desired_routes)]})
    return {"enabled": True, "rules": len(desired_rules), "routes": len(desired_routes)}


def purge():
    """Remove every UFW entry previously created by WPP, even while inactive."""
    previous = _load()
    if shutil.which("ufw"):
        for spec in previous["rules"]:
            _run("ufw", "--force", "delete", "allow", spec)
        for iface, output in previous["routes"]:
            _run("ufw", "--force", "route", "delete", "allow", "in", "on", iface, "out", "on", output)
    try:
        STATE.unlink()
    except FileNotFoundError:
        pass
