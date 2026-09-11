#!/usr/bin/env bash
# Dedicated-host provisioning. --dry-run performs validation and prints a plan only.
set -Eeuo pipefail
export LC_ALL=C
umask 077
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$root"
domain='' email='' image='' ssh_port='' dry_run=0
usage() {
  cat <<'TEXT'
Usage: deploy/bootstrap_infra.sh --domain DNS_NAME --email TLS_EMAIL \
  --image ghcr.io/OWNER/IMAGE@sha256:DIGEST [--ssh-port PORT] [--dry-run]

Run as root on a dedicated Ubuntu 24.04 or Debian 12/13 VPS with public DNS
already pointing to it. OPENAI_API_KEY is required for the extraction worker.
Optional Stripe variables: STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET,
STRIPE_PRICE_STARTER, STRIPE_PRICE_TEAM, STRIPE_LIVEMODE.
Secrets are read from the environment, never command-line arguments or logs.
An existing initialized deployment is not reprovisioned or silently rekeyed.
TEXT
}
fail() { printf '%s\n' "$*" >&2; exit 1; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain|--email|--image|--ssh-port)
      [[ $# -ge 2 ]] || fail "Missing value for $1"
      case "$1" in
        --domain) domain=$2;; --email) email=$2;; --image) image=$2;; --ssh-port) ssh_port=$2;;
      esac
      shift 2;;
    --dry-run) dry_run=1; shift;;
    --help|-h) usage; exit 0;;
    *) usage >&2; fail "Unknown argument: $1";;
  esac
done
[[ "$domain" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ && "$domain" == *.* && ${#domain} -le 253 ]] || fail 'A DNS hostname is required.'
[[ "$email" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || fail 'A TLS contact email is required.'
[[ "$image" =~ ^ghcr\.io/[a-z0-9_./-]+@sha256:[a-f0-9]{64}$ ]] || fail 'An immutable GHCR application image digest is required.'
[[ -z "$ssh_port" || "$ssh_port" =~ ^[0-9]{1,5}$ ]] || fail 'Invalid SSH port.'
if [[ -n "$ssh_port" ]]; then
  ssh_port=$((10#$ssh_port))
  ((ssh_port > 0 && ssh_port < 65536)) || fail 'SSH port must be 1 through 65535.'
fi
if ((dry_run)); then
  cat <<TEXT
Validated dry-run plan for $domain; no files, packages, firewall rules or containers changed.
1. Require a fresh dedicated Ubuntu 24.04 / Debian 12 or 13 systemd host.
2. Preserve SSH authentication; discover its active port${ssh_port:+ and verify explicit port $ssh_port}.
3. Install Docker from its signed official apt repository plus UFW and fail2ban.
4. Allow host TCP SSH/80/443 and UDP 443; protect Docker forwarding separately.
5. Generate private database/Redis credentials; pin pulled infrastructure image digests.
6. Apply all migrations; start PostgreSQL 17 + pgvector, authenticated Redis and app.
7. Run Caddy/Redis/app as non-root, publish only Caddy, enable ACME TLS and streaming.
8. Deploy $image and verify HTTPS readiness; write local installation evidence.
Worker leases remain in PostgreSQL, atomically with the outbox; Redis provides shared
OAuth admission and encrypted OAuth persistence after identity-provider configuration.
TEXT
  exit 0
fi
[[ $EUID -eq 0 ]] || fail 'Provisioning must run as root.'
[[ "$root" =~ ^/[A-Za-z0-9_./-]+$ ]] || fail 'Install from an absolute path without spaces or shell metacharacters.'
[[ ! -e deploy/host.env && ! -e deploy/infra-attempted ]] || fail 'Host already initialized or provisioning was interrupted. Inspect existing protected state; credentials are never rotated on rerun.'
[[ -s /etc/os-release ]] || fail 'Missing OS release information.'
source /etc/os-release
case "$ID:$VERSION_ID" in ubuntu:24.04|debian:12|debian:13) ;; *) fail 'Supported hosts: Ubuntu 24.04, Debian 12, Debian 13.';; esac
[[ -d /run/systemd/system ]] || fail 'A normal systemd VPS is required; do not run this inside a container.'
[[ -n ${OPENAI_API_KEY:-} ]] || fail 'Export OPENAI_API_KEY before provisioning the extraction worker.'
for name in STRIPE_SECRET_KEY STRIPE_WEBHOOK_SECRET STRIPE_PRICE_STARTER STRIPE_PRICE_TEAM; do
  if [[ -n ${STRIPE_SECRET_KEY:-} && -z ${!name:-} ]]; then fail "Billing requested but $name is missing."; fi
done
command -v sshd >/dev/null || [[ -x /usr/sbin/sshd ]] || fail 'An existing OpenSSH server is required; SSH authentication is preserved.'
sshd_bin=$(command -v sshd || printf /usr/sbin/sshd)
"$sshd_bin" -t
mapfile -t configured_ports < <("$sshd_bin" -T | awk '$1=="port" {print $2}')
[[ ${#configured_ports[@]} -gt 0 ]] || fail 'Could not discover an SSH port.'
if [[ -n ${SSH_CONNECTION:-} ]]; then
  read -r _ _ _ connected_port <<< "$SSH_CONNECTION"
  [[ "$connected_port" =~ ^[0-9]+$ ]] || fail 'Could not parse the active SSH connection port.'
  if [[ -n "$ssh_port" && "$ssh_port" != "$connected_port" ]]; then
    fail 'The explicit SSH port differs from this active connection; refusing firewall changes.'
  fi
  ssh_port=$connected_port
fi
ssh_port=${ssh_port:-${configured_ports[0]}}
for port in "${configured_ports[@]}"; do
  [[ "$port" == "$ssh_port" ]] || fail 'Multiple or socket-overridden SSH ports require operator reconciliation before bootstrap.'
done
getent ahosts "$domain" >/dev/null || fail 'The public domain does not resolve yet.'
if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  [[ -z $(docker ps -aq) ]] || fail 'Existing containers detected. This installer only changes a dedicated empty Docker host.'
fi
for package in docker.io docker-compose docker-compose-v2 podman-docker containerd runc; do
  if dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -qx 'install ok installed'; then
    fail "Conflicting package $package is installed. Review its removal before provisioning."
  fi
done
if command -v ufw >/dev/null; then
  [[ $(ufw status | head -n1) != 'Status: active' ]] || fail 'An existing active firewall needs a reviewed merge; this installer will not reset it.'
  if ufw show added | grep -q '^ufw '; then fail 'Existing UFW rules need a reviewed merge; no rules were changed.'; fi
fi
if [[ -s /etc/docker/daemon.json ]]; then
  python3 - <<'PY'
import json
from pathlib import Path
config = json.loads(Path('/etc/docker/daemon.json').read_text())
if config.get('firewall-backend', 'iptables') != 'iptables' or config.get('iptables') is False:
    raise SystemExit('Docker must use its supported iptables firewall backend for this installer.')
PY
fi
mkdir -p deploy
chmod 700 deploy
printf 'Inspect this marker and protected state before retrying an interrupted host installation.\n' > deploy/infra-attempted
trap 'printf "Provisioning stopped at line %s. Existing state is preserved; inspect before retrying.\\n" "$LINENO" >&2' ERR
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl gnupg python3 ufw fail2ban iproute2 iptables
install -m 0755 -d /etc/apt/keyrings
key_tmp=$(mktemp)
curl --proto '=https' --tlsv1.2 --fail --silent --show-error "https://download.docker.com/linux/$ID/gpg" -o "$key_tmp"
gpg --show-keys --with-colons "$key_tmp" | awk -F: '$1=="fpr" {print $10}' | grep -qx '9DC858229FC7DD38854AE2D88D81803C0EBFCD88' || fail 'Unexpected Docker signing key fingerprint.'
install -m 0644 "$key_tmp" /etc/apt/keyrings/docker.asc
rm -f "$key_tmp"
printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/%s %s stable\n' "$(dpkg --print-architecture)" "$ID" "$VERSION_CODENAME" > /etc/apt/sources.list.d/hivemind-docker.list
apt-get update
apt-get install -y --no-install-recommends docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
# Keep Docker's forwarding rules. This separate user chain checks original DNAT ports.
cat > /usr/local/sbin/hivemind-docker-firewall <<'FIREWALL'
#!/usr/bin/env bash
set -Eeuo pipefail
mapfile -t uplinks < <({ ip -o -4 route show default; ip -o -6 route show default; } | awk '{for(i=1;i<=NF;i++)if($i=="dev")print $(i+1)}' | sort -u)
[[ ${#uplinks[@]} -gt 0 ]] || { echo 'No default-route interface; Docker firewall not installed.' >&2; exit 1; }
for command in iptables ip6tables; do
  if ! "$command" -w -nL DOCKER-USER >/dev/null 2>&1; then
    [[ "$command" == ip6tables ]] && continue
    echo 'Docker DOCKER-USER chain missing; unsupported firewall backend.' >&2; exit 1
  fi
  "$command" -w -N HVM-PUBLISHED 2>/dev/null || true
  "$command" -w -F HVM-PUBLISHED
  "$command" -w -A HVM-PUBLISHED -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
  for interface in "${uplinks[@]}"; do
    "$command" -w -A HVM-PUBLISHED -i "$interface" -p tcp -m conntrack --ctstate NEW --ctorigdstport 80 -j RETURN
    "$command" -w -A HVM-PUBLISHED -i "$interface" -p tcp -m conntrack --ctstate NEW --ctorigdstport 443 -j RETURN
    "$command" -w -A HVM-PUBLISHED -i "$interface" -p udp -m conntrack --ctstate NEW --ctorigdstport 443 -j RETURN
    "$command" -w -A HVM-PUBLISHED -i "$interface" -j DROP
  done
  "$command" -w -A HVM-PUBLISHED -j RETURN
  "$command" -w -C DOCKER-USER -j HVM-PUBLISHED 2>/dev/null || "$command" -w -I DOCKER-USER 1 -j HVM-PUBLISHED
done
FIREWALL
chmod 0755 /usr/local/sbin/hivemind-docker-firewall
cat > /etc/systemd/system/hivemind-docker-firewall.service <<'UNIT'
[Unit]
Description=Restrict externally published Docker ports to Hivemind HTTPS and ACME
Requires=docker.service
After=docker.service network-online.target
PartOf=docker.service
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/hivemind-docker-firewall
RemainAfterExit=yes
[Install]
WantedBy=docker.service
UNIT
systemctl daemon-reload
systemctl enable --now hivemind-docker-firewall.service
# Install SSH allowance first. Do not change sshd_config or disable the active login.
ufw allow "$ssh_port/tcp" comment 'Existing SSH port'
ufw allow 80/tcp comment 'ACME HTTP challenge'
ufw allow 443/tcp comment 'Hivemind HTTPS'
ufw allow 443/udp comment 'HTTP3'
ufw default deny incoming
ufw default allow outgoing
ufw --force enable
/usr/local/sbin/hivemind-docker-firewall
mkdir -p /etc/fail2ban/jail.d
cat > /etc/fail2ban/jail.d/hivemind-sshd.local <<JAIL
[sshd]
enabled = true
backend = systemd
port = $ssh_port
maxretry = 5
findtime = 10m
bantime = 1h
banaction = ufw
JAIL
fail2ban-client -t
systemctl enable --now fail2ban
systemctl restart fail2ban
# Existing bootstrap owns digest pinning, strong role passwords and migration bookkeeping.
scripts/bootstrap.sh "$domain" "$email" "$image" selfhost
chmod 0755 deploy/caddy
chmod 0644 deploy/caddy/Caddyfile deploy/caddy/upstream.conf
docker pull redis:7.4-alpine
redis_image=$(docker image inspect redis:7.4-alpine --format '{{index .RepoDigests 0}}')
[[ "$redis_image" == *@sha256:* ]] || fail 'Redis image did not resolve to an immutable digest.'
export HVM_INFRA_REDIS_IMAGE="$redis_image"
python3 - <<'PY'
import os, secrets
from pathlib import Path
root = Path('deploy')
def update(path, values):
    lines = path.read_text().splitlines() if path.exists() else []
    lines = [line for line in lines if line.partition('=')[0] not in values]
    for key, value in values.items():
        if any(c in value for c in "'\n\r"):
            raise SystemExit(f'{key}: unsupported character in protected environment value')
        lines.append(f"{key}='{value}'")
    path.write_text('\n'.join(lines) + '\n'); path.chmod(0o600)
password = secrets.token_urlsafe(48)
path = root/'secrets/oauth_redis_password'; path.write_text(password + '\n'); path.chmod(0o600)
config = ('bind 0.0.0.0\nprotected-mode yes\nport 6379\nappendonly yes\n'
          'appendfsync everysec\nsave 900 1\ndir /data\nmaxmemory 192mb\n'
          'maxmemory-policy noeviction\nrequirepass ' + password + '\n')
path = root/'oauth-redis.conf'; path.write_text(config); path.chmod(0o644)
update(root/'versions.env', {'REDIS_IMAGE': os.environ['HVM_INFRA_REDIS_IMAGE']})
update(root/'host.env', {'COMPOSE_EXTRA': 'deploy/compose.infra.yml'})
values = {'OAUTH_REDIS_URL': 'redis://oauth-redis:6379/0', 'OAUTH_REDIS_PASSWORD': password}
for key in ('STRIPE_SECRET_KEY','STRIPE_WEBHOOK_SECRET','STRIPE_PRICE_STARTER','STRIPE_PRICE_TEAM',
            'STRIPE_LIVEMODE','STRIPE_PORTAL_CONFIGURATION'):
    if os.getenv(key): values[key] = os.environ[key]
update(root/'runtime.env', values)
PY
cat > deploy/compose.infra.yml <<'COMPOSE'
services:
  caddy:
    user: '10001:10001'
    cap_drop: [ALL]
    cap_add: [NET_BIND_SERVICE]
    security_opt: [no-new-privileges:true]
    read_only: true
    tmpfs: ['/tmp:size=16m,mode=1777']
  oauth-redis:
    image: ${REDIS_IMAGE:?Missing Redis digest}
    user: '999:999'
    restart: unless-stopped
    networks: [backend]
    volumes: [oauth_redis_data:/data, ./deploy/oauth-redis.conf:/usr/local/etc/redis/redis.conf:ro]
    command: [redis-server, /usr/local/etc/redis/redis.conf]
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    read_only: true
    tmpfs: ['/tmp:size=16m,mode=1777']
    mem_limit: 256m
    cpus: 0.5
    healthcheck:
      test: [CMD-SHELL, 'REDISCLI_AUTH=$$(sed -n "s/^requirepass //p" /usr/local/etc/redis/redis.conf) redis-cli ping | grep -qx PONG']
      interval: 5s
      timeout: 3s
      retries: 20
    logging:
      driver: local
      options: {max-size: 10m, max-file: '3'}
volumes:
  oauth_redis_data:
COMPOSE
# Volume ownership initialization is a short-lived root operation; runtime users are fixed.
scripts/compose.sh run --rm --no-deps --user 0:0 --cap-add CHOWN --cap-add FOWNER --cap-add DAC_OVERRIDE --entrypoint /bin/sh caddy -c 'chown -R 10001:10001 /data /config'
scripts/compose.sh run --rm --no-deps --user 0:0 --cap-add CHOWN --cap-add FOWNER --cap-add DAC_OVERRIDE --entrypoint /bin/sh oauth-redis -c 'chown -R 999:999 /data'
scripts/compose.sh config --quiet
scripts/compose.sh up -d --wait --wait-timeout 60 oauth-redis
scripts/deploy.sh "$image"
# Restart the active color only; the inactive color must not receive production traffic.
cat > deploy/start_stack.sh <<'START'
#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."
source deploy/state.env
[[ "$ACTIVE_COLOR" == blue || "$ACTIVE_COLOR" == green ]] || exit 1
scripts/compose.sh --profile selfhost-db up -d --wait --wait-timeout 90 postgres oauth-redis
scripts/compose.sh up -d --wait --wait-timeout 90 "$ACTIVE_COLOR" worker caddy
START
chmod 0700 deploy/start_stack.sh
cat > /etc/systemd/system/hivemind-stack.service <<UNIT
[Unit]
Description=Hivemind Scale active application, workers and storage
Requires=docker.service hivemind-docker-firewall.service
After=docker.service hivemind-docker-firewall.service network-online.target
[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$root
ExecStart=$root/deploy/start_stack.sh
TimeoutStartSec=240
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable hivemind-stack.service
{
  printf 'Provisioned at: '; date -u +%FT%TZ
  printf 'OS: %s %s\nDomain: %s\nApplication: %s\n' "$ID" "$VERSION_ID" "$domain" "$image"
  docker version --format 'Docker server: {{.Server.Version}}'
  docker compose version
  dpkg-query -W docker-ce docker-compose-plugin ufw fail2ban
  scripts/compose.sh ps
  ufw status
  fail2ban-client status sshd
} > deploy/infra-evidence.txt
printf 'Provisioned %s; HTTPS readiness passed. Evidence: deploy/infra-evidence.txt\n' "$domain"
printf 'OAuth hosted clients still require identity-provider registration and exact redirect allowlists.\n'
