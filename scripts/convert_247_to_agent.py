"""Convert 247 from a NetMirror PANEL (with login gateway) to a pure AGENT
(allowlist gateway, no UI), registered under 246's single panel.

247 is CentOS7 with no native python3, so its gateway runs as a
python:3-alpine container (same as 246's gateway, but in AGENT_MODE via
ALLOW_IPS). The node URL http://<AGENT1_HOST>:3000 is unchanged, so 246's
existing "HKG1-Node2" node entry keeps working transparently.
"""
import sys, time
sys.path.insert(0, ".")
from nm_ssh import run_cmd

AGENT_IMAGE = "ghcr.io/asxiaowen/netmirror-agent:fixed"
LOCATION = "HKG1-Node2"
SELF_IP = "<AGENT1_HOST>"
PANEL_IP = "<PANEL_HOST>"  # kept for reference; agent gateway is now public so UI can call it


def step(tag, cmd, timeout=300, pause=2):
    print(f"\n[{tag}] $ {cmd[:120]}")
    rc, o, e = run_cmd(tag, cmd, timeout=timeout)
    if o.strip():
        print(o.strip())
    if e.strip():
        print("ERR:", e.strip())
    time.sleep(pause)
    return rc, o, e


print("=" * 64)
print("STEP 1: stop + remove 247 panel + login gateway")
print("=" * 64)
step("247", "docker rm -f netmirror-panel nm-gateway 2>/dev/null; echo cleaned")
step("247", "docker ps -a --format '{{.Names}} {{.Status}}' || true")

print("\n" + "=" * 64)
print("STEP 2: pull agent image")
print("=" * 64)
step("247", f"docker pull {AGENT_IMAGE}", timeout=420)

print("\n" + "=" * 64)
print("STEP 3: run netmirror-agent on 127.0.0.1:3001 (localhost-only)")
print("=" * 64)
step("247", (
    f"docker run -d --name netmirror-agent --restart always "
    f"-p 127.0.0.1:3001:3000 "
    f"-e HTTP_PORT=3000 -e LOCATION={LOCATION} "
    f"-e PUBLIC_IPV4={SELF_IP} -e AGENT_MODE=true "
    f"-v /opt/netmirror/data:/data {AGENT_IMAGE}"
))
time.sleep(6)
step("247", "docker ps --filter name=netmirror-agent --format '{{.Names}} {{.Status}}'")

print("\n" + "=" * 64)
print("STEP 4: run public agent-mode gateway on host :3000")
print("=" * 64)
gw = (
    "docker rm -f nm-gateway 2>/dev/null; "
    "docker run -d --name nm-gateway --network host --restart always "
    "-v /opt/nm-gateway:/data "
    "-e PORT=3000 -e UPSTREAM=127.0.0.1:3001 "
    "-e AGENT_MODE=true "
    "-e USERS_FILE=/data/users.txt -e SESS_FILE=/data/sessions.json "
    "--entrypoint python3 python:3-alpine /data/gateway.py"
)
step("247", gw, timeout=180)
time.sleep(4)
step("247", "docker ps --filter name=nm-gateway --format '{{.Names}} {{.Status}}'; "
              "ss -ltnp 2>/dev/null | grep ':3000' || true")

print("\n" + "=" * 64)
print("STEP 5: verify agent responds on :3000 (localhost)")
print("=" * 64)
rc, o, e = run_cmd("247",
    "curl -s -m 8 http://127.0.0.1:3000/ -w '\\nHTTP %{http_code}\\n' 2>&1")
print(o.strip() or e.strip())

print("\n" + "=" * 64)
print("STEP 6: verify 246 -> 247:3000 (allowed IP) gets agent response")
print("=" * 64)
rc, o, e = run_cmd("246",
    f"curl -s -m 8 http://{SELF_IP}:3000/ -w '\\nHTTP %{{http_code}}\\n' 2>&1 | head -c 400")
print(o.strip() or e.strip())

print("\n" + "=" * 64)
print("STEP 7: verify 246 -> 247:3000/session returns a SessionId (SSE)")
print("=" * 64)
rc, o, e = run_cmd("246",
    f"curl -s -N -m 8 http://{SELF_IP}:3000/session 2>&1 | head -c 400")
print(o.strip() or e.strip())

print("\nDONE convert_247_to_agent")
