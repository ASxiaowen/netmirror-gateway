"""Create a deploy token for 247 and register the agent to the 246 Panel."""
import json
import urllib.request
import urllib.error

KEY = json.load(open("nm_config.json"))["admin_api_key"]
PANEL = "http://<PANEL_HOST>:8050"
AGENT_URL = "http://<AGENT1_HOST>:8050"


def api(method, path, body=None, token=KEY):
    headers = {"Content-Type": "application/json", "X-API-Key": token}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(PANEL + path, data=data, headers=headers, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=20)
        return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as e:
        return -1, repr(e)


# 1) create token for 247
st, resp = api("POST", "/api/admin/tokens", {"name": "HKG1-Node2", "location": "Hong Kong, HKG1-B"})
print("create token:", st, resp)
tok = resp.get("token", {}).get("token") if isinstance(resp.get("token"), dict) else resp.get("token")
# handle both shapes
if isinstance(resp.get("token"), dict):
    tok = resp["token"]["token"]
print("agent token =", tok)

# 2) register agent
st2, resp2 = api("POST", "/api/register", {"token": tok, "url": AGENT_URL})
print("register:", st2, resp2)

# 3) list nodes
st3, resp3 = api("GET", "/api/admin/nodes")
print("nodes:", st3, json.dumps(resp3, ensure_ascii=False)[:800])
