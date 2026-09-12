#!/usr/bin/env python3
"""A fake GPS tracker that lives in Traccar like real hardware.

It registers itself as a device through Traccar's REST API, then reports
positions on an interval over the OsmAnd protocol — the same way an actual
tracker does. Traccar decodes the positions, runs its own geofence, overspeed,
motion and alarm logic, and pushes the results to Home Assistant. Nothing about
the outcome is faked; only the vehicle is.

Standard library only. Run it with any Python 3, no virtualenv needed.

    export TRACCAR_TOKEN=...            # or pass --token

    # Create the device and drive it in a circle, reporting every 10s:
    python tools/mock_gps.py --server traccar.example --id mock-001 run

    # While that runs, from another terminal, make things happen to it:
    python tools/mock_gps.py --server traccar.example --id mock-001 alarm tow
    python tools/mock_gps.py --server traccar.example --id mock-001 ignition off
    python tools/mock_gps.py --server traccar.example --id mock-001 attr io800=14583

    python tools/mock_gps.py --server traccar.example --id mock-001 remove

Set `--model FTC921` at registration to exercise device profile auto-matching,
which keys off the model Traccar reports.

Two ports are involved, and they are different:

* the **API** (8082, or 443 behind a proxy) creates and deletes the device
* the **OsmAnd port** (5055) receives positions

The OsmAnd port is frequently not exposed when Traccar sits behind an HTTPS
reverse proxy. If registration works but positions do not, that is why.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta

DEFAULT_API_PORT = 8082
DEFAULT_OSMAND_PORT = 5055

# From Position.java. Only used for the help text and a typo warning; anything
# is sent regardless, because vendors invent their own.
KNOWN_ALARMS = (
    "general sos vibration movement lowspeed overspeed fallDown lowPower "
    "lowBattery fault powerOff powerOn door lock unlock geofence geofenceEnter "
    "geofenceExit gpsAntennaCut accident tow idle highRpm hardAcceleration "
    "hardBraking hardCornering laneChange fatigueDriving powerCut powerRestored "
    "jamming temperature parking bonnet footBrake fuelLeak tampering removing"
).split()

EARTH_RADIUS_M = 6_371_000


class TraccarError(RuntimeError):
    """Raised when Traccar refuses a request, with its reason attached."""


# -- REST API: the device's existence ----------------------------------------


def api(args: argparse.Namespace, path: str, method: str = "GET", body=None):
    """Call the Traccar REST API with the bearer token."""
    if not args.token:
        sys.exit(
            "An API token is required to create or remove a device.\n"
            "Set TRACCAR_TOKEN, or pass --token. Generate one in Traccar under "
            "Settings -> Account."
        )
    scheme = "https" if args.api_ssl else "http"
    url = f"{scheme}://{args.server}:{args.api_port}/api/{path}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {args.token}")
    request.add_header("Accept", "application/json")
    if data:
        request.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = response.read()
            return json.loads(payload) if payload else None
    except urllib.error.HTTPError as err:
        detail = err.read().decode(errors="replace").strip()
        raise TraccarError(f"HTTP {err.code} {err.reason}: {detail}") from err
    except urllib.error.URLError as err:
        raise TraccarError(f"cannot reach {url}: {err.reason}") from err


def find_device(args: argparse.Namespace) -> dict | None:
    """Return the device with our identifier, if Traccar already has it."""
    for device in api(args, "devices") or []:
        if device.get("uniqueId") == args.id:
            return device
    return None


def ensure_device(args: argparse.Namespace) -> dict:
    """Create the device if it does not exist yet. Idempotent."""
    if (existing := find_device(args)) is not None:
        print(f"  device {args.id!r} already exists (id {existing['id']})")
        return existing

    created = api(
        args,
        "devices",
        method="POST",
        body={"name": args.name, "uniqueId": args.id, "model": args.model,
              "category": args.category},
    )
    print(f"  created device {args.id!r} (id {created['id']}) model={args.model!r}")
    return created


# -- OsmAnd: the device's behaviour ------------------------------------------


def send(args: argparse.Namespace, **extra: object) -> bool:
    """Report one position, exactly as a tracker would."""
    params: dict[str, object] = {
        "id": args.id,
        "lat": round(args.lat, 6),
        "lon": round(args.lon, 6),
        "timestamp": int(datetime.now(UTC).timestamp()),
        "speed": round(args.speed, 2),
        "bearing": round(args.bearing, 1),
        "altitude": args.altitude,
        "accuracy": args.accuracy,
        "batt": round(args.battery, 1),
        "valid": "true",
    }
    params.update(extra)
    for item in args.attr or []:
        key, _, value = item.partition("=")
        if not key or not value:
            sys.exit(f"--attr expects key=value, got {item!r}")
        params[key] = value

    scheme = "https" if args.ssl else "http"
    url = f"{scheme}://{args.server}:{args.port}/?" + urllib.parse.urlencode(params)

    if args.dry_run:
        print(url)
        return True

    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            ok = response.status == 200
    except urllib.error.HTTPError as err:
        print(f"  rejected: HTTP {err.code} {err.reason}", file=sys.stderr)
        if err.code == 400:
            print(
                f"  Traccar returns 400 for an unknown device. Run the `register` "
                f"command first, or check that {args.id!r} exists.",
                file=sys.stderr,
            )
        return False
    except urllib.error.URLError as err:
        print(f"  unreachable: {err.reason}", file=sys.stderr)
        print(
            f"  The OsmAnd port ({args.port}) is separate from the API port and is "
            f"often not exposed behind a reverse proxy.",
            file=sys.stderr,
        )
        return False

    detail = " ".join(f"{k}={v}" for k, v in extra.items())
    print(
        f"  {args.lat:.5f},{args.lon:.5f}  {args.speed:4.1f}kn  "
        f"{args.bearing:5.1f}deg  batt {args.battery:.0f}%  {detail}".rstrip()
    )
    return ok


# -- Commands -----------------------------------------------------------------


def cmd_register(args: argparse.Namespace) -> bool:
    ensure_device(args)
    return True


def cmd_remove(args: argparse.Namespace) -> bool:
    if (device := find_device(args)) is None:
        print(f"  no device with identifier {args.id!r}")
        return True
    api(args, f"devices/{device['id']}", method="DELETE")
    print(f"  removed device {args.id!r} (id {device['id']})")
    return True


def cmd_whoami(args: argparse.Namespace) -> bool:
    """Show which Traccar user the token is, and what notifications it can see.

    Traccar delivers an event to a websocket client only through
    NotificatorWeb, which sends to the users linked to a matching Notification.
    If the notifications were created by a different user than the one this
    token belongs to, everything looks correctly configured and no event is
    ever sent.
    """
    user = api(args, f"session?token={urllib.parse.quote(args.token or '')}")
    if not user:
        print("  could not identify the token's user")
        return False
    print(
        f"  token user  {user.get('name')!r}  id={user.get('id')}  "
        f"{user.get('email')}  admin={user.get('administrator')}"
    )

    notifications = api(args, "notifications") or []
    print(f"  notifications visible to this user: {len(notifications)}")
    if not notifications:
        print("    none. Events will never be pushed over the websocket.")
        print("    Create them in Traccar while logged in as this user, or share")
        print("    the existing ones with it.")
    for item in notifications:
        channels = item.get("notificators") or "(no channel)"
        alarms = (item.get("attributes") or {}).get("alarms")
        scope = "all devices" if item.get("always") else "selected devices"
        extra = f"  alarms={alarms}" if item.get("type") == "alarm" else ""
        print(f"    {item.get('type'):22} {channels:12} {scope}{extra}")
        if "web" not in str(channels).lower():
            print("      ^ not on the Web channel, so it never reaches the websocket")
        if item.get("type") == "alarm" and not alarms:
            print("      ^ alarm notification with no alarms listed never matches")
    return True


def cmd_status(args: argparse.Namespace) -> bool:
    """Ask Traccar what it actually has for this device.

    Answers the three questions separately, because a position being accepted,
    a position being stored, and an event being generated are three different
    things and only Traccar can tell them apart.
    """
    if (device := find_device(args)) is None:
        print(f"  no device with identifier {args.id!r} — run `register` first")
        return False

    print(f"  device    {device['name']!r}  id={device['id']}  model={device.get('model')!r}")
    print(f"  status    {device.get('status')}   last update {device.get('lastUpdate')}")
    if device.get("disabled"):
        print("  DISABLED in Traccar — positions are accepted but ignored")

    positions = api(args, f"positions?deviceId={device['id']}") or []
    if not positions:
        print("  position  none stored")
        print("            Traccar accepted the fix but did not keep it. Its filters "
              "(filter.duplicate, filter.static, filter.future) are the usual reason.")
    for position in positions:
        print(f"  position  {position['fixTime']}  "
              f"{position['latitude']:.5f},{position['longitude']:.5f}  "
              f"speed={position['speed']}kn  valid={position['valid']}")
        attributes = position.get("attributes") or {}
        if attributes:
            print("  attrs     " + ", ".join(f"{k}={v}" for k, v in attributes.items()))

    now = datetime.now(UTC).replace(tzinfo=None)
    since = now - timedelta(hours=args.hours)
    events = api(
        args,
        f"reports/events?deviceId={device['id']}"
        f"&from={since.isoformat()}Z&to={now.isoformat()}Z",
    ) or []
    print(f"  events    {len(events)} in the last {args.hours}h")
    for event in events[-10:]:
        detail = (event.get("attributes") or {}).get("alarm", "")
        print(f"            {event['eventTime']}  {event['type']}  {detail}".rstrip())
    return True


def cmd_position(args: argparse.Namespace) -> bool:
    return send(args)


def cmd_alarm(args: argparse.Namespace) -> bool:
    """Send an alarm, clearing first so a repeat is not swallowed.

    Traccar's AlarmEventHandler drops an alarm identical to the previous
    position's when `event.ignoreDuplicateAlerts` is on, so firing `tow` twice
    in a row produces one event. Sending a clean position in between makes the
    second one land, which is what you want when testing.
    """
    if args.name not in KNOWN_ALARMS:
        print(f"  note: {args.name!r} is not a documented Traccar alarm", file=sys.stderr)
    if not args.no_clear:
        print("  clearing previous alarm state")
        if not send(args):
            return False
        if not args.dry_run:
            time.sleep(1)
    return send(args, alarm=args.name)


def cmd_overspeed(args: argparse.Namespace) -> bool:
    """Exceed a speed limit, dropping below it first so the event fires.

    Overspeed is a transition, not a condition. OverspeedProcessor stores
    `overspeedState` and `overspeedTime` on the device and clears the time once
    it has fired, so a second over-limit position finds nothing to compare and
    reports nothing. The state persists in the database, which means a device
    left in overspeed stays silent across restarts until it slows down.

    Sending a compliant position first resets that state, so this command fires
    every time it is run.
    """
    if not args.no_clear:
        print("  slowing below the limit to reset overspeed state")
        compliant = args.limit / 2
        saved, args.speed = args.speed, compliant
        ok = send(args, speedLimit=args.limit)
        args.speed = saved
        if not ok:
            return False
        if not args.dry_run:
            time.sleep(1)

    args.speed = args.limit * 2
    return send(args, speedLimit=args.limit)


def cmd_fuel(args: argparse.Namespace) -> bool:
    """Report a fuel level.

    FuelEventHandler compares against the previous position, so a drop needs
    two calls: a high level, then a low one. It only fires if the device or
    server has `fuelDropThreshold` set — it defaults to 0, meaning disabled.
    """
    return send(args, fuel=args.litres)


def cmd_driver(args: argparse.Namespace) -> bool:
    """Change the driver, which fires driverChanged when the id differs."""
    return send(args, driverUniqueId=args.driver_id)


def cmd_motion(args: argparse.Namespace) -> bool:
    """Set motion directly, rather than letting Traccar infer it from speed."""
    return send(args, motion="true" if args.state == "on" else "false")


def cmd_result(args: argparse.Namespace) -> bool:
    """Report a command result, which fires commandResult."""
    return send(args, result=args.text)


def cmd_media(args: argparse.Namespace) -> bool:
    """Reference a media file, which fires the media event."""
    return send(args, **{args.kind: args.reference})


def cmd_ignition(args: argparse.Namespace) -> bool:
    """Change ignition, setting the opposite first so the event fires.

    IgnitionEventHandler compares against the previous position, so asking for
    `on` when it is already on produces nothing. The clearing fix also emits the
    opposite event, which is the price of making this reliable; `--no-clear`
    skips it.
    """
    wanted = args.state == "on"
    if not args.no_clear:
        print(f"  setting ignition {'off' if wanted else 'on'} first, so the change registers")
        if not send(args, ignition="false" if wanted else "true"):
            return False
        if not args.dry_run:
            time.sleep(1)
    return send(args, ignition="true" if wanted else "false")


def cmd_attr(args: argparse.Namespace) -> bool:
    args.attr = (args.attr or []) + args.pairs
    return send(args)


def cmd_run(args: argparse.Namespace) -> bool:
    """Behave like a tracker: register, then report on an interval forever.

    The route is a circle so the device genuinely moves — crossing geofences,
    changing bearing and producing motion — rather than sitting still and
    repeating one position, which would exercise none of Traccar's event logic.
    """
    if not args.no_register and not args.dry_run:
        try:
            ensure_device(args)
        except TraccarError as err:
            print(f"  registration failed: {err}", file=sys.stderr)
            return False

    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True
        print("\n  stopping")

    signal.signal(signal.SIGINT, stop)

    print(
        f"  reporting every {args.interval}s around "
        f"{args.lat:.4f},{args.lon:.4f} at radius {args.radius}m. Ctrl+C to stop."
    )

    started = time.monotonic()
    sent = 0
    while not stopping:
        elapsed = time.monotonic() - started
        angle = (elapsed / args.lap) * 2 * math.pi if args.lap else 0.0

        # Offset from the centre, converted from metres to degrees.
        lat_offset = (args.radius * math.cos(angle)) / EARTH_RADIUS_M
        lon_offset = (args.radius * math.sin(angle)) / (
            EARTH_RADIUS_M * math.cos(math.radians(args.centre_lat))
        )
        args.lat = args.centre_lat + math.degrees(lat_offset)
        args.lon = args.centre_lon + math.degrees(lon_offset)

        # Tangent to the circle, and the speed that circle implies.
        args.bearing = (math.degrees(angle) + 90) % 360
        circumference = 2 * math.pi * args.radius
        args.speed = (circumference / args.lap) * 1.94384 if args.lap else 0.0

        # A slow, believable drain so battery sensors have something to plot.
        args.battery = max(5.0, args.start_battery - elapsed / 600)

        extra = {"ignition": "true"} if args.speed > 0.5 else {"ignition": "false"}
        send(args, **extra)
        sent += 1

        if args.count and sent >= args.count:
            break
        if args.dry_run:
            break
        time.sleep(args.interval)

    print(f"  sent {sent} position(s)")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--server", required=True, help="Traccar hostname")
    parser.add_argument("--id", required=True, help="device identifier")
    parser.add_argument("--token", default=os.environ.get("TRACCAR_TOKEN"),
                        help="API token, or set TRACCAR_TOKEN")
    parser.add_argument("--name", default=None, help="device name in Traccar")
    parser.add_argument("--model", default="FTC921",
                        help="model, which drives device profile matching")
    parser.add_argument("--category", default="car")
    parser.add_argument("--api-port", type=int, default=DEFAULT_API_PORT)
    parser.add_argument("--api-ssl", action="store_true", help="API over https")
    parser.add_argument("--port", type=int, default=DEFAULT_OSMAND_PORT,
                        help="OsmAnd protocol port")
    parser.add_argument("--ssl", action="store_true", help="OsmAnd over https")
    parser.add_argument("--lat", type=float, default=45.5017)
    parser.add_argument("--lon", type=float, default=-73.5673)
    parser.add_argument("--speed", type=float, default=0.0, help="knots")
    parser.add_argument("--bearing", type=float, default=0.0)
    parser.add_argument("--altitude", type=float, default=30.0)
    parser.add_argument("--accuracy", type=float, default=5.0)
    parser.add_argument("--battery", type=float, default=95.0, help="percent")
    parser.add_argument("--attr", action="append", metavar="KEY=VALUE",
                        help="extra attribute, repeatable")
    parser.add_argument("--dry-run", action="store_true", help="print URLs only")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("register", help="create the device in Traccar").set_defaults(
        func=cmd_register)
    sub.add_parser("remove", help="delete the device from Traccar").set_defaults(
        func=cmd_remove)
    sub.add_parser("position", help="report a single fix").set_defaults(
        func=cmd_position)

    sub.add_parser(
        "whoami", help="show the token's user and its visible notifications"
    ).set_defaults(func=cmd_whoami)

    status = sub.add_parser(
        "status", help="show what Traccar has: device, last position, recent events")
    status.add_argument("--hours", type=float, default=1.0)
    status.set_defaults(func=cmd_status)

    alarm = sub.add_parser("alarm", help="report a fix carrying an alarm")
    alarm.add_argument("name", help=f"one of: {', '.join(KNOWN_ALARMS)}")
    alarm.add_argument("--no-clear", action="store_true",
                       help="skip the clearing fix sent first to defeat duplicate suppression")
    alarm.set_defaults(func=cmd_alarm)

    overspeed = sub.add_parser("overspeed", help="exceed a speed limit")
    overspeed.add_argument("--limit", type=float, default=10.0, help="knots")
    overspeed.add_argument("--no-clear", action="store_true",
                           help="skip the compliant fix sent first to reset overspeed state")
    overspeed.set_defaults(func=cmd_overspeed)

    fuel = sub.add_parser("fuel", help="report a fuel level (drop needs two calls)")
    fuel.add_argument("litres", type=float)
    fuel.set_defaults(func=cmd_fuel)

    driver = sub.add_parser("driver", help="change the driver")
    driver.add_argument("driver_id", metavar="DRIVER_ID")
    driver.set_defaults(func=cmd_driver)

    motion = sub.add_parser("motion", help="set motion on or off")
    motion.add_argument("state", choices=["on", "off"])
    motion.set_defaults(func=cmd_motion)

    result = sub.add_parser("result", help="report a command result")
    result.add_argument("text")
    result.set_defaults(func=cmd_result)

    media = sub.add_parser("media", help="reference an image, video or audio file")
    media.add_argument("kind", choices=["image", "video", "audio"])
    media.add_argument("reference", help="file name Traccar should record")
    media.set_defaults(func=cmd_media)

    ignition = sub.add_parser("ignition", help="report ignition on or off")
    ignition.add_argument("state", choices=["on", "off"])
    ignition.add_argument("--no-clear", action="store_true",
                          help="skip the opposite fix sent first to force a change")
    ignition.set_defaults(func=cmd_ignition)

    attr = sub.add_parser("attr", help="report a fix with extra attributes")
    attr.add_argument("pairs", nargs="+", metavar="KEY=VALUE")
    attr.set_defaults(func=cmd_attr)

    run = sub.add_parser("run", help="act as a live device until stopped")
    run.add_argument("--interval", type=float, default=10.0, help="seconds between fixes")
    run.add_argument("--radius", type=float, default=300.0, help="circle radius, metres")
    run.add_argument("--lap", type=float, default=600.0, help="seconds per lap; 0 to park")
    run.add_argument("--count", type=int, default=0, help="stop after N fixes")
    run.add_argument("--no-register", action="store_true",
                     help="assume the device already exists")
    run.set_defaults(func=cmd_run)

    args = parser.parse_args()
    args.name = args.name or f"Mock {args.id}"
    args.centre_lat, args.centre_lon = args.lat, args.lon
    args.start_battery = args.battery

    try:
        return 0 if args.func(args) else 1
    except TraccarError as err:
        print(f"  {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
