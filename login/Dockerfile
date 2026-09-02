# NetMirror Gateway — self-contained Docker image
# Pure standard-library reverse proxy + login/access-control + share links +
# admin console + dark/light theme toggle. No pip dependencies.
FROM python:3-alpine

# Application code lives in /app (read-only at runtime).
# Runtime data (keys, sessions, users, shares) lives in /data — mount a host
# volume there to persist it across container restarts. Keeping code and data in
# separate directories means bind-mounting /data never hides the gateway code.
WORKDIR /app

# The gateway is a single file with zero third-party imports.
COPY gateway.py /app/gateway.py

# Default upstream is the local panel/agent backend (NetMirror listens on :3001
# internally; override with -e UPSTREAM=host:port if yours differs).
ENV PORT=3000 \
    UPSTREAM=127.0.0.1:3001 \
    AGENT_MODE=false

VOLUME /data
EXPOSE 3000

# Must run with --network host so it can reach 127.0.0.1:3001 on the host and
# so visitor IPs are not NAT'd behind the docker bridge.
CMD ["python", "/app/gateway.py"]
