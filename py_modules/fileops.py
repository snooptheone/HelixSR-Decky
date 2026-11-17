"""File helpers: hashing, atomic and durable writes, safe archive extraction, HTTPS context."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import ssl
import stat
import uuid
import zipfile
from functools import lru_cache


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def confirmed_executable_sha(path):
    """Hash a confirmed executable once per stable on-disk identity."""
    p = Path(path)
    info = p.stat()
    return _confirmed_executable_sha(str(p), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


@lru_cache(maxsize=64)
def _confirmed_executable_sha(path, device, inode, size, modified):
    return sha(path)


def atomic_json(path, value):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + '.tmp-' + uuid.uuid4().hex)
    with tmp.open('w', encoding='utf-8') as f:
        f.write(json.dumps(value, indent=2) + '\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    if os.name == 'posix':
        fd = os.open(p.parent, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def sync_directory(path):
    if os.name == 'posix':
        fd = os.open(path, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def durable_directory(path):
    """Persist each new directory entry before a recovery journal depends on it."""
    missing, parent = [], Path(path)
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        directory.mkdir()
        sync_directory(directory.parent)


def durable_copy(src, dst, expected=None):
    """Persist verified recovery bytes and every newly created directory entry."""
    src, dst = Path(src), Path(dst)
    durable_directory(dst.parent)
    expected = expected or sha(src)
    shutil.copy2(src, dst)
    if sha(dst) != expected or sha(src) != expected:
        raise ValueError('Recovery copy changed or failed verification')
    # POSIX permits syncing a read-only descriptor, preserving original modes.
    # Windows' CRT requires a writable descriptor for its fsync implementation.
    with dst.open('rb' if os.name == 'posix' else 'r+b') as stream:
        os.fsync(stream.fileno())
    sync_directory(dst.parent)


def https_context():
    # Decky's bundled Python may retain a build-host CA path. Use SteamOS's trust store.
    if os.name == 'posix':
        for path in ('/etc/ssl/certs/ca-certificates.crt', '/etc/ssl/cert.pem'):
            if Path(path).is_file():
                return ssl.create_default_context(cafile=path)
    return ssl.create_default_context()


def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {} if default is None else default


def checked_path(root, relative):
    p = Path(root) / relative
    if Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise ValueError('Unsafe managed path')
    current = p
    while current != Path(root):
        if current.is_symlink():
            raise ValueError(f'Symlink in managed path: {relative}')
        current = current.parent
    if Path(root).resolve() not in p.resolve().parents:
        raise ValueError('Managed path escapes game')
    return p


def safe_extract(archive, destination):
    total, seen = 0, set()
    with zipfile.ZipFile(archive) as z:
        if len(z.infolist()) > 30000:
            raise ValueError('Archive member limit')
        for info in z.infolist():
            name = info.filename.replace('\\', '/')
            p = PurePosixPath(name)
            normalized = str(p)
            total += info.file_size
            if (p.is_absolute() or '..' in p.parts or ':' in name or not p.parts or
                    stat.S_ISLNK(info.external_attr >> 16) or total > 2 << 30 or normalized in seen or
                    info.compress_size and info.file_size / info.compress_size > 1000):
                raise ValueError('Unsafe or oversized release archive')
            seen.add(normalized)
            target = Path(destination).joinpath(*p.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, target.open('wb') as dst:
                    shutil.copyfileobj(src, dst)
                mode = info.external_attr >> 16
                target.chmod(0o755 if mode & 0o111 else 0o644)
