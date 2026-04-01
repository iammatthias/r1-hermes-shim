#!/usr/bin/env python3
import json
import os
import socket
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
QR_DIR = ROOT / 'qr'
QR_DIR.mkdir(exist_ok=True)
PORT = int(os.environ.get('R1_SHIM_PORT', '18789'))
TOKEN = os.environ.get('R1_SHIM_TOKEN', '')
TAILSCALE_HOST = os.environ.get('R1_SHIM_TS_HOST', '')


def get_ips():
    ips = []
    try:
        out = subprocess.check_output(['ifconfig'], text=True)
        for line in out.splitlines():
            line = line.strip()
            if line.startswith('inet '):
                ip = line.split()[1]
                if ip.startswith('127.') or ip.startswith('169.254.'):
                    continue
                ips.append(ip)
    except Exception:
        pass
    # dedupe preserve order
    seen = set()
    out = []
    for ip in ips:
        if ip not in seen:
            seen.add(ip)
            out.append(ip)
    return out


payload = {
    'type': 'clawdbot-gateway',
    'version': 1,
    'ips': get_ips(),
    'port': PORT,
    'token': TOKEN,
    'protocol': 'ws',
}
if TAILSCALE_HOST:
    payload['ips'].append(TAILSCALE_HOST)

payload_text = json.dumps(payload, separators=(',', ':'))
(ROOT / 'gateway_payload.json').write_text(json.dumps(payload, indent=2))
print(payload_text)
