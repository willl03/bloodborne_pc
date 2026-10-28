#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Co-op dummy guest: publish a real summon sign on a Seamless Hunt server.

Every seamless client has to do two things to be summoned: ring the bell locally, and then
ask the server to publish the sign (POST /summon_messenger/create). The host, ringing its
own bell, only searches (POST /summon_messenger/get) and claims what it finds
(POST /summon_messenger/request).

This tool stands in for the guest half, so the host half can be tested on its own:

    terminal 1:  python3 extra/tools/test_coop_dummy.py -p bbtest -l 60 -a 242000,242010
    the game:    ring only the Hunt Bell, at a lamp of that area, same password

Use a password of your own for the test, not a shared one such as 1234: the server lists
signs by password, and anyone else on that password could claim the dummy's sign and make
this look like a success.

What each outcome means:

  * the game shows the sign / "HunterBot" and this tool reports "claimed" - the server,
    the search, the claim and the relay all work, and the only missing piece is the
    game's own sign post (its /summon_messenger/create never leaves the client);
  * the game finds nothing and this tool still lists the sign - the host's search is
    what fails (area, level window or ServerRegion filtering);
  * the create is refused or the sign is never visible here - the server refuses the
    advertisement payload.

The dummy has no world of its own, so a claimed sign will not turn into a session: the
game will try to connect to it and fail. That is expected; leave with the Parting Blank.
"""

import argparse
import base64
import json
import random
import sys
import time
import urllib.error
import urllib.request
import uuid

DEFAULT_SERVER = "http://play.seamlesshunt.com:31315"

try:  # the area names are shared with the radar, so both tools say the same thing
    from bb_radar import AREA_MAP, get_area_name
except ImportError:  # run from another directory: the ids still work, only the names are lost
    AREA_MAP = {}

    def get_area_name(area_id):
        return f"area {area_id}"


def _decode(raw):
    text = raw.decode("utf-8", errors="replace") if raw else ""
    try:
        return json.loads(text)
    except ValueError:
        return text


def _post(url, payload, timeout=8.0):
    """(status, decoded body); status 0 and the reason on a transport error."""
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, _decode(response.read())
    except urllib.error.HTTPError as error:
        return error.code, _decode(error.read())
    except (OSError, ValueError) as error:
        return 0, str(error)


def _summon_data(name, level):
    """The opaque SummonData blob: the owner's name at 0x40, the level at 0x70."""
    raw = bytearray(224)
    encoded = name.encode("utf-8", errors="replace")[:15]
    raw[0x40:0x40 + len(encoded)] = encoded
    raw[0x70:0x74] = int(level).to_bytes(4, "little", signed=True)
    return base64.b64encode(bytes(raw)).decode("ascii")


def _create_payload(area, level, password, name, session_id, user_id):
    return {
        "AreaId": 402718720,  # 0x18000000, as the game sends it
        "AreaRegionId": area,
        "ChannelId": 0,
        "CharaId": 9223372036854775808,  # the game's own sentinel
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
        "SummonData": _summon_data(name, level),
        "SummonDataVersion": 3,
        "SummonMethod": 0,
        "SummonType": 0,
        "SummonWord": password or None,
        "UnlockedFlagList": [{"UnlockedFlag": 0}, {"UnlockedFlag": 0}, {"UnlockedFlag": 0}],
        "UserId": user_id,
    }


def _search(url, password):
    """The signs the server lists now (password query first, then the public list)."""
    queries = [{"SummonWord": password}] if password else []
    queries.append({})
    signs, seen = [], set()
    for query in queries:
        status, body = _post(url, query)
        if status != 200 or not isinstance(body, dict):
            continue
        for sign in body.get("SummonDataList") or []:
            key = (sign.get("UserId"), sign.get("SessionId"))
            if key not in seen:
                seen.add(key)
                signs.append(sign)
    return signs


def _parse_areas(values):
    areas = []
    for value in values or []:
        for part in str(value).replace(";", ",").split(","):
            part = part.strip()
            if part:
                areas.append(int(part, 0))
    return areas


def main():
    parser = argparse.ArgumentParser(
        description="Publish a summon sign as a dummy guest, to test the host's bell alone"
    )
    parser.add_argument("--server", default=DEFAULT_SERVER, help="WebAPI address of the server")
    parser.add_argument("--password", "-p", default="", help="the in-game password (SummonWord)")
    parser.add_argument(
        "--area", "-a", action="append", default=None,
        help="AreaRegionId to advertise in; repeat or comma separate. The host's search only "
             "returns signs of its own area, so this must be the area the game is in.",
    )
    parser.add_argument("--level", "-l", type=int, default=60, help="level of the dummy (the "
                        "host's search has a +-20 window around its own level)")
    parser.add_argument("--name", default="HunterBot", help="name shown on the sign")
    parser.add_argument("--duration", "-d", type=int, default=180,
                        help="seconds to keep the sign up (default 180)")
    parser.add_argument("--interval", "-i", type=float, default=3.0,
                        help="seconds between searches (default 3)")
    parser.add_argument("--keep", action="store_true",
                        help="leave the sign on the server instead of removing it on exit")
    parser.add_argument("--list-areas", action="store_true",
                        help="print the area ids the radar knows and exit")
    args = parser.parse_args()

    if args.list_areas:
        for area_id, name in sorted(AREA_MAP.items()):
            print(f"{area_id:>7}  {name}")
        return 0

    server = args.server.rstrip("/")
    url_create = server + "/summon_messenger/create"
    url_get = server + "/summon_messenger/get"
    url_delete = server + "/summon_messenger/delete"
    areas = _parse_areas(args.area) or [241010]

    print("=" * 78)
    print(f"Co-op dummy guest: {args.name}, level {args.level}")
    print(f"Server:   {server}")
    print(f"Password: {args.password!r}" if args.password else "Password: (none, public sign)")
    for area in areas:
        print(f"Area:     {area} ({get_area_name(area)})")
    print("=" * 78)

    published = []  # (area, session_id, user_id)
    failures = 0
    for area in areas:
        session_id = f"bbport-dummy-{uuid.uuid4()}"
        user_id = random.randint(700_000_000, 799_999_999)
        payload = _create_payload(area, args.level, args.password, args.name, session_id, user_id)
        status, body = _post(url_create, payload)
        kind = body.get("ResKind") if isinstance(body, dict) else None
        if status == 200 and kind == 0:
            published.append((area, session_id, user_id))
            print(f"create  {area:>7}: status {status}, ResKind {kind} - sign published")
        else:
            failures += 1
            print(f"create  {area:>7}: FAILED, status {status}, body {body!r}")

    if not published:
        print("\nThe server refused every sign; nothing to wait for.")
        return 1

    time.sleep(1.0)
    visible = {(s.get("UserId"), s.get("SessionId")) for s in _search(url_get, args.password)}
    for area, session_id, user_id in published:
        state = "visible in the search" if (user_id, session_id) in visible else "NOT in the search"
        print(f"search  {area:>7}: {state}")

    print("\nNow ring only the Hunt Bell in the game, at a lamp of that area, same password.")
    print("Waiting for the host's game to claim the sign (Ctrl+C stops the test)...\n")

    claimed = False
    started = time.time()
    try:
        while time.time() - started < args.duration:
            time.sleep(max(0.5, args.interval))
            elapsed = int(time.time() - started)
            listed = _search(url_get, args.password)
            keys = {(s.get("UserId"), s.get("SessionId")) for s in listed}
            remaining = [area for area, session_id, user_id in published
                         if (user_id, session_id) in keys]
            if len(remaining) < len(published):
                gone = [area for area, _s, _u in published if area not in remaining]
                print(f"[{elapsed:>4}s] sign no longer listed in {gone}: "
                      f"claimed by the game (the server hides a sign while it is being "
                      f"prepared or after it is claimed)")
                claimed = True
                break
            # Show the neighbours' signs too: an empty search while the sign is up means the
            # host's query filters it out (area / level / password), not that nothing exists.
            neighbours = len(listed) - len(published)
            extra = f", {neighbours} other sign(s) listed" if neighbours > 0 else ""
            print(f"[{elapsed:>4}s] sign still listed{extra} - the host has not claimed it yet")
    except KeyboardInterrupt:
        print("\ntest stopped")
    finally:
        if args.keep:
            print("--keep: the sign stays on the server")
        else:
            for area, session_id, user_id in published:
                status, body = _post(url_delete, {
                    "MessageId": "SummonDataRemoveRequest",
                    "SessionId": session_id,
                    "UserId": user_id,
                    "Force": True,
                })
                kind = body.get("ResKind") if isinstance(body, dict) else "?"
                print(f"delete  {area:>7}: status {status}, ResKind {kind}")

    print()
    if claimed:
        print("RESULT: the sign was found and claimed - advertisement, search, claim and the "
              "server all work.")
        print("        In the game's log the host side must show a "
              "'/summon_messenger/request' line for this claim.")
    else:
        print(f"RESULT: not claimed within {args.duration} s.")
        print("        Check, in this order: same password; the game's area is the one above; "
              "the game's level is within +-20 of the dummy's; the game's log shows "
              "'/summon_messenger/get' responses with body much larger than 73 bytes.")
    return 0 if claimed else 2


if __name__ == "__main__":
    sys.exit(main())
