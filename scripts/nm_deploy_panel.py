"""Deploy NetMirror Panel on 246 (master)."""
import json
import secrets
import time
from nm_ssh import run_cmd

ADMIN_KEY = "nm_" + secrets.token_hex(24)
PANEL_PORT = "8050"

# persist config locally for reference
with open("nm_config.json", "w") as f:
    json.dump({"admin_api_key": ADMIN_KEY, "panel_url": f"http://<PANEL_HOST>:{PANEL_PORT}"}, f, indent=2)

print("ADMIN_API_KEY =", ADMIN_KEY)

cmds = [
    # stop & remove old containers we deployed earlier
    "docker rm -f looking-glass lg-agg 2>/dev/null; echo removed-old",
    # pull panel image
    "docker pull soyorins/netmirror-panel:latest",
    # run panel (host network -> listens on PANEL_PORT directly)
    (
        "docker rm -f netmirror-panel 2>/dev/null; "
        f"docker run -d --name netmirror-panel --network host --restart always "
        f"-e ADMIN_API_KEY={ADMIN_KEY} "
        f"-e HTTP_PORT={PANEL_PORT} "
        f"-e LOCATION='Hong Kong, HKG1' "
        f"-e PUBLIC_IPV4=<PANEL_HOST> "
        f"-e DISPLAY_TRAFFIC=true -e ENABLE_SPEEDTEST=true "
        f"-e UTILITIES_PING=true -e UTILITIES_MTR=true -e UTILITIES_TRACEROUTE=true "
        f"-e UTILITIES_SPEEDTESTDOTNET=true -e UTILITIES_FAKESHELL=true -e UTILITIES_IPERF3=true "
        f"-v /opt/netmirror-panel:/data "
        f"soyorins/netmirror-panel:latest"
    ),
    "sleep 6; docker ps -a --filter name=netmirror-panel --format '{{.Names}} | {{.Status}}'",
]

for c in cmds:
    print("\n>>> " + c[:90])
    rc, o, e = run_cmd("246", c, timeout=300)
    print("RC", rc)
    print(o)
    if e.strip():
        print("ERR:", e)
    time.sleep(1)
