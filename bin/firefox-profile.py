#!/usr/bin/env python3
"""Back up or restore the container's Firefox profile.

The profile holds the browser's cookies, saved logins and preferences, so it is the one
piece of state in this setup that hurts to lose: `docker compose down -v` or a stray
`rm -rf` on the volume takes the X session with it.

  bin/firefox-profile.py backup            # -> ./backups/firefox-profile-<ts>.tar.gz
  bin/firefox-profile.py restore [FILE]    # newest backup when FILE is omitted
  bin/firefox-profile.py list

Caches, session restore data and SQLite journals are left out of the archive. Every
*.sqlite is copied through sqlite3's backup API, so a running Firefox does not produce a
torn copy.
"""
import argparse
import os
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTAINER = os.environ.get("SHUTTLE_CONTAINER", "shuttle-box")
PROFILE = os.environ.get("FIREFOX_PROFILE", "/home/box/.mozilla/firefox/agent.default")
BACKUP_DIR = os.environ.get("SHUTTLE_BACKUP_DIR", os.path.join(REPO, "backups"))

BACKUP_PY = r'''
import os, sqlite3, sys, tarfile

src, out = sys.argv[1], sys.argv[2]
SKIP_DIRS = {"cache2", "startupCache", "shader-cache", "security_state", "crashes",
             "datareporting", "sessionstore-backups", "storage", "Saved Login Backups"}
kept = 0
with tarfile.open(out, "w:gz") as tar:
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if name.endswith(("-wal", "-shm")) or name in ("lock", ".parentlock"):
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, src)
            if name.endswith(".sqlite"):
                tmp = "/tmp/ffbak-" + name
                try:
                    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
                    dst = sqlite3.connect(tmp)
                    with dst:
                        con.backup(dst)
                    con.close()
                    dst.close()
                    tar.add(tmp, arcname=rel)
                    os.unlink(tmp)
                    kept += 1
                    continue
                except Exception as exc:
                    print("warn: %s: %s" % (rel, exc), file=sys.stderr)
            tar.add(path, arcname=rel)
            kept += 1
print(kept)
'''

RESTORE_PY = r'''
import os, shutil, sys, tarfile, time

src, dst = sys.argv[1], sys.argv[2]
if os.path.isdir(dst):
    moved = dst + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    os.rename(dst, moved)
    print("previous profile kept at", moved)
os.makedirs(dst, exist_ok=True)
with tarfile.open(src) as tar:
    tar.extractall(dst)
print("restored", sum(len(f) for _, _, f in os.walk(dst)), "files")
'''


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def die(msg):
    print(f"firefox-profile: {msg}", file=sys.stderr)
    sys.exit(1)


def ensure_container():
    probe = run(["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER])
    if probe.returncode != 0:
        die(f"no container named {CONTAINER}")
    if probe.stdout.strip() != "true":
        die(f"{CONTAINER} is not running (start it with `make up`)")


def backups():
    if not os.path.isdir(BACKUP_DIR):
        return []
    return sorted(
        (os.path.join(BACKUP_DIR, f) for f in os.listdir(BACKUP_DIR)
         if f.startswith("firefox-profile-") and f.endswith(".tar.gz")),
        key=os.path.getmtime,
    )


def do_backup():
    ensure_container()
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"firefox-profile-{stamp}.tar.gz"
    remote = f"/tmp/{name}"
    proc = subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "python3", "-", PROFILE, remote],
        input=BACKUP_PY, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        die(f"in-container backup failed: {proc.stderr.strip()}")
    dest = os.path.join(BACKUP_DIR, name)
    cp = run(["docker", "cp", f"{CONTAINER}:{remote}", dest])
    run(["docker", "exec", CONTAINER, "rm", "-f", remote])
    if cp.returncode != 0:
        die(f"docker cp failed: {cp.stderr.strip()}")
    print(f"backed up {proc.stdout.strip()} files -> {dest} ({os.path.getsize(dest)} bytes)")


def do_restore(path):
    ensure_container()
    if path is None:
        found = backups()
        if not found:
            die(f"no backup in {BACKUP_DIR}; run `bin/firefox-profile.py backup` first")
        path = found[-1]
    if not os.path.isfile(path):
        die(f"no such backup: {path}")
    print(f"stopping firefox in {CONTAINER}")
    run(["docker", "exec", CONTAINER, "pkill", "-x", "firefox-bin"])
    time.sleep(2)
    remote = "/tmp/restore-" + os.path.basename(path)
    cp = run(["docker", "cp", path, f"{CONTAINER}:{remote}"])
    if cp.returncode != 0:
        die(f"docker cp failed: {cp.stderr.strip()}")
    proc = subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "python3", "-", remote, PROFILE],
        input=RESTORE_PY, capture_output=True, text=True,
    )
    run(["docker", "exec", CONTAINER, "rm", "-f", remote])
    if proc.returncode != 0:
        die(f"restore failed: {proc.stderr.strip()}")
    print(proc.stdout.strip())


def do_list():
    found = backups()
    if not found:
        print(f"no backups in {BACKUP_DIR}")
        return
    for p in found:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(os.path.getmtime(p)))}  "
              f"{os.path.getsize(p) // 1024:>7} KB  {os.path.basename(p)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("backup")
    p = sub.add_parser("restore")
    p.add_argument("file", nargs="?")
    sub.add_parser("list")
    args = ap.parse_args()

    if args.cmd == "backup":
        do_backup()
    elif args.cmd == "restore":
        do_restore(args.file)
    elif args.cmd == "list":
        do_list()
    return 0


if __name__ == "__main__":
    sys.exit(main())
