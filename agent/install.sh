#!/usr/bin/env bash
set -euo pipefail
umask 077
BASE=@BASE@
if [ "$(id -u)" -ne 0 ]; then echo 'Please run as root.' >&2; exit 1; fi
if ! command -v systemctl >/dev/null || ! command -v apt-get >/dev/null; then
  echo 'This installer supports Debian/Ubuntu with systemd.' >&2; exit 1
fi
case "$(uname -m)" in
  x86_64) ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) echo 'Unsupported architecture.' >&2; exit 1 ;;
esac
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv ca-certificates curl tar
TMP=$(mktemp -d)
trap 'rm -rf -- "$TMP"' EXIT
mkdir -p /opt/sub-manager-agent /etc/sub-manager-agent /var/lib/sub-manager-agent
chmod 700 /etc/sub-manager-agent /var/lib/sub-manager-agent
curl --proto '=https' --tlsv1.2 -fsSL "$BASE/api/agent/core-manifest" -o "$TMP/manifest.json"
SHA=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$TMP/manifest.json" "$ARCH")
curl --proto '=https' --tlsv1.2 -fsSL "$BASE/api/agent/core/$ARCH" -o "$TMP/sing-box"
printf '%s  %s\n' "$SHA" "$TMP/sing-box" | sha256sum -c -
chmod 755 "$TMP/sing-box"
curl --proto '=https' --tlsv1.2 -fsSL "$BASE/api/agent/files/control_agent.py" -o "$TMP/control_agent.py"
curl --proto '=https' --tlsv1.2 -fsSL "$BASE/api/agent/files/stats.py" -o "$TMP/stats.py"
printf '%s  %s\n' '@CONTROL_AGENT_PY_SHA@' "$TMP/control_agent.py" '@STATS_PY_SHA@' "$TMP/stats.py" | sha256sum -c -
python3 -m venv /opt/sub-manager-agent/venv
/opt/sub-manager-agent/venv/bin/pip -q install grpcio==1.76.0
# Check the downloaded build has the required statistics API before consuming the pairing token.
printf '%s\n' '{"inbounds":[],"outbounds":[{"type":"direct"}],"experimental":{"v2ray_api":{"listen":"127.0.0.1:19091","stats":{"enabled":true}}}}' > "$TMP/check.json"
"$TMP/sing-box" check -c "$TMP/check.json" >/dev/null
cat > "$TMP/pairing.json" <<'PAIRING'
@PAIRING@
PAIRING
python3 - "$TMP/pairing.json" <<'PY'
import json, os, pathlib, urllib.request, uuid
pairing = json.loads(pathlib.Path(__import__('sys').argv[1]).read_text())
path = pathlib.Path('/etc/sub-manager-agent/config.json')
old = json.loads(path.read_text()) if path.exists() else {}
if old and old.get('base') != pairing['base']:
    raise SystemExit('Existing agent belongs to another panel. Remove it explicitly before switching.')
if old and old.get('server_id') != pairing['server_id']:
    raise SystemExit('This host is already enrolled as a different server. Do not reuse the agent directory.')
instance = old.get('instance_id') or uuid.uuid4().hex
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None
req = urllib.request.Request(pairing['base']+'/api/agent/enroll',
    data=json.dumps({'token':pairing['token'],'instance_id':instance}).encode(),
    headers={'Content-Type':'application/json'})
with urllib.request.build_opener(NoRedirect).open(req, timeout=30) as response:
    result = json.load(response)
if old and old.get('server_id') != result['server_id']:
    raise SystemExit('This host is already enrolled as a different server. Do not reuse the agent directory.')
config = {'base':pairing['base'],'token':result['token'],'server_id':result['server_id'],
          'instance_id':instance,'binary':'/opt/sub-manager-agent/sing-box',
          'state_dir':'/var/lib/sub-manager-agent'}
temp=path.with_suffix('.tmp')
temp.write_text(json.dumps(config)); os.chmod(temp,0o600); temp.replace(path)
PY
systemctl stop sub-manager-agent.service 2>/dev/null || true
install -m 755 "$TMP/sing-box" /opt/sub-manager-agent/sing-box
install -m 644 "$TMP/control_agent.py" /opt/sub-manager-agent/control_agent.py
install -m 644 "$TMP/stats.py" /opt/sub-manager-agent/stats.py
cat > /etc/systemd/system/sub-manager-agent.service <<'SERVICE'
[Unit]
Description=Sub Manager proxy agent
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/opt/sub-manager-agent/venv/bin/python /opt/sub-manager-agent/control_agent.py --config /etc/sub-manager-agent/config.json
Restart=always
RestartSec=5
KillMode=control-group
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/sub-manager-agent
CapabilityBoundingSet=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
SERVICE
systemctl daemon-reload
systemctl enable --now sub-manager-agent.service
echo 'Agent installed. Create nodes and assign user packages in the panel. Allow configured proxy ports in your firewall.'
