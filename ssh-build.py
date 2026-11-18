#!/usr/bin/env python3
"""Build an SSH VPN: a point-to-point layer 3 tunnel carried by `ssh -w` between two tun devices.

Mirrors the manual workflow:
  server: ip_forward, PermitTunnel + ClientAlive in sshd, a persistent tun owned by the
          SSH user (systemd-networkd .netdev + networkd-dispatcher script), nftables NAT
  client: tun device, static route to the server, `ssh -fqNTw`, default route through the
          tunnel, public DNS; then undo it all on the way down

Writes a server/ and client/ folder, each with an install.sh. The client's SSH key is made
on the client by client/install.sh, so the private key never leaves that machine.
SSH VPNs are point-to-point only: one `ssh -w` links exactly one tun device to one other.

Requires: python3. Server: systemd-networkd, networkd-dispatcher. Client: openssh-client.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

from common import (BuildError, Progress, address, ask, choice, claim, hand_back, ifname, port,
                    tunnel_name, write_private)

DEFAULT_BASE_DIR = "/opt/sshvpn"
KEEPALIVE_INTERVAL = 10
KEEPALIVE_COUNT = 6
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]*\$?$")
TUN_RE = re.compile(r"^tun(\d+)$")


# ---------- validators ----------

def tun_dev(val):
    """ssh -w takes tun numbers, so the device has to be tunN."""
    if not TUN_RE.match(val):
        raise ValueError("ssh -w needs a tun device named tunN (e.g. tun0).")
    return val


def username(val):
    if not USER_RE.match(val):
        raise ValueError("Not a valid Linux user name.")
    return val


def tun_number(dev):
    return TUN_RE.match(dev).group(1)


def render(template, **values):
    """Fill {{key}} placeholders (bash uses ${...}, so str.format would trip over it)."""
    for key, val in values.items():
        template = template.replace("{{" + key + "}}", str(val))
    return template


def write_file(path, text, mode=0o600):
    write_private(path, text)
    os.chmod(path, mode)


# ---------- server files ----------

SYSCTL = """\
# {{name}} SSH VPN: forward traffic arriving on {{server_dev}}
net.ipv4.ip_forward=1
"""

SSHD = """\
# {{name}} SSH VPN: allow tun forwarding, drop dead clients after ~{{dead_after}}s
PermitTunnel yes
ClientAliveInterval {{interval}}
ClientAliveCountMax {{count}}
"""

NETDEV = """\
[NetDev]
Name={{server_dev}}
Kind=tun

[Tun]
User={{server_user}}
Group={{server_user}}
"""

DISPATCHER = """\
#!/bin/bash
# {{name}} SSH VPN: address {{server_dev}} whenever networking comes up
/sbin/ip address replace {{server_ip}}/32 peer {{client_ip}} dev {{server_dev}}
/sbin/ip link set dev {{server_dev}} up
"""

NFTABLES = """\
#!/usr/sbin/nft -f
# {{name}} SSH VPN server firewall. This replaces the WHOLE ruleset: merge it by hand if
# this server already has rules (e.g. another VPN's port, or its own NAT/forward rules).

flush ruleset

table inet filter {
        chain input {
                type filter hook input priority filter; policy drop;
                ct state established,related accept
                ct state invalid drop
                iif "lo" accept
                ip protocol icmp accept
                tcp dport {{port}} counter accept
                log prefix "nftables input dropped: "
        }
        chain forward {
                type filter hook forward priority filter; policy drop;
                ct state established,related accept
                ct state invalid drop
                iifname "{{server_dev}}" oifname "{{wan}}" counter accept
                log prefix "nftables forward dropped: "
        }
        chain output {
                type filter hook output priority filter; policy drop;
                ct state established,related accept
                ct state invalid drop
                oif "lo" accept
                ip protocol icmp accept
                udp dport { 53, 123 } counter accept
                tcp dport { 80, 443 } counter accept
                log prefix "nftables output dropped: "
        }
}
table inet nat {
        chain prerouting {
                type nat hook prerouting priority dstnat; policy accept;
        }
        chain postrouting {
                type nat hook postrouting priority srcnat; policy accept;
                oifname "{{wan}}" counter masquerade
        }
}
"""

SERVER_INSTALL = """\
#!/bin/bash
# {{name}} SSH VPN: server side. Run on the server with sudo from inside this folder.
set -euo pipefail
[ "$EUID" -eq 0 ] || { echo "Run with sudo."; exit 1; }
cd "$(dirname "$0")"
id {{server_user}} >/dev/null 2>&1 || { echo "User {{server_user}} does not exist here."; exit 1; }

echo "[1/4] IP forwarding"
install -m 644 sysctl.conf /etc/sysctl.d/99-{{name}}-ssh-vpn.conf
sysctl -q -p /etc/sysctl.d/99-{{name}}-ssh-vpn.conf

echo "[2/4] sshd: PermitTunnel + keepalive"
install -m 644 sshd.conf /etc/ssh/sshd_config.d/{{name}}-ssh-vpn.conf
sshd -t
systemctl restart ssh 2>/dev/null || systemctl restart sshd

echo "[3/4] {{server_dev}} device owned by {{server_user}}"
install -m 644 {{server_dev}}.netdev /etc/systemd/network/{{server_dev}}.netdev
mkdir -p /etc/networkd-dispatcher/routable.d
install -m 755 99-{{server_dev}} /etc/networkd-dispatcher/routable.d/99-{{server_dev}}
networkctl reload
for _ in 1 2 3 4 5; do ip link show {{server_dev}} >/dev/null 2>&1 && break; sleep 1; done
/etc/networkd-dispatcher/routable.d/99-{{server_dev}}

echo "[4/4] check"
ip address show dev {{server_dev}}

echo
echo "Next: on the client, run client/install.sh. It makes the SSH key and tries to authorize it"
echo "here over a password login; if that is off, copy its .pub here and run: sudo ./authorize-key.sh <file>.pub"
echo "Firewall/NAT is NOT applied automatically (nftables.conf replaces the whole ruleset)."
echo "Review it, then:  sudo cp nftables.conf /etc/nftables.conf && sudo nft -f /etc/nftables.conf"
"""


AUTHORIZE = """\
#!/bin/bash
# {{name}} SSH VPN: authorize a client's public key for {{server_user}}, locked to {{server_dev}}.
# Usage: sudo ./authorize-key.sh {{name}}_ed25519.pub
set -euo pipefail
[ "$EUID" -eq 0 ] || { echo "Run with sudo."; exit 1; }
[ $# -eq 1 ] && [ -f "$1" ] || { echo "Usage: sudo $0 <client public key file>"; exit 1; }
key=$(head -n1 "$1")
case "$key" in ssh-*|ecdsa-*) ;; *) echo "$1 does not look like an SSH public key."; exit 1 ;; esac
line='{{restrict}}'" $key"

home=$(getent passwd {{server_user}} | cut -d: -f6)
group=$(id -gn {{server_user}})
install -d -m 700 -o {{server_user}} -g "$group" "$home/.ssh"
touch "$home/.ssh/authorized_keys"
if grep -qF "$key" "$home/.ssh/authorized_keys"; then
    echo "Key already authorized."
else
    echo "$line" >> "$home/.ssh/authorized_keys"
    echo "Key authorized for {{server_user}} (tunnel {{server_dev}} only)."
fi
chown {{server_user}}:"$group" "$home/.ssh/authorized_keys"
chmod 600 "$home/.ssh/authorized_keys"
"""


# ---------- client files ----------

SSH_CONFIG = """\
Host {{name}}
    Hostname {{remote}}
    Port {{port}}
    User {{server_user}}
    IdentityFile ~/.ssh/{{name}}_ed25519
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new
    ServerAliveInterval {{interval}}
    ServerAliveCountMax {{count}}
"""

UP = """\
#!/bin/bash
# Bring up the {{name}} SSH VPN: {{client_dev}} {{client_ip}} <-> {{server_dev}} {{server_ip}} on {{remote}}
# Run as your normal user (not sudo): ssh uses your key; sudo is used for network changes.
set -euo pipefail
NAME={{name}}
TUN={{client_dev}}
LOCAL_IP={{client_ip}}
PEER_IP={{server_ip}}
SERVER={{remote}}
SSH_W={{client_tun}}:{{server_tun}}
DNS={{dns}}
ROUTE_MODE={{route_mode}}
STATE=/run/$NAME-ssh-vpn.state
DOWN="$(dirname "$(readlink -f "$0")")/$NAME-down"

[ "$EUID" -ne 0 ] || { echo "Run as your normal user, not root/sudo."; exit 1; }
if ip link show "$TUN" >/dev/null 2>&1; then
    echo "$TUN already exists; is the VPN already up? Run $NAME-down first."; exit 1
fi

read -r GW DEV < <(ip -4 route show default | awk '{for (i = 1; i < NF; i++) {
    if ($i == "via") g = $(i + 1); if ($i == "dev") d = $(i + 1) } print g, d; exit}')
[ -n "${GW:-}" ] && [ -n "${DEV:-}" ] || { echo "No default route found."; exit 1; }
SERVER_IP=$(getent ahostsv4 "$SERVER" | awk '{print $1; exit}')
[ -n "$SERVER_IP" ] || { echo "Cannot resolve $SERVER."; exit 1; }
OLD_DNS=""
if command -v resolvectl >/dev/null; then
    OLD_DNS=$(resolvectl dns "$DEV" 2>/dev/null | sed 's/^[^:]*: *//')
fi

echo "Your current IP: $(curl -s --max-time 5 ipinfo.io/ip || echo unknown)"
printf 'GW=%q\\nDEV=%q\\nSERVER_IP=%q\\nOLD_DNS=%q\\nROUTE_MODE=%q\\n' \\
    "$GW" "$DEV" "$SERVER_IP" "$OLD_DNS" "$ROUTE_MODE" | sudo tee "$STATE" >/dev/null

trap 'echo "Tunnel failed to come up; cleaning up."; "$DOWN" --quiet' ERR

sudo ip tuntap add dev "$TUN" mode tun user "$USER" group "$(id -gn)"
sudo ip address add "$LOCAL_IP/32" peer "$PEER_IP" dev "$TUN"
sudo ip link set dev "$TUN" up
# keep the ssh connection itself off the tunnel
sudo ip route replace "$SERVER_IP/32" via "$GW" dev "$DEV"

ssh -fqNT -w "$SSH_W" "$NAME"
for _ in $(seq 10); do ping -c1 -W1 "$PEER_IP" >/dev/null 2>&1 && break; sleep 1; done
ping -c1 -W2 "$PEER_IP" >/dev/null

if [ "$ROUTE_MODE" = replace ]; then
    sudo ip route replace default via "$PEER_IP" dev "$TUN"
else
    sudo ip route add 0.0.0.0/1 via "$PEER_IP" dev "$TUN"
    sudo ip route add 128.0.0.0/1 via "$PEER_IP" dev "$TUN"
fi
if command -v resolvectl >/dev/null; then
    sudo resolvectl dns "$DEV" $DNS
    sudo resolvectl flush-caches
fi
trap - ERR

echo "Tunnel up: $TUN $LOCAL_IP -> $PEER_IP"
echo "Your new IP: $(curl -s --max-time 5 ipinfo.io/ip || echo unknown)"
"""

DOWN = """\
#!/bin/bash
# Take down the {{name}} SSH VPN and restore routes and DNS.
NAME={{name}}
TUN={{client_dev}}
SSH_W={{client_tun}}:{{server_tun}}
STATE=/run/$NAME-ssh-vpn.state
QUIET=${1:-}

[ "$QUIET" = --quiet ] || echo "Your current IP: $(curl -s --max-time 5 ipinfo.io/ip || echo unknown)"
GW="" DEV="" SERVER_IP="" OLD_DNS="" ROUTE_MODE=""
[ -f "$STATE" ] && source "$STATE"

# only this tunnel's ssh process, not every ssh session
pkill -u "$(id -u "${SUDO_USER:-$USER}")" -f "ssh -fqNT -w $SSH_W $NAME\\$" 2>/dev/null
# deleting the device also drops the routes through it
sudo ip link del dev "$TUN" 2>/dev/null
if [ -n "$SERVER_IP" ]; then sudo ip route del "$SERVER_IP/32" 2>/dev/null; fi
if [ "$ROUTE_MODE" = replace ] && [ -n "$GW" ]; then
    sudo ip route replace default via "$GW" dev "$DEV"
fi
if [ -n "$DEV" ] && command -v resolvectl >/dev/null; then
    if [ -n "$OLD_DNS" ]; then sudo resolvectl dns "$DEV" $OLD_DNS; else sudo resolvectl revert "$DEV"; fi
    sudo resolvectl flush-caches
fi
sudo rm -f "$STATE"

[ "$QUIET" = --quiet ] || echo "Tunnel down. Your IP: $(curl -s --max-time 5 ipinfo.io/ip || echo unknown)"
"""

CLIENT_INSTALL = """\
#!/bin/bash
# {{name}} SSH VPN: client side. Run as your normal user from inside this folder.
set -euo pipefail
[ "$EUID" -ne 0 ] || { echo "Run as your normal user, not root/sudo."; exit 1; }
cd "$(dirname "$0")"

KEY=~/.ssh/{{name}}_ed25519

install -d -m 700 ~/.ssh
if [ -f "$KEY" ]; then
    echo "Using existing key $KEY"
else
    ssh-keygen -q -t ed25519 -N "" -C "{{name}}-ssh-vpn@$(hostname)" -f "$KEY"
    echo "Created key $KEY (the private key stays on this machine)"
fi
touch ~/.ssh/config && chmod 600 ~/.ssh/config
if grep -qE '^Host {{name}}$' ~/.ssh/config; then
    echo "~/.ssh/config already has 'Host {{name}}'; left it alone."
else
    { echo; cat ssh_config; } >> ~/.ssh/config
fi
sudo install -m 755 {{name}}-up {{name}}-down /usr/local/bin/

# authorize the key on the server, locked to the tunnel. Needs a password login once.
LINE='{{restrict}}'" $(cat "$KEY.pub")"
echo
echo "Authorizing the key on {{remote}} as {{server_user}} (enter that user's password if asked)..."
if printf '%s\\n' "$LINE" | ssh -o PreferredAuthentications=password,keyboard-interactive {{name}} \\
    'umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; read -r line;
     grep -qF "${line#* }" ~/.ssh/authorized_keys || printf "%s\\n" "$line" >> ~/.ssh/authorized_keys'; then
    echo "Key authorized."
else
    echo "Could not log in with a password. Copy $KEY.pub to the server's {{name}}/server folder and run:"
    echo "  sudo ./authorize-key.sh {{name}}_ed25519.pub"
fi

echo
echo "Installed. Bring the VPN up with: {{name}}-up    and down with: {{name}}-down"
"""


# ---------- main ----------

def new_tunnel(base_dir):
    cfg = {"topology": "p2p"}
    cfg["name"] = ask("Tunnel name", validate=tunnel_name)
    tunnel_dir = base_dir / cfg["name"]
    if tunnel_dir.exists():
        sys.exit(f"{tunnel_dir} already exists; refusing to overwrite an existing tunnel.")

    cfg["server_ip"] = ask("Server tunnel IP", validate=address)
    while True:
        cfg["client_ip"] = ask("Client tunnel IP", cfg["server_ip"] + 1, address)
        if cfg["client_ip"] != cfg["server_ip"]:
            break
        print("  Client IP must differ from the server IP.")
    cfg["server_dev"] = ask("Server tun device (tunN)", validate=tun_dev)
    cfg["client_dev"] = ask("Client tun device (tunN)", validate=tun_dev)
    cfg["remote"] = ask("Server public IP/hostname")
    cfg["port"] = ask("Server SSH port", 22, port)
    cfg["server_user"] = ask("SSH user on the server", validate=username)
    cfg["wan"] = ask("Server internet-facing interface (for NAT)", "ens33", ifname)
    cfg["route_mode"] = ask("Send all client traffic via (hijack/replace)", "hijack",
                            choice(["hijack", "replace"]))
    cfg["dns"] = ask("DNS server while the VPN is up", "1.1.1.1", address)

    try:
        if not base_dir.exists():
            base_dir.mkdir(parents=True)
            claim(base_dir, recursive=False)
        tunnel_dir.mkdir(mode=0o700)
        claim(tunnel_dir)
        server_dir = tunnel_dir / "server"
        client_dir = tunnel_dir / "client"
        server_dir.mkdir(mode=0o700)
        client_dir.mkdir(mode=0o700)
    except PermissionError:
        sys.exit(f"Cannot write to {base_dir}. Re-run with sudo or use --base-dir.")

    name = cfg["name"]
    values = dict(cfg, interval=KEEPALIVE_INTERVAL, count=KEEPALIVE_COUNT,
                  dead_after=KEEPALIVE_INTERVAL * KEEPALIVE_COUNT,
                  server_tun=tun_number(cfg["server_dev"]),
                  client_tun=tun_number(cfg["client_dev"]))
    # the client's key may open this tunnel's tun device and nothing else
    values["restrict"] = (f'tunnel="{values["server_tun"]}",no-pty,no-agent-forwarding,'
                          "no-X11-forwarding,no-port-forwarding")

    print()
    with Progress(2) as bar:
        bar.step("Writing server files")
        server_files = {
            "install.sh": (SERVER_INSTALL, 0o700),
            "sysctl.conf": (SYSCTL, 0o600),
            "sshd.conf": (SSHD, 0o600),
            f"{cfg['server_dev']}.netdev": (NETDEV, 0o600),
            f"99-{cfg['server_dev']}": (DISPATCHER, 0o700),
            "nftables.conf": (NFTABLES, 0o600),
            "authorize-key.sh": (AUTHORIZE, 0o700),
        }
        for fname, (template, mode) in server_files.items():
            write_file(server_dir / fname, render(template, **values), mode)

        bar.step("Writing client files")
        client_files = {
            "install.sh": (CLIENT_INSTALL, 0o700),
            "ssh_config": (SSH_CONFIG, 0o600),
            f"{name}-up": (UP, 0o700),
            f"{name}-down": (DOWN, 0o700),
        }
        for fname, (template, mode) in client_files.items():
            write_file(client_dir / fname, render(template, **values), mode)

        meta = {k: str(v) if not isinstance(v, int) else v for k, v in cfg.items()}
        (tunnel_dir / "tunnel.json").write_text(json.dumps(meta, indent=2) + "\n")

    print(f"\nDone. Tunnel '{name}' written to {tunnel_dir}")
    print(f"  server: {server_dir}/  ({cfg['server_dev']}, {cfg['server_ip']})")
    print(f"  client: {client_dir}/  ({cfg['client_dev']}, {cfg['client_ip']})")
    print(f"\n1. Server ({cfg['remote']}): sudo {server_dir}/install.sh"
          f"\n   sshd must already listen on port {cfg['port']}; review nftables.conf before applying it."
          f"\n2. Client: copy client/ over, then  ./install.sh   (as your normal user)"
          f"\n   It makes the SSH key there and authorizes it on the server.")
    print(f"3. Client: VPN up: {name}-up    VPN down: {name}-down")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-dir", default=DEFAULT_BASE_DIR,
                        help=f"parent directory for tunnel folders (default: {DEFAULT_BASE_DIR})")
    args = parser.parse_args()
    print("SSH VPN tunnel builder\n")
    build(Path(args.base_dir))


def build(base_dir):
    print("SSH VPNs are point-to-point: one server, one client per tunnel.\n")
    new_tunnel(base_dir)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nAborted.")
    except BuildError as e:
        if e.output:
            sys.stderr.write(e.output.rstrip() + "\n")
        sys.exit(f"Error: {e}")
    finally:
        hand_back()
