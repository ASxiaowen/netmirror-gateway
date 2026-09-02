#!/usr/bin/env python3
"""Upgrade 246/247 nm-gateway to the new image that fixes /method/* 502 timeouts."""
import sys
import time
import socket
import paramiko

sys.path.insert(0, ".")
import nm_ssh as s

IMAGE = "ghcr.io/asxiaowen/netmirror-gateway:latest"

CONFIG = {
    "246": {
        "host": "<PANEL_HOST>",
        "port": 22,
        "user": "root",
        "pass": "<ROOT_PASSWORD>",
        "env": [
            "PORT=3000",
            "UPSTREAM=127.0.0.1:3001",
            "PEER_IPS=<AGENT1_HOST>",
            "USERS_FILE=/data/users.txt",
            "SESS_FILE=/data/sessions.json",
            "AGENT_MODE=false",
        ],
    },
    "247": {
        "host": "<AGENT1_HOST>",
        "port": 22,
        "user": "root",
        "pass": "<ROOT_PASSWORD>",
        "env": [
            "AGENT_MODE=true",
            "USERS_FILE=/data/users.txt",
            "SESS_FILE=/data/sessions.json",
            "PORT=3000",
            "UPSTREAM=127.0.0.1:3001",
        ],
    },
}


def connect(cfg, timeout=20):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect((cfg["host"], cfg["port"]))
    t = paramiko.Transport(sock)
    t.banner_timeout = timeout
    t.handshake_timeout = timeout
    t.connect(username=cfg["user"], password=cfg["pass"])
    return t


def run(t, cmd, timeout=120):
    ch = t.open_session()
    ch.settimeout(timeout)
    ch.exec_command(cmd)
    out, err = b"", b""
    while not ch.exit_status_ready():
        if ch.recv_ready():
            out += ch.recv(65536)
        if ch.recv_stderr_ready():
            err += ch.recv_stderr(65536)
        time.sleep(0.05)
    while ch.recv_ready():
        out += ch.recv(65536)
    while ch.recv_stderr_ready():
        err += ch.recv_stderr(65536)
    return ch.recv_exit_status(), out.decode(errors="replace"), err.decode(errors="replace")


def upgrade(tag):
    cfg = CONFIG[tag]
    print(f"\n===== {tag} ({cfg['host']}) =====")
    t = connect(cfg)
    try:
        print("[1/4] pull latest image")
        rc, o, e = run(t, f"docker pull {IMAGE}", timeout=300)
        print(f"pull rc={rc}")
        if e.strip():
            print("err:", e.strip()[:500])

        print("[2/4] stop & remove old container")
        rc, o, e = run(t, "docker stop nm-gateway && docker rm nm-gateway", timeout=60)
        print(f"stop/rm rc={rc}")
        if e.strip() and "No such container" not in e:
            print("err:", e.strip()[:500])

        print("[3/4] recreate container")
        env_args = " ".join(f'-e "{v}"' for v in cfg["env"])
        cmd = (
            f"docker run -d --name nm-gateway --restart always --network host "
            f"-v /opt/nm-gateway:/data {env_args} {IMAGE}"
        )
        rc, o, e = run(t, cmd, timeout=120)
        print(f"run rc={rc}")
        if o.strip():
            print("container:", o.strip()[:80])
        if e.strip():
            print("err:", e.strip()[:500])

        print("[4/4] verify login page")
        for i in range(10):
            rc, o, e = run(t, "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:3000/login; echo", timeout=30)
            code = o.strip()
            print(f"  attempt {i+1}: HTTP {code}")
            if code == "200":
                break
            time.sleep(1)
    finally:
        t.close()


if __name__ == "__main__":
    only = sys.argv[1:] or list(CONFIG)
    for tag in only:
        if tag not in CONFIG:
            print(f"[skip] unknown host {tag}")
            continue
        upgrade(tag)
    print("\nDONE")
