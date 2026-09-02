"""Deploy NetMirror Agent on 247 and register it to the 246 Panel."""
import json
import time
from nm_ssh import run_cmd

KEY = json.load(open("nm_config.json"))["admin_api_key"]
PANEL = "http://<PANEL_HOST>:8050"
AGENT_PORT = "8050"

# ---- 1. On 247: stop old LG, pull agent image, open fw, run agent ----
cmds_247 = [
    "docker rm -f looking-glass 2>/dev/null; echo removed-old",
    "docker pull soyorins/netmirror-agent:latest",
    "firewall-cmd --permanent --add-port=8050/tcp; firewall-cmd --reload; echo fw-ok",
    (
        "docker rm -f netmirror-agent 2>/dev/null; "
        f"docker run -d --name netmirror-agent --network host --restart always "
        f"-e AGENT_MODE=true "
        f"-e HTTP_PORT={AGENT_PORT} "
        f"-e LOCATION='Hong Kong, HKG1-B' "
        f"-e PUBLIC_IPV4=<AGENT1_HOST> "
        f"soyorins/netmirror-agent:latest"
    ),
    "sleep 6; docker ps -a --filter name=netmirror-agent --format '{{.Names}} | {{.Status}}'",
]
for c in cmds_247:
    print("\n[247] >>>", c[:80])
    rc, o, e = run_cmd("247", c, timeout=300)
    print("RC", rc); print(o)
    if e.strip(): print("ERR:", e)
    time.sleep(1)
