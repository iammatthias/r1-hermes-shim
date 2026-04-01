#!/usr/bin/env python3
"""Generate a pairing QR code for the R1 to scan.

Usage:
    R1_SHIM_TOKEN=<your-token> python3 make_qr.py

Requires: npx (Node.js) for QR generation, or pip install qrcode pillow
"""
import json, os, subprocess, sys

PORT = int(os.environ.get("R1_SHIM_PORT", "18789"))
TOKEN = os.environ.get("R1_SHIM_TOKEN", "")

if not TOKEN:
    print("Error: R1_SHIM_TOKEN environment variable is required", file=sys.stderr)
    sys.exit(1)


def get_lan_ips():
    """Get LAN IP addresses (macOS/Linux)."""
    ips = []
    try:
        out = subprocess.check_output(["ifconfig"], text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("inet "):
                ip = line.split()[1]
                if not ip.startswith("127.") and not ip.startswith("169.254."):
                    ips.append(ip)
    except Exception:
        pass
    if not ips:
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ips.append(s.getsockname()[0])
            s.close()
        except Exception:
            ips.append("127.0.0.1")
    return list(dict.fromkeys(ips))  # dedupe, preserve order


payload = {
    "type": "clawdbot-gateway",
    "version": 1,
    "ips": get_lan_ips(),
    "port": PORT,
    "token": TOKEN,
    "protocol": "ws",
}

payload_json = json.dumps(payload, separators=(",", ":"))
print("Payload:", payload_json)
print()

# Try npx qrcode first
try:
    subprocess.run(
        ["npx", "--yes", "qrcode", payload_json, "-o", "r1-pairing-qr.png"],
        check=True, capture_output=True,
    )
    print("QR saved to: r1-pairing-qr.png")
except Exception:
    # Fallback: try python qrcode library
    try:
        import qrcode
        img = qrcode.make(payload_json)
        img.save("r1-pairing-qr.png")
        print("QR saved to: r1-pairing-qr.png")
    except ImportError:
        print("Install qrcode: pip install qrcode pillow", file=sys.stderr)
        print("Or use npx: npx qrcode '{}'".format(payload_json))
        sys.exit(1)

# Also print to terminal if possible
try:
    subprocess.run(["npx", "--yes", "qrcode", payload_json], check=True)
except Exception:
    pass
