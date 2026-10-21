"""Portable WPP settings backup with a strict, checksummed ZIP format."""
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import time
import zipfile


FORMAT = 1
PRODUCT = "web-panel-proxy"
MAX_ARCHIVE = 16 * 1024 * 1024
MAX_UNPACKED = 32 * 1024 * 1024
MAX_FILES = 512

# Only logical, portable state is exported. Runtime counters, logs, binaries,
# certificates and systemd units are deliberately excluded.
ROOTS = {
    "config": Path("/etc/web-proxy-panel"),
    "panel": Path("/var/lib/tproxy-panel"),
    "site": Path("/srv/tproxy-site"),
}
PANEL_FILES = {
    "api.key", "custom-presets.json", "data.json", "location.json",
    "nodes.json", "routing.json", "site-draft.html", "site-source.html",
}
CONFIG_FILES = {"connections.json", "mtproxy-secrets", "primary-secret",
                "users.json", "xray-path"}
CONFIG_DIRS = {"openflux", "awg"}
REQUIRED = {"state/config/primary-secret", "state/config/users.json",
            "state/panel/data.json", "state/site/index.html"}


class BackupError(ValueError):
    pass


def _allowed(group, relative):
    value = PurePosixPath(relative)
    if not relative or value.is_absolute() or ".." in value.parts:
        return False
    if group == "panel":
        return len(value.parts) == 1 and relative in PANEL_FILES
    if group == "config":
        return ((len(value.parts) == 1 and relative in CONFIG_FILES) or
                (len(value.parts) > 1 and value.parts[0] in CONFIG_DIRS))
    return group == "site"


def _source_files():
    for group, root in ROOTS.items():
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(root).as_posix()
            if _allowed(group, relative):
                yield group, relative, path


def export_bytes(version=""):
    files = []
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for group, relative, path in _source_files():
            data = path.read_bytes()
            if len(data) > MAX_UNPACKED:
                raise BackupError("Один из файлов настроек слишком большой.")
            name = "state/%s/%s" % (group, relative)
            archive.writestr(name, data)
            files.append({"path": name, "size": len(data),
                          "sha256": hashlib.sha256(data).hexdigest()})
        manifest = {"format": FORMAT, "product": PRODUCT, "version": str(version),
                    "created_at": int(time.time()), "files": files}
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False,
                                                     separators=(",", ":")).encode("utf-8"))
    value = output.getvalue()
    if len(value) > MAX_ARCHIVE:
        raise BackupError("Архив настроек превышает лимит 16 МБ.")
    return value


def _read_archive(raw):
    if not raw or len(raw) > MAX_ARCHIVE:
        raise BackupError("Файл резервной копии пуст или превышает 16 МБ.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw), "r")
    except (zipfile.BadZipFile, OSError):
        raise BackupError("Выбранный файл не является резервной копией WPP.")
    infos = archive.infolist()
    if (len(infos) > MAX_FILES or len({item.filename for item in infos}) != len(infos) or
            sum(item.file_size for item in infos) > MAX_UNPACKED):
        archive.close()
        raise BackupError("Резервная копия содержит слишком много данных.")
    try:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
    except (KeyError, ValueError, UnicodeDecodeError):
        archive.close()
        raise BackupError("В резервной копии отсутствует корректный манифест.")
    if (not isinstance(manifest, dict) or manifest.get("format") != FORMAT or
            manifest.get("product") != PRODUCT or not isinstance(manifest.get("files"), list)):
        archive.close()
        raise BackupError("Формат резервной копии не поддерживается.")
    listed = {item.filename for item in infos}
    validated = []
    seen = set()
    for entry in manifest["files"]:
        if not isinstance(entry, dict):
            archive.close(); raise BackupError("Манифест резервной копии повреждён.")
        name = entry.get("path", "")
        parts = PurePosixPath(name).parts
        if len(parts) < 3 or parts[0] != "state" or name in seen or name not in listed:
            archive.close(); raise BackupError("Резервная копия содержит недопустимый путь.")
        group, relative = parts[1], PurePosixPath(*parts[2:]).as_posix()
        if group not in ROOTS or not _allowed(group, relative):
            archive.close(); raise BackupError("Резервная копия содержит недопустимый файл.")
        info = archive.getinfo(name)
        # Reject Unix symlinks and special files.
        mode = (info.external_attr >> 16) & 0o170000
        if mode not in (0, 0o100000) or info.file_size != entry.get("size"):
            archive.close(); raise BackupError("Тип или размер файла в архиве не совпадает.")
        data = archive.read(name)
        if hashlib.sha256(data).hexdigest() != entry.get("sha256"):
            archive.close(); raise BackupError("Контрольная сумма резервной копии не совпадает.")
        seen.add(name)
        validated.append((group, relative, data))
    if not REQUIRED.issubset(seen):
        archive.close()
        raise BackupError("Резервная копия неполная: отсутствуют основные настройки WPP.")
    return archive, manifest, validated


def restore_bytes(raw, apply_callback=None):
    archive, manifest, files = _read_archive(raw)
    archive.close()
    stage = Path(tempfile.mkdtemp(prefix="wpp-import.", dir="/tmp"))
    rollback = Path(tempfile.mkdtemp(prefix="wpp-restore.", dir="/tmp"))
    try:
        # Stage every verified byte before touching live settings.
        for group, relative, data in files:
            target = stage / group / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        for group, root in ROOTS.items():
            source = stage / group
            if not source.exists():
                continue
            saved = rollback / group
            if root.exists():
                shutil.copytree(root, saved, symlinks=True)
            if group == "site":
                shutil.rmtree(root, ignore_errors=True)
                root.mkdir(parents=True, exist_ok=True)
            elif group == "config":
                for subtree in ("openflux", "awg"):
                    shutil.rmtree(root / subtree, ignore_errors=True)
            for item in source.rglob("*"):
                relative = item.relative_to(source)
                target = root / relative
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary = target.with_name(target.name + ".restore-tmp")
                    temporary.write_bytes(item.read_bytes())
                    os.chmod(temporary, 0o640 if group == "site" else 0o600)
                    os.replace(temporary, target)
        for root in ROOTS.values():
            if root.exists():
                for directory in [root] + [p for p in root.rglob("*") if p.is_dir()]:
                    os.chmod(directory, 0o750 if root == ROOTS["site"] else 0o700)
        try:
            import grp
            site_gid = grp.getgrnam("tproxy").gr_gid
            for path in [ROOTS["site"]] + list(ROOTS["site"].rglob("*")):
                if path.exists() and not path.is_symlink():
                    os.chown(path, 0, site_gid)
        except (ImportError, KeyError, OSError):
            pass
        if apply_callback:
            apply_callback()
        return {"version": str(manifest.get("version", "")), "files": len(files)}
    except Exception:
        # Restore only roots that were present in the incoming archive.
        for group in {item[0] for item in files}:
            root, saved = ROOTS[group], rollback / group
            if saved.exists():
                shutil.rmtree(root, ignore_errors=True)
                shutil.copytree(saved, root, symlinks=True)
            else:
                shutil.rmtree(root, ignore_errors=True)
        if apply_callback:
            try:
                apply_callback()
            except Exception:
                pass
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        shutil.rmtree(rollback, ignore_errors=True)
