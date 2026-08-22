#!/usr/bin/env python3
"""App Store Connect driver for CI — zero pip dependencies.

Auth is an ES256 JWT signed with `openssl dgst -sha256 -sign` against the .p8.
openssl emits a DER signature; ASC wants raw JOSE r||s, so the DER is unpacked
into two 32-byte halves here. No PyJWT, no cryptography, no requests.

  next-build-number   print (highest build ASC has for this app) + 1
  wait-for-build      block until a build reaches a non-PROCESSING state
  check-version       refuse a marketing version whose train Apple has closed
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.appstoreconnect.apple.com/v1"


def b64u(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def der_to_jose(der):
    """DER SEQUENCE{INTEGER r, INTEGER s} -> 64 raw bytes (P-256)."""
    if der[0] != 0x30:
        raise SystemExit("openssl did not return a DER SEQUENCE")
    idx = 2 if der[1] < 0x80 else 2 + (der[1] & 0x7F)

    def read_int(pos):
        if der[pos] != 0x02:
            raise SystemExit("expected DER INTEGER at offset {}".format(pos))
        length = der[pos + 1]
        val = der[pos + 2 : pos + 2 + length]
        return val.lstrip(b"\x00").rjust(32, b"\x00"), pos + 2 + length

    r, idx = read_int(idx)
    s, _ = read_int(idx)
    return r + s


def jwt(key_path, key_id, issuer_id):
    now = int(time.time())
    header = b64u(json.dumps(
        {"alg": "ES256", "kid": key_id, "typ": "JWT"}, separators=(",", ":")
    ).encode())
    claims = b64u(json.dumps(
        {"iss": issuer_id, "iat": now, "exp": now + 1200, "aud": "appstoreconnect-v1"},
        separators=(",", ":"),
    ).encode())
    signing_input = "{}.{}".format(header, claims).encode()
    der = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", key_path],
        input=signing_input,
        capture_output=True,
        check=True,
    ).stdout
    return "{}.{}.{}".format(header, claims, b64u(der_to_jose(der)))


def api(token, path, params=None):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        raise SystemExit(
            "ASC {} {}\n  GET {}\n  {}".format(
                exc.code, exc.reason, url, exc.read().decode("utf-8", "replace")
            )
        )


def builds(token, app_id, version=None, limit=200):
    params = {"filter[app]": app_id, "limit": limit, "sort": "-uploadedDate"}
    if version:
        params["filter[preReleaseVersion.version]"] = version
    return api(token, "/builds", params).get("data", [])


def cmd_next_build_number(args, token):
    data = builds(token, args.app_id, args.version)
    numbers = []
    for build in data:
        raw = build["attributes"].get("version") or "0"
        try:
            numbers.append(int(raw))
        except ValueError:
            pass  # a non-numeric CFBundleVersion cannot take part in max()+1
    print(max(numbers) + 1 if numbers else 1)


# Once a version has shipped, Apple closes its pre-release train: any further
# build carrying that CFBundleShortVersionString is rejected at upload with
# "Invalid Pre-Release Train". These are the states that mean shipped.
CLOSED_STATES = {
    "READY_FOR_SALE",
    "PROCESSING_FOR_APP_STORE",
    "PENDING_APPLE_RELEASE",
    "REPLACED_WITH_NEW_VERSION",
    "REMOVED_FROM_SALE",
    "DEVELOPER_REMOVED_FROM_SALE",
}


def cmd_check_version(args, token):
    """Fail before a 25-minute macOS build that Apple would reject anyway."""
    versions = api(
        token,
        "/apps/{}/appStoreVersions".format(args.app_id),
        {"limit": 50},
    ).get("data", [])

    for version in versions:
        attrs = version["attributes"]
        if attrs.get("versionString") != args.version:
            continue
        state = attrs.get("appStoreState", "?")
        if state in CLOSED_STATES:
            raise SystemExit(
                "App Store Connect zaten {} surumunu yayinlamis ({}).\n"
                "  Ayni surum numarasiyla yeni build KABUL EDILMEZ.\n"
                "  pubspec.yaml'daki `version:` satirini yukselt "
                "(ornek {} -> {}), sonra tekrar dene.".format(
                    args.version, state, args.version, _bump(args.version)
                )
            )
        print("{} exists in state {} — still open for builds".format(args.version, state))
        return
    print("{} is a new version — nothing on App Store Connect blocks it".format(args.version))


def _bump(version):
    """Suggest the next patch, purely for the error message."""
    parts = version.split(".")
    try:
        parts[-1] = str(int(parts[-1]) + 1)
        return ".".join(parts)
    except ValueError:
        return version + ".1"


def cmd_wait_for_build(args, token):
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        for build in builds(token, args.app_id, args.version, limit=20):
            if build["attributes"].get("version") != str(args.build):
                continue
            state = build["attributes"].get("processingState")
            print("build {} ({}) -> {}".format(args.build, args.version, state))
            if state == "VALID":
                if os.environ.get("GITHUB_OUTPUT"):
                    with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
                        fh.write("build_id={}\n".format(build["id"]))
                return
            if state in ("INVALID", "FAILED"):
                raise SystemExit("App Store Connect rejected the build: " + state)
        time.sleep(args.interval)
    raise SystemExit(
        "build {} ({}) never showed up as VALID within {}s".format(
            args.build, args.version, args.timeout
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-id", default=os.environ.get("ASC_KEY_ID"))
    parser.add_argument("--issuer-id", default=os.environ.get("ASC_ISSUER_ID"))
    parser.add_argument(
        "--key-path",
        default=os.environ.get("ASC_KEY_PATH"),
        help="path to AuthKey_<KEYID>.p8",
    )
    parser.add_argument("--app-id", required=True, help="numeric App Store id")
    sub = parser.add_subparsers(dest="cmd", required=True)

    nbn = sub.add_parser("next-build-number")
    nbn.add_argument("--version", default="", help="marketing version, e.g. 1.2.0")
    nbn.set_defaults(func=cmd_next_build_number)

    cv = sub.add_parser("check-version")
    cv.add_argument("--version", required=True, help="marketing version, e.g. 1.2.0")
    cv.set_defaults(func=cmd_check_version)

    wfb = sub.add_parser("wait-for-build")
    wfb.add_argument("--version", required=True)
    wfb.add_argument("--build", required=True)
    wfb.add_argument("--timeout", type=int, default=2400)
    wfb.add_argument("--interval", type=int, default=45)
    wfb.set_defaults(func=cmd_wait_for_build)

    args = parser.parse_args()
    missing = [n for n in ("key_id", "issuer_id", "key_path") if not getattr(args, n)]
    if missing:
        raise SystemExit("missing: " + ", ".join("--" + m.replace("_", "-") for m in missing))
    args.func(args, jwt(args.key_path, args.key_id, args.issuer_id))


if __name__ == "__main__":
    main()
