#!/usr/bin/env python3
"""Fail if two apps in the fleet ship the same AdMob identifier.

An ad unit id belonging to another app is not rejected by anything: it is a
valid id, it serves ads, and the money lands in the other app's reports. The
only place the mistake is visible is by comparing the whole fleet at once,
which no single repo's test suite can do from its own checkout.

    python3 fleet_ad_ids.py <dir-of-checkouts>

Exits non-zero on a collision, or if it found so few apps that a collision
could not have been observed.
"""

import os
import re
import sys
from collections import defaultdict

UNIT_ID = re.compile(r"ca-app-pub-\d+/\d+")
APP_ID = re.compile(r"ca-app-pub-\d+~\d+")

# Google's public test ids are shared with the entire world by design.
TEST_PREFIXES = ("ca-app-pub-3940256099942544/", "ca-app-pub-3940256099942544~")

SOURCES = (
    "lib/ads/ad_unit_ids.dart",
    "app/lib/ads/ad_unit_ids.dart",
    "ios/Runner/Info.plist",
    "app/ios/Runner/Info.plist",
    "android/app/src/main/AndroidManifest.xml",
    "app/android/app/src/main/AndroidManifest.xml",
)


def ids_for(app_dir):
    """Every non-test AdMob id this app ships, with where it was found."""
    found = defaultdict(set)  # id -> {relative paths}
    for rel in SOURCES:
        path = os.path.join(app_dir, rel)
        if not os.path.isfile(path):
            continue
        with open(path, "r", errors="replace") as fh:
            text = fh.read()
        for pattern in (UNIT_ID, APP_ID):
            for match in pattern.findall(text):
                if match.startswith(TEST_PREFIXES):
                    continue
                found[match].add(rel)
    return found


def main():
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    root = sys.argv[1]

    apps = sorted(
        d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))
    )
    owners = defaultdict(dict)  # id -> {app: {paths}}
    with_ads = []

    for app in apps:
        found = ids_for(os.path.join(root, app))
        if found:
            with_ads.append(app)
        for ident, paths in found.items():
            owners[ident][app] = paths

    print("checked {} checkouts, {} of them ship AdMob ids: {}".format(
        len(apps), len(with_ads), ", ".join(with_ads) or "none"))

    # Guard, in the same spirit as the test this replaces: a comparison across
    # one app can never find a collision, so an empty result there is not a
    # clean bill of health.
    if len(with_ads) < 2:
        raise SystemExit(
            "only {} app(s) with AdMob ids were checked out — a collision could "
            "not have been observed, so this run proves nothing".format(len(with_ads))
        )

    collisions = {i: a for i, a in owners.items() if len(a) > 1}
    if collisions:
        print("\nFAIL: an identifier is shipped by more than one app")
        for ident, apps_for_id in sorted(collisions.items()):
            print("  {}".format(ident))
            for app, paths in sorted(apps_for_id.items()):
                print("    {} — {}".format(app, ", ".join(sorted(paths))))
        raise SystemExit(1)

    print("PASS: {} distinct ids, none shared between apps".format(len(owners)))


if __name__ == "__main__":
    main()
