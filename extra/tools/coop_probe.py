#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Co-op probe: everything outside the game, in one run.

Answers "is it my network, my account or the client?" for Bloodborne seamless co-op
without starting the game:

  1. config      what the launcher and the host overrides make the game use
  2. network     DNS, TCP and RTT to the community server and the WebAPI
  3. ss.info     the game's own server list: are the summon URLs usable
  4. account     get / stale record / create as *your* UserId, then delete it
  5. relay       the outbound UDP relay on port 3478 (BBR1 register)
  6. local       firewall rules, clock skew against the server, proxy variables, route

Run it on the machine that plays the game:

    python3 extra/tools/coop_probe.py
    python3 extra/tools/coop_probe.py -p 1234 -a 242000 -l 60      # probe with your area
    python3 extra/tools/coop_probe.py --no-create                    # read-only

Exit code 0 when every critical check passes, 1 otherwise. Nothing is left on the
server: the probe's sign is deleted at the end unless --keep is given.
"""

import argparse
import base64
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

DEFAULT_COMMUNITY = "http://play.seamlesshunt.com:20443"
SS_INFO_PATH = "/bb-eu/ss.info"
DEFAULT_WEBAPI = "http://play.seamlesshunt.com:31315"
RELAY_HOST = "play.seamlesshunt.com"
RELAY_PORT = 3478

ROWS = []  # (phase, name, status, detail)


def row(phase, name, status, detail=""):
    ROWS.append((phase, name, status, detail))
    print(f"  [{status:4s}] {name}: {detail}" if detail else f"  [{status:4s}] {name}")


def data_dir():
    override = os.environ.get("BB_DATA_DIR")
    if override:
        return Path(override)
    return Path.home() / ".local" / "share" / "bbport"


def config_dir():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "bbport-launcher"


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def post_json(url, payload, timeout):
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), (time.time() - started) * 1000, response.headers
    except urllib.error.HTTPError as error:
        return error.code, error.read(), (time.time() - started) * 1000, error.headers
    except (OSError, ValueError) as error:
        return 0, str(error).encode(), (time.time() - started) * 1000, {}


def tcp_probe(host, port, timeout):
    started = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, (time.time() - started) * 1000
    except OSError:
        return False, (time.time() - started) * 1000


def sign_list(result):
    try:
        return json.loads(result).get("SummonDataList") or []
    except (ValueError, AttributeError):
        return []


def newest_log_user_id():
    logs = sorted(data_dir().glob("logs/*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    for log in logs[:5]:
        try:
            with log.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    found = re.search(r"summon_messenger/[a-z]+\?user_id=(\d+)", line)
                    if found:
                        return int(found.group(1)), log.name
        except OSError:
            continue
    return None, None


def phase_config(args):
    print("\n== config")
    settings = read_json(config_dir() / "settings.json") or {}
    overrides_path = data_dir() / "user" / "host_overrides.json"
    overrides = read_json(overrides_path) or {}

    if settings:
        row("config", "launcher settings", "OK", str(config_dir() / "settings.json"))
        for key in ("online", "online_backend", "online_pool", "online_community",
                    "online_server", "online_webapi", "online_upnp", "online_npid"):
            if key in settings:
                value = settings[key]
                if key == "online_password":
                    value = "*" * len(str(value))
                print(f"        {key:16s} {value}")
    else:
        row("config", "launcher settings", "WARN", f"not found at {config_dir() / 'settings.json'}")

    password = args.password if args.password is not None else str(settings.get("online_password") or "")
    server = args.server or str(settings.get("online_webapi") or "") or DEFAULT_WEBAPI
    community = args.community or str(settings.get("online_community") or "") or DEFAULT_COMMUNITY
    if "://" not in server:
        server = "http://" + server
    if "://" not in community:
        community = "http://" + community

    if overrides:
        summon_routes = {k: v for k, v in overrides.items() if "summon_messenger" in k}
        if summon_routes:
            source, target = next(iter(summon_routes.items()))
            # The override must land on the WebAPI the launcher configured, or the game's summon
            # calls go somewhere that cannot answer them (the old "everything to 0.0.0.0" trap).
            target_host = str(target).split("://", 1)[-1].split("/")[0]
            server_host = server.split("://", 1)[-1].split("/")[0]
            status = "OK" if target_host == server_host else "WARN"
            row("config", "summon route", status, f"{source} -> {target} (WebAPI {server_host})")
        else:
            row("config", "summon route", "WARN", f"{overrides_path} has no summon entry")
    else:
        row("config", "summon route", "WARN",
            f"no {overrides_path} - the launcher writes it when it starts the game")

    print(f"        server    {server}")
    print(f"        community {community}")
    return password, server.rstrip("/"), community.rstrip("/")


def phase_network(server, community, timeout):
    print("\n== network")
    seen = set()
    for url in (community, server):
        host = url.split("://", 1)[-1].split("/")[0]
        port = int(host.rsplit(":", 1)[1]) if ":" in host else (443 if url.startswith("https") else 80)
        name = host.split(":")[0]
        if (name, port) in seen:
            continue
        seen.add((name, port))
        try:
            addresses = sorted({info[4][0] for info in socket.getaddrinfo(name, port, socket.AF_INET)})
            row("network", f"dns {name}", "OK", ", ".join(addresses))
        except OSError as error:
            row("network", f"dns {name}", "FAIL", str(error))
            continue
        ok, elapsed = tcp_probe(name, port, timeout)
        row("network", f"tcp {name}:{port}", "OK" if ok else "FAIL", f"{elapsed:.0f} ms")


def phase_ss_info(community, server, timeout):
    print("\n== ss.info (what the game is told to call)")
    url = community + SS_INFO_PATH
    try:
        started = time.time()
        with urllib.request.urlopen(url, timeout=timeout) as response:
            raw = response.read()
            headers = response.headers
        elapsed = (time.time() - started) * 1000
        row("ss.info", "fetch", "OK", f"{url} - {len(raw)} bytes in {elapsed:.0f} ms")
    except (OSError, ValueError) as error:
        row("ss.info", "fetch", "FAIL", f"{url}: {error}")
        return None, None
    try:
        text = base64.b64decode(raw).decode("utf-8", "replace")
    except ValueError:
        text = raw.decode("utf-8", "replace")

    found = {}
    for key in ("api_SummonDataCreate", "api_SummonDataGetList", "api_SummonDataRemove",
                "api_SummonDataSummon", "SummonDataCreateInterval2", "SummonDataGetListInterval2",
                "SummonDataCoopMatchingLevelUpperAbs2", "SummonDataCoopMatchingLevelLowerAbs2"):
        match = re.search(rf"<{key}>([^<]*)</{key}>", text)
        found[key] = match.group(1) if match else None
        print(f"        {key:42s} {found[key] if found[key] is not None else 'MISSING'}")

    bad = [k for k in ("api_SummonDataCreate", "api_SummonDataGetList") if not found.get(k)]
    dead = [k for k, v in found.items() if v and ("0.0.0.0" in v)]
    if bad:
        row("ss.info", "summon urls", "FAIL", f"missing: {', '.join(bad)}")
    elif dead:
        row("ss.info", "summon urls", "FAIL", f"unusable: {', '.join(dead)} (the 0.0.0.0 bug)")
    else:
        row("ss.info", "summon urls", "OK", "create and get are both addressed")
    return found, headers


def phase_account(server, password, user_id, area, level, timeout, do_create, keep):
    print("\n== account and server")
    url_get = server + "/summon_messenger/get"
    url_create = server + "/summon_messenger/create"
    url_delete = server + "/summon_messenger/delete"

    status, body, elapsed, _headers = post_json(url_get, {}, timeout)
    public = sign_list(body)
    row("account", "get (public)", "OK" if status == 200 else "FAIL",
        f"status {status}, {len(public)} sign(s), {elapsed:.0f} ms")

    password_signs = []
    if password:
        status, body, elapsed, _headers = post_json(url_get, {"SummonWord": password}, timeout)
        password_signs = sign_list(body)
        row("account", "get (your password)", "OK" if status == 200 else "FAIL",
            f"status {status}, {len(password_signs)} sign(s)")

    pool = {s.get("UserId"): s for s in public + password_signs if s.get("UserId") is not None}
    if user_id is None:
        row("account", "your UserId", "WARN", "not given and not found in the logs")
        return
    print(f"        probing as UserId {user_id}")
    mine = [s for s in public + password_signs if s.get("UserId") == user_id]
    if mine:
        row("account", "stale record", "WARN",
            f"{len(mine)} sign(s) already on the server for this UserId "
            f"(session {mine[0].get('SessionId')}) - it can block a new one")
    else:
        row("account", "stale record", "OK", "none for this UserId")

    if not do_create:
        row("account", "create", "SKIP", "--no-create")
        return



    session_id = f"coop-probe-{uuid.uuid4()}"
    summon_data = bytearray(224)
    summon_data[0x40:0x4A] = b"CoopProbe\0"
    payload = {
        "AreaId": 402718720,
        "AreaRegionId": area,
        "ChannelId": 0,
        "CharaId": 9223372036854775808,
        "ClientVersion": 0,
        "MatchingLevel": level,
        "MessageId": "SummonDataCreateRequest",
        "NatType": 0,
        "NaturalEnemy_BloodHunter": 0,
        "NaturalEnemy_HunterOfHunter": 0,
        "NaturalEnemy_Vileblood": 0,
        "NaturalEnemy_VilebloodHunter": 0,
        "PosX": -194,
        "PosY": -28,
        "PosZ": 67,
        "Region": 2,
        "RegionFlag": 1,
        "SessionId": session_id,
        "SummonData": base64.b64encode(bytes(summon_data)).decode("ascii"),
        "SummonDataVersion": 3,
        "SummonMethod": 0,
        "SummonType": 0,
        "SummonWord": password or None,
        "UnlockedFlagList": [{"UnlockedFlag": 0}, {"UnlockedFlag": 0}, {"UnlockedFlag": 0}],
        "UserId": user_id,
    }
    status, body, elapsed, _headers = post_json(url_create, payload, timeout)
    try:
        kind = json.loads(body).get("ResKind")
    except (ValueError, AttributeError):
        kind = None
    row("account", "create", "OK" if status == 200 and kind == 0 else "FAIL",
        f"status {status}, ResKind {kind}, {elapsed:.0f} ms, area {area}, level {level}")

    if status == 200 and kind == 0:
        # A sign with a password is only listed for that password, not in the public list.
        query = {"SummonWord": password} if password else {}
        listed = [s for s in sign_list(post_json(url_get, query, timeout)[1])
                  if s.get("SessionId") == session_id]
        row("account", "create visible", "OK" if listed else "WARN",
            f"{len(listed)} of 1 in the {'password' if password else 'public'} search")

    if keep:
        row("account", "cleanup", "SKIP", f"--keep: leaving {session_id}")
        return
    status, body, elapsed, _headers = post_json(url_delete, {
        "MessageId": "SummonDataRemoveRequest",
        "SessionId": session_id,
        "UserId": user_id,
        "Force": True,
    }, timeout)
    row("account", "cleanup", "OK" if status == 200 else "WARN",
        f"status {status}, {body[:60].decode('utf-8', 'replace')}")


def phase_relay(timeout):
    print("\n== relay (outbound UDP)")
    try:
        address = socket.gethostbyname(RELAY_HOST)
    except OSError as error:
        row("relay", "dns", "FAIL", str(error))
        return
    probe_id = ("probe-%d" % os.getpid()).encode()[:16].ljust(16, b"\0")
    packet = b"BBR1" + b"\x01" + probe_id
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    started = time.time()
    try:
        for _attempt in range(3):
            sock.sendto(packet, (address, RELAY_PORT))
            try:
                data, source = sock.recvfrom(64)
            except socket.timeout:
                continue
            elapsed = (time.time() - started) * 1000
            if data[:4] == b"BBR1" and len(data) > 4 and data[4] == 0x02:
                row("relay", "register", "OK",
                    f"{address}:{RELAY_PORT} acked in {elapsed:.0f} ms ({source[0]})")
            else:
                row("relay", "register", "WARN",
                    f"answered {data[:8].hex()} in {elapsed:.0f} ms (unexpected reply)")
            return
        row("relay", "register", "FAIL",
            f"no reply from {address}:{RELAY_PORT} - outbound UDP 3478 is blocked, or the relay is down")
    finally:
        sock.close()


def phase_local(server_host, timeout, ss_headers):
    print("\n== local network")
    clock = ss_headers.get("Date") if ss_headers else None
    if clock:
        try:
            from email.utils import parsedate_to_datetime
            skew = abs((time.time() - parsedate_to_datetime(clock).timestamp()))
            row("local", "clock skew", "OK" if skew < 300 else "WARN", f"{skew:.0f} s vs the server")
        except (TypeError, ValueError):
            row("local", "clock skew", "WARN", f"cannot parse Date: {clock}")
    else:
        row("local", "clock skew", "WARN", "no Date header from ss.info")

    proxies = {k: os.environ[k] for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                                          "http_proxy", "https_proxy", "all_proxy")
               if os.environ.get(k)}
    if proxies:
        row("local", "proxy variables", "WARN",
            "set but ignored by the port: " + ", ".join(f"{k}={v}" for k, v in proxies.items()))
    else:
        row("local", "proxy variables", "OK", "none set")

    try:
        target = socket.gethostbyname(server_host)  # ip route wants an address, not a name
        route = subprocess.run(["ip", "route", "get", target], capture_output=True,
                               text=True, timeout=5)
        if route.returncode == 0:
            row("local", "route", "OK", f"{target}: {route.stdout.strip()}")
        else:
            row("local", "route", "WARN", route.stderr.strip() or "ip route get failed")
    except (OSError, subprocess.SubprocessError):
        row("local", "route", "WARN", "ip not available")

    rules = ""
    for command in (["nft", "list", "ruleset"], ["iptables", "-S"]):
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=5)
            rules += result.stdout or ""
        except (OSError, subprocess.SubprocessError):
            continue
    if rules:
        hits = [line for line in rules.splitlines()
                if re.search(r"\b(31315|3478)\b", line) and re.search(r"drop|reject", line, re.I)]
        if hits:
            row("local", "firewall", "WARN", f"{len(hits)} rule(s) mention 31315/3478 with drop/reject")
        else:
            row("local", "firewall", "OK", "no drop/reject rule for 31315 or 3478")
    else:
        row("local", "firewall", "WARN", "rules not readable without root (skipped)")


def summary():
    print("\n" + "=" * 78)
    failures = [r for r in ROWS if r[2] == "FAIL"]
    warnings = [r for r in ROWS if r[2] == "WARN"]
    for phase, name, status, detail in ROWS:
        print(f"{status:4s} {phase:8s} {name:22s} {detail[:90]}")
    print("=" * 78)
    if failures:
        print(f"RESULT: {len(failures)} critical check(s) failed - the problem is outside the game "
              f"(see above).")
        return 1
    if warnings:
        print(f"RESULT: nothing critical failed ({len(warnings)} warning(s)).")
    else:
        print("RESULT: everything passed.")
    print("If this run passes and the game still posts no /summon_messenger/create, the network and "
          "the account are fine: the client never asks.")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Co-op probe: config, server, account, relay and local network in one run")
    parser.add_argument("--server", help="WebAPI base URL (default: launcher settings)")
    parser.add_argument("--community", help="community/game server base URL (default: launcher)")
    parser.add_argument("--password", "-p", help="in-game password (default: launcher settings)")
    parser.add_argument("--user-id", "-u", type=int, help="UserId to probe (default: newest log)")
    parser.add_argument("--area", "-a", type=int, default=241010, help="AreaRegionId for the create")
    parser.add_argument("--level", "-l", type=int, default=60, help="level for the create")
    parser.add_argument("--timeout", "-t", type=float, default=8.0, help="network timeout (s)")
    parser.add_argument("--no-create", action="store_true", help="read-only: do not post a sign")
    parser.add_argument("--keep", action="store_true", help="leave the probe's sign on the server")
    args = parser.parse_args()

    password, server, community = phase_config(args)
    user_id = args.user_id
    if user_id is None:
        user_id, log_name = newest_log_user_id()
        if user_id:
            print(f"        UserId {user_id} (from {log_name})")
    phase_network(server, community, args.timeout)
    info, headers = phase_ss_info(community, server, args.timeout)
    phase_account(server, password, user_id, args.area, args.level, args.timeout,
                  not args.no_create, args.keep)
    phase_relay(args.timeout)
    phase_local(server.split("://", 1)[-1].split("/")[0].split(":")[0], args.timeout, headers)
    return summary()


if __name__ == "__main__":
    sys.exit(main())
