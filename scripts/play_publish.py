#!/usr/bin/env python3
"""Google Play Android Publisher driver for CI — zero pip dependencies.

Auth is an RS256 service-account JWT signed with `openssl dgst -sha256 -sign`
and exchanged for an OAuth token, so this runs on a bare ubuntu-latest runner
with no `pip install` step to break.

Two subcommands:

  next-version-code   print (highest versionCode Play has ever seen) + 1
  upload              upload an AAB, point a track at it, commit the edit

`upload` compares the sha1 Play reports back against the local file's sha1 and
exits non-zero if they differ. A 200 only means Play accepted the request; the
byte comparison is what proves the artifact on the store is the artifact we
built.
"""

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://androidpublisher.googleapis.com/androidpublisher/v3"
UPLOAD = "https://androidpublisher.googleapis.com/upload/androidpublisher/v3"
SCOPE = "https://www.googleapis.com/auth/androidpublisher"


def b64u(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def access_token(sa):
    now = int(time.time())
    header = b64u(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = b64u(json.dumps(
        {
            "iss": sa["client_email"],
            "scope": SCOPE,
            "aud": TOKEN_URL,
            "iat": now,
            "exp": now + 3600,
        },
        separators=(",", ":"),
    ).encode())
    signing_input = "{}.{}".format(header, claims).encode()

    fd, key_path = tempfile.mkstemp(suffix=".pem")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(sa["private_key"])
        sig = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", key_path],
            input=signing_input,
            capture_output=True,
            check=True,
        ).stdout
    finally:
        os.unlink(key_path)

    assertion = "{}.{}.{}".format(header, claims, b64u(sig))
    body = urllib.parse.urlencode(
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        }
    ).encode()
    req = urllib.request.Request(TOKEN_URL, data=body)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)["access_token"]


def api(token, method, url, body=None, data=None, content_type=None, timeout=900):
    """One HTTP call. `body` is JSON; `data` is raw bytes (media upload)."""
    headers = {"Authorization": "Bearer " + token}
    payload = None
    if body is not None:
        payload = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    elif data is not None:
        payload = data
        headers["Content-Type"] = content_type or "application/octet-stream"
        headers["Content-Length"] = str(len(data))

    req = urllib.request.Request(url, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(
            "Play API {} {}\n  {} {}\n  {}".format(exc.code, exc.reason, method, url, detail)
        )


def sha1_of(path):
    digest = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_sa(path):
    with open(path) as fh:
        return json.load(fh)


def open_edit(token, pkg):
    return api(token, "POST", "{}/applications/{}/edits".format(API, pkg), body={})["id"]


def delete_edit(token, pkg, edit_id):
    try:
        api(token, "DELETE", "{}/applications/{}/edits/{}".format(API, pkg, edit_id))
    except SystemExit:
        pass  # rollback is best-effort; the real error is already on its way up


def cmd_next_version_code(args):
    token = access_token(load_sa(args.service_account))
    edit_id = open_edit(token, args.package)
    try:
        bundles = api(
            token, "GET", "{}/applications/{}/edits/{}/bundles".format(API, args.package, edit_id)
        ).get("bundles", [])
        apks = api(
            token, "GET", "{}/applications/{}/edits/{}/apks".format(API, args.package, edit_id)
        ).get("apks", [])
    finally:
        delete_edit(token, args.package, edit_id)

    codes = [b["versionCode"] for b in bundles] + [a["versionCode"] for a in apks]
    print(max(codes) + 1 if codes else 1)


def cmd_upload(args):
    aab = args.aab
    if not os.path.isfile(aab):
        raise SystemExit("AAB not found: " + aab)
    local_sha1 = sha1_of(aab)
    size_mb = os.path.getsize(aab) / (1024 * 1024)
    print("local  {}  {:.1f} MB  sha1={}".format(os.path.basename(aab), size_mb, local_sha1))

    token = access_token(load_sa(args.service_account))
    edit_id = open_edit(token, args.package)
    print("edit   {}".format(edit_id))

    try:
        with open(aab, "rb") as fh:
            blob = fh.read()
        uploaded = api(
            token,
            "POST",
            "{}/applications/{}/edits/{}/bundles?uploadType=media".format(
                UPLOAD, args.package, edit_id
            ),
            data=blob,
        )
        version_code = uploaded["versionCode"]
        remote_sha1 = uploaded.get("sha1", "")
        print("play   versionCode={}  sha1={}".format(version_code, remote_sha1))

        # A 200 only says the request was accepted. This is the byte check.
        if remote_sha1.lower() != local_sha1.lower():
            raise SystemExit(
                "sha1 mismatch — Play stored different bytes than we built.\n"
                "  local  {}\n  play   {}".format(local_sha1, remote_sha1)
            )

        release = {"status": args.status, "versionCodes": [str(version_code)]}
        if args.name:
            release["name"] = args.name
        if args.release_notes and os.path.isfile(args.release_notes):
            with open(args.release_notes) as fh:
                notes = json.load(fh)
            # {"en-US": "text", ...} or the API's own list form
            if isinstance(notes, dict):
                notes = [{"language": k, "text": v} for k, v in notes.items()]
            release["releaseNotes"] = notes

        api(
            token,
            "PUT",
            "{}/applications/{}/edits/{}/tracks/{}".format(API, args.package, edit_id, args.track),
            body={"track": args.track, "releases": [release]},
        )
        print("track  {} <- versionCode {} (status={})".format(args.track, version_code, args.status))

        api(token, "POST", "{}/applications/{}/edits/{}:commit".format(API, args.package, edit_id))
        print("commit OK")
    except BaseException:
        delete_edit(token, args.package, edit_id)
        raise

    # Read back through a fresh edit: the committed state, not the response we
    # were handed during the upload.
    verify_edit = open_edit(token, args.package)
    try:
        bundles = api(
            token,
            "GET",
            "{}/applications/{}/edits/{}/bundles".format(API, args.package, verify_edit),
        ).get("bundles", [])
    finally:
        delete_edit(token, args.package, verify_edit)

    match = [b for b in bundles if b["versionCode"] == version_code]
    if not match:
        raise SystemExit("post-commit read-back: versionCode {} not on Play".format(version_code))
    if match[0].get("sha1", "").lower() != local_sha1.lower():
        raise SystemExit("post-commit read-back: sha1 drifted")
    print("verify versionCode {} present, sha1 matches local build".format(version_code))

    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write("version_code={}\n".format(version_code))
            fh.write("sha1={}\n".format(local_sha1))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service-account", required=True, help="path to the SA JSON key")
    parser.add_argument("--package", required=True, help="applicationId, e.g. com.erekstudio.waqt")
    sub = parser.add_subparsers(dest="cmd", required=True)

    nvc = sub.add_parser("next-version-code")
    nvc.set_defaults(func=cmd_next_version_code)

    up = sub.add_parser("upload")
    up.add_argument("--aab", required=True)
    up.add_argument("--track", default="internal")
    up.add_argument(
        "--status",
        default="draft",
        choices=["draft", "completed", "inProgress", "halted"],
        help="draft never publishes anything; completed rolls out to the track",
    )
    up.add_argument("--name", default="", help="release name shown in Console")
    up.add_argument("--release-notes", default="", help="JSON: {\"en-US\": \"...\"}")
    up.set_defaults(func=cmd_upload)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
