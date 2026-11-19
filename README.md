# vpn-builder

Interactive builders for three kinds of VPN tunnel. You answer a few questions, and each builder generates the keys, certs and config files for one tunnel and writes them into a folder for that tunnel.

| VPN | Topologies | Script | Tunnels go in |
|---|---|---|---|
| WireGuard | subnet, p2p | `wg-build.py` | `/opt/wireguard/<name>/` |
| OpenVPN | subnet, p2p | `ovpn-build.py` | `/opt/openvpn/<name>/` |
| SSH VPN | p2p only | `ssh-build.py` | `/opt/sshvpn/<name>/` |

## Quick start

```bash
sudo ./vpn-build.py
```

```
VPN tunnel builder

Which VPN?
  1) WireGuard
  2) OpenVPN
  3) SSH VPN
```

`vpn-build.py` runs the builder you choose. Each builder can also be run on its own, e.g. `sudo ./wg-build.py`.

- `sudo` is needed only because `/opt` is owned by root. When the run finishes, the files are handed back to your user so you can browse them without sudo.
- `--base-dir DIR` writes tunnels somewhere else, e.g. `./vpn-build.py --base-dir ~/tunnels` (then no sudo is needed).
- A builder refuses to overwrite an existing tunnel folder.

## Requirements

- Python 3 (standard library only)
- Only the tools for the VPN you pick (Ubuntu package names):
  - WireGuard: `wireguard-tools`
  - OpenVPN: `easy-rsa` and `openvpn` 2.5 or newer
  - SSH VPN: nothing extra on the machine you build on. The client needs `openssh-client`. The server needs `systemd-networkd` and `networkd-dispatcher`.

---

## WireGuard

**Build a new tunnel.** It asks for:
- tunnel name and topology
- for subnet: tunnel network, server interface name, server public IP, ListenPort, number of clients, and a client interface name
- for p2p: server and client tunnel IPs, and server and client interface names

`wg-quick` names the interface after the config file, so every config is called `<interface>.conf`. In subnet mode, all clients use the one interface name you type (e.g. `wg0`), and each client gets its own folder so the files don't overwrite each other:

```
/opt/wireguard/republic/
├── wg0.conf                 server
├── republic-client1/wg0.conf
├── republic-client2/wg0.conf
├── keys/                    server.key/.pub, client1.key/.pub, ...
└── tunnel.json              lets "add clients" extend the tunnel later
```

The summary at the end lists every client with its tunnel IP.

**Deploy.** Copy the server's config to `/etc/wireguard/` on the server. Copy each client's folder to that client and put its config in `/etc/wireguard/`. Then run `sudo wg-quick up wg0` on each.

**Add clients** (subnet tunnels only). Pick "Add clients to an existing tunnel". The builder:
- gives the new clients the next free IPs
- puts them in new client folders
- appends a `[Peer]` block for each one to the server config

Copy the updated server config to the server and load it without dropping the connected peers:

```bash
wg syncconf wg0 <(wg-quick strip wg0)
```

## OpenVPN

**Build a new tunnel.** It asks for:
- tunnel name and topology
- for subnet: network, server tun device name, server public IP, protocol (default tcp), port (default 443), and number of clients
- for p2p: server and client tunnel IPs, server and client tun device names, server public IP, protocol (default udp), and port (default 30001)

Each tunnel gets its own CA (EC keys, sha384, no passphrase) and tls-crypt-v2 keys. Every cert and key is inlined into the configs, so each `.conf` is a single file you can copy over:

```
/opt/openvpn/world/
├── world-server.conf
├── world-client1.conf ...
├── pki/          easy-rsa CA, issued certs, private keys
├── tls/          tls-crypt-v2 server and client keys
└── tunnel.json
```

**Deploy.** The configs `chroot` into a jail and drop to `nobody`. Create the jail on the server, and on the client too for p2p tunnels:

```bash
sudo mkdir -p /etc/openvpn/<name>-jail/tmp
sudo chmod 1777 /etc/openvpn/<name>-jail/tmp
```

**Add clients** (subnet tunnels only). The builder issues a new cert and tls-crypt-v2 key for each client. The server config does not change, because it accepts any cert named `<name>-client*`, so there's nothing to redeploy on the server.

## SSH VPN

This is a layer 3 tunnel made with `ssh -w`. One SSH connection links one tun device on the client to one tun device on the server. That's why SSH VPNs are point-to-point only and have no "add clients" option.

The builder is meant to be run **on the server**. It asks for:
- tunnel name
- server and client tunnel IPs
- server and client tun devices (must be `tunN`, because `ssh -w` uses the number)
- server public IP and SSH port
- the SSH user on the server
- whether to change the client's default gateway (default **no**). If you answer yes, it also asks:
  - the method, `hijack` or `replace`
  - the DNS server to use while the VPN is up (default `1.1.1.1`)

```
/opt/sshvpn/fortune/
├── server/
│   ├── install.sh          run with sudo on the server
│   ├── authorize-key.sh    authorizes the client's key (paste it in)
│   ├── sysctl.conf         net.ipv4.ip_forward=1
│   ├── sshd.conf           PermitTunnel yes + ClientAlive keepalive
│   ├── tun0.netdev         persistent tun0 owned by the SSH user
│   └── 99-tun0             networkd-dispatcher script that addresses tun0
├── client/
│   ├── install.sh          run as your normal user on the client
│   ├── ssh_config          "Host fortune": tunnel login with keepalives
│   ├── fortune-up
│   └── fortune-down
└── tunnel.json
```

### Deploy

1. **Server**, from the tunnel folder:
   ```bash
   sudo ./server/install.sh
   ```
   This installs the sysctl, sshd and networkd files into their system locations, restarts sshd and brings up the tun device.
   - **Port:** sshd must already be listening on the port you entered; the installer doesn't change it.
   - **Firewall/NAT:** not handled here. The server's firewall must let traffic from the tun device be forwarded and masqueraded out to the internet, or the client gets a tunnel but no internet through it.

2. **Client.** Copy `client/` over, then run it as your normal user (not sudo):
   ```bash
   ./install.sh
   ```
   It:
   - **creates the SSH key on the client** (`~/.ssh/<name>_ed25519`), so the private key never leaves that machine
   - adds a `Host <name>` block to `~/.ssh/config` that logs in with only that key
   - installs `<name>-up` and `<name>-down` into `/usr/local/bin`
   - prints the public key (one `ssh-ed25519 ...` line) for you to copy

3. **Server: authorize the key.** In the tunnel's `server/` folder:
   ```bash
   sudo ./authorize-key.sh
   ```
   Paste the line when asked. It's added to the SSH user's `authorized_keys`, locked to this tunnel's tun device: no shell and no port forwarding. A partial or garbled paste is rejected, and pasting the same key twice adds it only once. If you have the `.pub` file on the server, you can give it instead: `sudo ./authorize-key.sh fortune_ed25519.pub`.

   Copying the public key across by hand is safe because it isn't secret. Some step like this is unavoidable: the server only accepts a new key from someone who can already log in (a password, an existing key, or you on the server).

4. **Use it**, on the client as your normal user:
   ```bash
   fortune-up      # with the gateway changed, prints your public IP before and after
   fortune-down
   ```

### What up and down do

**Default gateway left alone (the default).** This is a split tunnel. The client keeps using its own gateway (`eth0`, `wlan0`, ...) for the internet, and only traffic addressed through the tunnel goes into it. To start with, that's just the server's tunnel IP. To reach networks behind the server, add a route through the tunnel, e.g. `sudo ip route add 172.x.6.0/24 via 172.x.5.1 dev tun0`. The server must also forward that traffic and have a way to send replies back.

`<name>-up`:
1. Creates the tun device and gives it an address.
2. Runs `ssh -fqNT -w 0:0 <name>` and waits until the far end answers a ping.
3. If any step fails, runs `<name>-down` to clean up.

`<name>-down` kills only this tunnel's ssh process (not your other SSH sessions) and deletes the tun device.

**Default gateway changed.** All of the client's traffic goes out through the server, as in the lesson's "SSH VPN to proxy web traffic" section. `<name>-up` does the steps above, plus:
- **Before:** saves your gateway, interface and DNS server to `/run/<name>-ssh-vpn.state`.
- **Route to the server:** adds a static route to the server, so the SSH connection itself stays off the tunnel.
- **After the tunnel is up:** moves the default route and switches DNS so lookups don't leak:
  - **hijack** (default) adds `0.0.0.0/1` and `128.0.0.0/1` routes through the tunnel. These are more specific than your default route, and they disappear when the tunnel does.
  - **replace** swaps your default route for one through the tunnel.

`<name>-down` also removes the static route, puts the original default route back (replace mode) and restores your DNS server.

---

## Files in this repo

| File | What it is |
|---|---|
| `vpn-build.py` | Entry point: choose a VPN, then runs that builder |
| `wg-build.py` | WireGuard builder |
| `ovpn-build.py` | OpenVPN builder |
| `ssh-build.py` | SSH VPN builder |
| `common.py` | Shared prompts, validators, progress bar, and the sudo file hand-back |
