"""
NetMirror API client — programmatic access to a NetMirror Panel or Agent node.

Verified against the deployed instance (Panel <PANEL_HOST>:8050, Agent <AGENT1_HOST>:8050).

How the API works (reverse-engineered from the running binary + frontend):
  1. Open an SSE stream at  <base>/session  ->  first event "SessionId" gives the session token.
  2. Keep that SSE connection OPEN. It also emits "Config" (node info/features) and,
     once you trigger a test, the streamed results (event name == the method, e.g. "Ping").
  3. Trigger a test with  GET <base>/method/<cmd>?ip=<target>   header: session: <SessionId>
     The HTTP response is just {"success":true}; the real output arrives via the SSE channel.
  4. Parse the SSE events by event-name.

Method names (from Config feature flags): ping, traceroute, mtr, iperf3,
speedtestdotnet, shell. param must be `ip` (NOT `host`).

Example
-------
    from nm_api_client import NetMirror
    node = NetMirror("http://<PANEL_HOST>:8050")     # panel's own node
    ack, results = node.run("ping", "8.8.8.8", window=6)
    for ev in results:
        print(ev["data"])
"""
import json
import threading
import time
import urllib.request
import urllib.parse


class NetMirror:
    def __init__(self, base_url, connect_timeout=10):
        self.base = base_url.rstrip("/")
        self._events = []          # full event log: (event, data)
        self._ev_lock = threading.Lock()
        self._stop = threading.Event()
        self.session_id = None
        self.config = None
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()
        for _ in range(connect_timeout * 20):
            if self.session_id:
                break
            time.sleep(0.05)
        if not self.session_id:
            raise RuntimeError("NetMirror: failed to obtain SessionId from %s/session" % self.base)

    # ----- internal SSE reader -----
    def _reader(self):
        try:
            r = urllib.request.urlopen(
                urllib.request.Request(self.base + "/session"), timeout=120)
        except Exception as e:  # pragma: no cover
            with self._ev_lock:
                self._events.append(("__ERR__", repr(e)))
            return
        buf = ""
        for raw in r:
            if self._stop.is_set():
                break
            buf += raw.decode(errors="replace")
            while "\n\n" in buf:
                msg, buf = buf.split("\n\n", 1)
                ev, data = "message", []
                for line in msg.splitlines():
                    if line.startswith("event:"):
                        ev = line[6:].strip()
                    elif line.startswith("data:"):
                        data.append(line[5:].strip())
                data = "\n".join(data)
                with self._ev_lock:
                    self._events.append((ev, data))
                if ev == "SessionId":
                    self.session_id = data
                elif ev == "Config":
                    try:
                        self.config = json.loads(data)
                    except Exception:
                        pass
        r.close()

    # ----- trigger a test, collect streamed results -----
    def run(self, method, target, window=6, count_event=None):
        """Trigger <method> against <target> and return (ack_json, list_of_result_dicts).

        result_dicts are the parsed SSE events whose event-name matches the method
        (case-insensitive). `window` seconds controls how long we listen for output.
        """
        # snapshot cursor BEFORE triggering so we don't miss already-streamed events
        with self._ev_lock:
            seen = len(self._events)
        url = self.base + "/method/" + method + "?" + urllib.parse.urlencode({"ip": target})
        req = urllib.request.Request(url, headers={"session": self.session_id})
        try:
            r = urllib.request.urlopen(req, timeout=window + 30)
            ack = json.loads(r.read(200).decode(errors="replace") or "{}")
        except Exception as e:
            ack = {"error": repr(e)}

        # listen for results
        start = time.time()
        collected = []
        while time.time() - start < window:
            with self._ev_lock:
                new = self._events[seen:]
                seen = len(self._events)
            for ev, data in new:
                if ev and ev.lower() == method.lower():
                    try:
                        collected.append(json.loads(data))
                    except Exception:
                        collected.append({"raw": data})
            time.sleep(0.2)
        return ack, collected

    def close(self):
        self._stop.set()


if __name__ == "__main__":
    import sys
    base = sys.argv[1] if len(sys.argv) > 1 else "http://<PANEL_HOST>:8050"
    target = sys.argv[2] if len(sys.argv) > 2 else "8.8.8.8"
    method = sys.argv[3] if len(sys.argv) > 3 else "ping"

    print("Connecting to", base)
    nm = NetMirror(base)
    print("Session:", nm.session_id)
    if nm.config:
        print("Node   :", nm.config.get("location"), "| IPv4", nm.config.get("public_ipv4"))
    ack, results = nm.run(method, target, window=6)
    print("ACK    :", ack)
    print("Results:")
    for r in results:
        print("  ", r)
    nm.close()
