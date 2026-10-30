"""Regressions for creating Mieru users after TLS maintenance.

Runs the real embedded manager add/apply flow and protocol sync with temporary
files. Only Linux service operations and ownership are simulated on Windows.
"""
import ast
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import wpp_connections as connections
import wpp_extra_protocols as extra

INSTALLER = (ROOT / "install-panel.sh").read_text(encoding="utf-8")


def embedded(marker):
    return INSTALLER.split(marker + "\n", 1)[1].split("\nPY\n", 1)[0]


def functions_from(source, names, namespace):
    nodes = [node for node in ast.parse(source).body
             if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(nodes) == len(names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "embedded.py", "exec"), namespace)


class MieruCreation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "extra-protocols"
        self.data.mkdir()
        # Exactly the state reported by namei on the affected VPS.
        self.dir_owner = ("root", "caddy")
        self.dir_mode = 0o750
        self.file_owners = {}
        self.file_modes = {}
        self.running = False
        self.calls = []
        real_atomic = extra._atomic

        def chown(path, user=None, group=None):
            if Path(path) == self.data:
                self.dir_owner = (user, group)
            else:
                self.file_owners[Path(path)] = (user, group)

        real_chmod = os.chmod

        def chmod(path, mode):
            if Path(path) == self.data:
                self.dir_mode = mode
            real_chmod(path, mode)

        def atomic(path, value, mode=0o600):
            real_atomic(path, value, mode)
            self.file_modes[Path(path)] = mode

        def run(*args, **kwargs):
            self.calls.append(args)
            if args[:2] == ("systemctl", "restart") and args[-1] == extra.MIERU_SERVICE:
                can_traverse = bool(self.dir_mode & 0o001) or (
                    self.dir_owner[1] == "mita" and bool(self.dir_mode & 0o010))
                readable = (self.file_owners.get(extra.MIERU_CONFIG) == ("root", "mita")
                            and self.file_modes.get(extra.MIERU_CONFIG) == 0o640)
                if not can_traverse or not readable:
                    raise PermissionError("permission denied: mieru-server.json")
                self.running = True
            if args == ("ss", "-lntuH"):
                return SimpleNamespace(returncode=0, stdout="tcp LISTEN 0 128 *:54000 *:*\n", stderr="")
            if args[-1:] == ("status",):
                return SimpleNamespace(returncode=0, stdout='mita server status is "RUNNING"', stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        for name, value in (("DATA_DIR", self.data), ("MIERU_CONFIG", self.data / "mieru-server.json"),
                            ("NAIVE_CONFIG", self.data / "Caddyfile.naive"), ("_run", run),
                            ("_atomic", atomic)):
            mock = patch.object(extra, name, value)
            mock.start(); self.addCleanup(mock.stop)
        for target, replacement in ((shutil, chown), (os, chmod)):
            mock = patch.object(target, "chown" if target is shutil else "chmod", replacement)
            mock.start(); self.addCleanup(mock.stop)

        namespace = dict(copy=copy, json=json, os=os, re=re, secrets=secrets, time=time,
                         shutil=shutil, wpp_extra_protocols=extra, wpp_connections=connections,
                         wpp_awg=SimpleNamespace(PROTOCOLS=("awg20", "awg31")),
                         MAX_USERS=32, HYSTERIA_PORT=8443,
                         run=run, port_in_use=lambda port: False)
        for name, filename in (("USERS", "users.json"), ("PROFILES", "profiles.json"),
                               ("CADDYFILE", "Caddyfile"), ("XRAY_CONFIG", "xray.json"),
                               ("CONNECTIONS", "connections.json")):
            namespace[name] = str(self.root / filename)
        namespace.update(XRAY_CERT="unused.crt", XRAY_KEY="unused.key", XRAY_SERVICE="xray.service")
        for name in ("remove_old_units", "wake_primary_proxy_if_needed", "write_unit", "sync_firewall"):
            namespace[name] = lambda *args: None
        Path(namespace["PROFILES"]).write_text("{}", encoding="utf-8")
        Path(namespace["CADDYFILE"]).write_text(":443 {}", encoding="utf-8")
        functions_from(embedded('cat > "$MANAGER" <<\'PY\''),
                       {"load", "save", "add", "apply"}, namespace)
        namespace["save"]({"users": [], "subscriptions": [], "traffic": {}})
        self.manager = namespace

    def create(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.manager["add"]("mieru", "Телефон", device_count=1,
                                options={"connection_id": "mieru-primary"})
        return json.loads(output.getvalue())

    def test_original_permissions_reproduce_failed_creation_and_rollback(self):
        with patch.object(extra, "_prepare_data_dir", lambda: None):
            with self.assertRaisesRegex(extra.ServiceError, "Mieru.*права доступа"):
                self.create()
        self.assertEqual(self.manager["load"]()["users"], [])

    def test_creation_repairs_old_directory_and_keeps_credentials_private(self):
        user = self.create()
        self.assertEqual(user["backend_port"], 54000)
        self.assertEqual(user["transport"], "TCP")
        self.assertEqual(self.dir_owner, ("root", "root"))
        self.assertEqual(self.dir_mode, 0o755)
        self.assertEqual(self.file_owners[extra.MIERU_CONFIG], ("root", "mita"))
        self.assertEqual(self.file_modes[extra.MIERU_CONFIG], 0o640)
        self.assertTrue(self.running)
        self.assertEqual(len(self.manager["load"]()["users"]), 1)
        config = json.loads(extra.MIERU_CONFIG.read_text())
        self.assertEqual(config["users"][0]["password"], user["secret"])

    def test_repeated_creation_after_tls_maintenance(self):
        self.create()
        match = re.search(r'install -d -o (\w+) -g (\w+) -m (\d+) "\$NAIVE_TLS_DIR"', INSTALLER)
        self.assertIsNotNone(match)
        owner, group, mode = match.groups()
        self.dir_owner, self.dir_mode = (owner, group), int(mode, 8)
        self.assertTrue(self.dir_mode & 0o001, "TLS maintenance must preserve mita traversal")
        self.create()
        self.assertEqual(len(self.manager["load"]()["users"]), 2)
        self.assertEqual(len(json.loads(extra.MIERU_CONFIG.read_text())["users"]), 2)

    def test_service_error_reaches_panel_without_credentials(self):
        fake = SimpleNamespace(PIPE=-1, run=lambda *a, **kw: SimpleNamespace(
            returncode=1, stderr="secret_in_traceback\nServiceError: Не удалось настроить Mieru.", stdout=""))
        namespace = dict(subprocess=fake, json=json, extra_protocols=extra, MANAGER="manager")
        functions_from(embedded('cat > "$APP_FILE" <<\'PY\''), {"ctl_manager_json"}, namespace)
        with self.assertRaises(extra.ServiceError) as caught:
            namespace["ctl_manager_json"]("add-json", {})
        self.assertEqual(str(caught.exception), "Не удалось настроить Mieru.")


if __name__ == "__main__":
    unittest.main()
