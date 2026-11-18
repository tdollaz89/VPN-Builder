#!/usr/bin/env python3
"""Build a WireGuard, OpenVPN or SSH VPN tunnel: pick the VPN, then answer its questions.

Runs the same builders as wg-build.py, ovpn-build.py and ssh-build.py (which still work on their own).

Requires: python3, plus the chosen VPN's tools:
  WireGuard: wireguard-tools   (Ubuntu: apt install wireguard-tools)
  OpenVPN:   easy-rsa, openvpn (Ubuntu: apt install easy-rsa openvpn)
  SSH VPN:   openssh (ssh-keygen)
"""
import argparse
import importlib.util
import sys
from pathlib import Path

from common import BuildError, hand_back, menu

HERE = Path(__file__).resolve().parent
BUILDERS = {
    "wireguard": ("WireGuard", "wg-build.py"),
    "openvpn": ("OpenVPN", "ovpn-build.py"),
    "ssh": ("SSH VPN", "ssh-build.py"),
}


def load(filename):
    """Import a builder script by path (its file name has a dash, so plain import won't do)."""
    spec = importlib.util.spec_from_file_location(filename.replace("-", "_")[:-3], HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-dir",
                        help="parent directory for tunnel folders "
                             "(default: /opt/wireguard, /opt/openvpn or /opt/sshvpn)")
    args = parser.parse_args()

    print("VPN tunnel builder\n")
    vpn = menu("Which VPN?", [(key, label) for key, (label, _) in BUILDERS.items()])
    label, filename = BUILDERS[vpn]
    builder = load(filename)
    print(f"\n{label} tunnel builder\n")
    builder.build(Path(args.base_dir or builder.DEFAULT_BASE_DIR))


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
