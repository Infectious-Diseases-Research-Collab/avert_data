#!/usr/bin/env python3
"""Download AVERT data files from the Burkina (SFTP) and Uganda (FTP) servers.

Credentials live in credentials.json next to this script. For each remote
file: skip it if a file with the same name already exists locally, and skip
it if the remote file size is 0.
"""

import ftplib
import json
import stat
import sys
from pathlib import Path

import paramiko

BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_FILE = BASE_DIR / "credentials.json"
DATA_DIR = BASE_DIR / "data"


def load_credentials():
    with open(CREDENTIALS_FILE) as f:
        return json.load(f)


def download_sftp(creds, local_dir):
    """Download all regular files from an SFTP server."""
    downloaded, skipped = 0, 0
    transport = paramiko.Transport((creds["host"], creds["port"]))
    try:
        transport.connect(username=creds["username"], password=creds["password"])
        sftp = paramiko.SFTPClient.from_transport(transport)
        sftp.chdir(creds.get("remote_dir", "/"))
        for entry in sftp.listdir_attr():
            if not stat.S_ISREG(entry.st_mode):
                continue  # skip directories etc.
            name = entry.filename
            local_path = local_dir / name
            if local_path.exists():
                skipped += 1
                continue
            if entry.st_size == 0:
                print(f"  SKIP (0 bytes): {name}")
                skipped += 1
                continue
            print(f"  downloading: {name} ({entry.st_size:,} bytes)")
            tmp_path = local_path.with_suffix(local_path.suffix + ".part")
            try:
                sftp.get(name, str(tmp_path))
                tmp_path.rename(local_path)
                downloaded += 1
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise
    finally:
        transport.close()
    return downloaded, skipped


def download_ftp(creds, local_dir):
    """Download all regular files from a plain FTP server."""
    downloaded, skipped = 0, 0
    with ftplib.FTP() as ftp:
        ftp.connect(creds["host"], creds["port"], timeout=60)
        ftp.login(creds["username"], creds["password"])
        ftp.cwd(creds.get("remote_dir", "/"))
        names = ftp.nlst()
        # NLST switches the connection to ASCII mode; SIZE requires binary mode
        ftp.voidcmd("TYPE I")
        for name in names:
            if name in (".", ".."):
                continue
            local_path = local_dir / name
            if local_path.exists():
                skipped += 1
                continue
            try:
                size = ftp.size(name)
            except ftplib.error_perm:
                # SIZE fails on directories - skip them
                continue
            if size == 0:
                print(f"  SKIP (0 bytes): {name}")
                skipped += 1
                continue
            print(f"  downloading: {name} ({size:,} bytes)")
            tmp_path = local_path.with_suffix(local_path.suffix + ".part")
            try:
                with open(tmp_path, "wb") as f:
                    ftp.retrbinary(f"RETR {name}", f.write)
                tmp_path.rename(local_path)
                downloaded += 1
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise
    return downloaded, skipped


def main():
    creds = load_credentials()
    ok = True
    for site, folder in (("burkina", "burkina"), ("uganda", "uganda")):
        local_dir = DATA_DIR / folder
        local_dir.mkdir(parents=True, exist_ok=True)
        site_creds = creds[site]
        print(f"[{site}] connecting to {site_creds['host']}:{site_creds['port']} ...")
        try:
            if site_creds["protocol"] == "sftp":
                downloaded, skipped = download_sftp(site_creds, local_dir)
            else:
                downloaded, skipped = download_ftp(site_creds, local_dir)
            print(f"[{site}] done: {downloaded} downloaded, {skipped} skipped\n")
        except Exception as e:
            print(f"[{site}] ERROR: {e}\n", file=sys.stderr)
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
