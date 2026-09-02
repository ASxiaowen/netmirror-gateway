"""Build the NetMirror gateway Docker image on host 246 and push to ghcr.io.

Steps:
  1. SFTP upload login/Dockerfile + login/gateway.py to /opt/nm-gw-build/
  2. docker build -t ghcr.io/asxiaowen/netmirror-gateway:<tag> /opt/nm-gw-build
  3. docker push ghcr.io/asxiaowen/netmirror-gateway:<tag>
"""
import os
import sys
import time
import socket
import paramiko

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nm_ssh as s

HOST = "246"
BUILD_DIR = "/opt/nm-gw-build"
IMAGE = "ghcr.io/asxiaowen/netmirror-gateway"
TAG = "latest"
DATE_TAG = "20260902"

LOCAL_DOCKERFILE = os.path.join("login", "Dockerfile")
LOCAL_GATEWAY = os.path.join("login", "gateway.py")


def sftp_upload(tag, local_path, remote_path):
    cfg = s.HOSTS[tag]
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(20)
    sock.connect((cfg["host"], cfg["port"]))
    t = paramiko.Transport(sock)
    t.banner_timeout = 20
    t.handshake_timeout = 20
    t.connect(username=cfg["user"], password=cfg["pass"])
    try:
        sftp = paramiko.SFTPClient.from_transport(t)
        # ensure remote dir exists
        try:
            sftp.stat(BUILD_DIR)
        except IOError:
            sftp.mkdir(BUILD_DIR)
        sftp.put(local_path, remote_path)
        print(f"  uploaded {local_path} -> {remote_path}")
    finally:
        t.close()


def run(cmd, timeout=600):
    rc, o, e = s.run_cmd(HOST, cmd, timeout=timeout)
    print(f"$ {cmd}\nRC={rc}")
    if o.strip():
        print(o)
    if e.strip():
        print("ERR:", e)
    return rc


def main():
    print("[1/3] Upload build context to", HOST)
    sftp_upload(HOST, LOCAL_DOCKERFILE, f"{BUILD_DIR}/Dockerfile")
    sftp_upload(HOST, LOCAL_GATEWAY, f"{BUILD_DIR}/gateway.py")

    print("[2/3] docker build")
    rc = run(f"docker build -t {IMAGE}:{TAG} -t {IMAGE}:{DATE_TAG} {BUILD_DIR}")
    if rc != 0:
        print("BUILD FAILED")
        return

    print("[3/3] docker push")
    rc = run(f"docker push {IMAGE}:{TAG}")
    if rc != 0:
        print("PUSH :latest FAILED")
        return
    rc = run(f"docker push {IMAGE}:{DATE_TAG}")
    if rc != 0:
        print("PUSH :{DATE_TAG} FAILED")
        return

    # sanity: list local images
    run(f"docker images {IMAGE}")
    print("DONE")


if __name__ == "__main__":
    main()
