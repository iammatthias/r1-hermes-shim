#!/usr/bin/env python3
# Idempotent re-application of the R1 shim source patch onto a Hermes checkout.
# The combined patch (config.py + run.py) is pinned to a tag's line context, so it
# stops applying after upstream refactors. This anchors on stable strings instead.
# Usage: sudo python3 r1-port.py /usr/local/lib/hermes-agent
import sys, os

L = sys.argv[1] if len(sys.argv) > 1 else "/usr/local/lib/hermes-agent"
CFG = os.path.join(L, "gateway/config.py")
RUN = os.path.join(L, "gateway/run.py")
AUTHZ = os.path.join(L, "gateway/authz_mixin.py")
changed = []


def patch(path, fn):
    s = open(path).read()
    ns = fn(s)
    if ns != s:
        open(path, "w").write(ns)
        changed.append(os.path.relpath(path, L))


def cfg(s):
    # 1) Platform enum member
    if 'R1_SHIM = "r1_shim"' not in s:
        a = '    WECOM_CALLBACK = "wecom_callback"\n'
        assert a in s, "config.py: enum anchor (WECOM_CALLBACK) not found"
        s = s.replace(a, a + '    R1_SHIM = "r1_shim"\n', 1)
    # 2) env-override block inside _apply_env_overrides
    if "R1_SHIM_ENABLED" not in s:
        block = (
            "\n"
            "    # R1 Shim (Rabbit R1 OpenClaw-compatible gateway)\n"
            '    r1_shim_enabled = os.getenv("R1_SHIM_ENABLED", "").lower() in {"true", "1", "yes"}\n'
            '    r1_shim_token = os.getenv("R1_SHIM_TOKEN", "")\n'
            '    r1_shim_port = os.getenv("R1_SHIM_PORT")\n'
            "    if r1_shim_enabled or r1_shim_token:\n"
            "        if Platform.R1_SHIM not in config.platforms:\n"
            "            config.platforms[Platform.R1_SHIM] = PlatformConfig()\n"
            "        config.platforms[Platform.R1_SHIM].enabled = True\n"
            "        if r1_shim_token:\n"
            '            config.platforms[Platform.R1_SHIM].extra["token"] = r1_shim_token\n'
            "        if r1_shim_port:\n"
            "            try:\n"
            '                config.platforms[Platform.R1_SHIM].extra["port"] = int(r1_shim_port)\n'
            "            except ValueError:\n"
            "                pass\n"
            "\n"
        )
        a = "    # Microsoft Graph webhook platform\n"
        assert a in s, "config.py: env-block anchor (Microsoft Graph webhook) not found"
        s = s.replace(a, block + a, 1)
    return s


def run(s):
    if "Platform.R1_SHIM:" not in s:
        a = "            return YuanbaoAdapter(config)\n"
        assert a in s, "run.py: dispatch anchor (YuanbaoAdapter) not found"
        block = (
            "\n"
            "        elif platform == Platform.R1_SHIM:\n"
            "            from gateway.platforms.r1_shim import R1ShimAdapter, check_r1_shim_requirements\n"
            "            if not check_r1_shim_requirements():\n"
            '                logger.warning("R1 Shim: aiohttp not installed")\n'
            "                return None\n"
            "            return R1ShimAdapter(config)\n"
        )
        s = s.replace(a, a + block, 1)
    return s


def authz(s):
    old = "if source.platform in {Platform.HOMEASSISTANT, Platform.WEBHOOK}:"
    new = "if source.platform in {Platform.HOMEASSISTANT, Platform.WEBHOOK, Platform.R1_SHIM}:"
    if "Platform.R1_SHIM" not in s:
        assert old in s, "authz_mixin.py: auth-bypass anchor not found"
        s = s.replace(old, new, 1)
    return s


patch(CFG, cfg)
patch(RUN, run)
patch(AUTHZ, authz)
print("R1 port changed:", ", ".join(changed) if changed else "nothing (already patched)")
