#!/usr/bin/env python3
"""Update the 248 native (systemd) gateway: SFTP the new gateway.py and restart."""
import os
import time
import socket
import paramiko
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nm_ssh as s

TAG = "248"
REMOTE_PATH = "/opt/nm-gateway/gateway.py"
LOCAL_PATH = os.path.join("login", "gateway.py")


def sftp_put(local, remote):
    cfg = s.HOSTS[TAG]
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(20)
    sock.connect((cfg["host"], cfg["port"]))
    t = paramiko.Transport(sock)
    t.banner_timeout = 20
    t.handshake_timeout = 20
    t.connect(username=cfg["user"], password=cfg["pass"])
    try:
        ftp = paramiko.SFTPClient.from_transport(t)
        # back up the previous version
        try:
            ftp.rename(REMOTE_PATH, REMOTE_PATH + ".bak")
        except IOError:
            pass
        ftp.put(local, REMOTE_PATH)
        print(f"  uploaded {local} -> {remote}")
    finally:
        t.close()


def run(cmd, timeout=120):
    rc, o, e = s.run_cmd(TAG, cmd, timeout=timeout)
    print(f"$ {cmd}\nRC={rc}")
    if o.strip():
        print(o.strip()[:800])
    if e.strip():
        print("ERR:", e.strip()[:800])
    return rc


def main():
    if not os.path.exists(LOCAL_PATH):
        print("LOCAL gateway.py not found:", LOCAL_PATH)
        return
    print("[1/3] SFTP upload new gateway.py to", TAG)
    sftp_put(LOCAL_PATH, REMOTE_PATH)
    print("[2/3] restart systemd service")
    run("systemctl restart nm-gateway", timeout=60)
    print("[3/3] verify it is up")
    for i in range(10):
        rc, o, e = s.run_cmd(TAG, "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:3000/; echo", timeout=30)
        code = o.strip()
        print(f"  attempt {i+1}: HTTP {code}")
        if code in ("200", "302", "401"):
            break
        time.sleep(1)
    print("DONE")


if __name__ == "__main__":
    main()
