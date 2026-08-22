#!/usr/bin/env python3
"""Print where every app in the fleet currently stands, as a markdown table.

Answers the question that otherwise costs nineteen browser tabs: what is on
Play, on which track, and what is the newest build on TestFlight.

    python3 fleet_status.py apps.json --service-account sa.json \\
        --asc-key-id KEY --asc-issuer-id ISS --asc-key-path AuthKey.p8

Anything it cannot read is reported as an error in the row rather than left
blank — a store that refused the request must not look like a store with
nothing on it.
"""

import argparse
import json
import sys

import asc_publish
import play_publish

TRACK_ORDER = ("production", "beta", "alpha", "internal")


def play_row(sa_path, package):
    """Highest versionCode per track, plus its status."""
    try:
        token = play_publish.access_token(play_publish.load_sa(sa_path))
        edit_id = play_publish.open_edit(token, package)
    except SystemExit as exc:
        return None, str(exc).splitlines()[0]

    out = {}
    try:
        for track in TRACK_ORDER:
            try:
                data = play_publish.api(
                    token,
                    "GET",
                    "{}/applications/{}/edits/{}/tracks/{}".format(
                        play_publish.API, package, edit_id, track
                    ),
                )
            except SystemExit:
                continue
            for rel in data.get("releases", []):
                codes = rel.get("versionCodes") or []
                if not codes:
                    continue
                label = str(max(int(c) for c in codes))
                status = rel.get("status", "?")
                if status != "completed":
                    label += " ({})".format(status)
                if rel.get("userFraction"):
                    label += " {:.0%}".format(rel["userFraction"])
                out[track] = label
    finally:
        play_publish.delete_edit(token, package, edit_id)
    return out, None


def testflight_row(token, app_id):
    try:
        builds = asc_publish.builds(token, app_id, limit=5)
    except SystemExit as exc:
        return None, str(exc).splitlines()[0]
    if not builds:
        return "—", None
    newest = builds[0]["attributes"]
    return "{} ({})".format(
        newest.get("version", "?"), newest.get("processingState", "?")
    ), None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    parser.add_argument("--service-account", required=True)
    parser.add_argument("--asc-key-id", required=True)
    parser.add_argument("--asc-issuer-id", required=True)
    parser.add_argument("--asc-key-path", required=True)
    args = parser.parse_args()

    apps = json.load(open(args.manifest))["apps"]
    asc_token = asc_publish.jwt(args.asc_key_path, args.asc_key_id, args.asc_issuer_id)

    print("| App | production | beta | alpha | internal | TestFlight |")
    print("|---|---|---|---|---|---|")

    problems = []
    for app in apps:
        if not app["platforms"]:
            continue
        cells = []
        if "android" in app["platforms"]:
            tracks, err = play_row(args.service_account, app["package"])
            if err:
                problems.append("{}: Play — {}".format(app["repo"], err))
                cells = ["!"] * 4
            else:
                cells = [tracks.get(t, "—") for t in TRACK_ORDER]
        else:
            cells = ["—"] * 4

        if "ios" in app["platforms"]:
            tf, err = testflight_row(asc_token, app["asc_app_id"])
            if err:
                problems.append("{}: App Store Connect — {}".format(app["repo"], err))
                tf = "!"
        else:
            tf = "—"

        print("| **{}** ({}) | {} |".format(
            app["name"], app["repo"], " | ".join(cells + [tf])))

    if problems:
        print("")
        print("**Okunamayanlar** (`!` isaretli hucreler):")
        for line in problems:
            print("- {}".format(line))
        # A store that refused the request is not a store with nothing on it.
        sys.exit(1)


if __name__ == "__main__":
    main()
