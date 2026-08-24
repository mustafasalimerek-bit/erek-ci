#!/usr/bin/env python3
"""Google Play Android Publisher driver for CI — zero pip dependencies.

Auth is an RS256 service-account JWT signed with `openssl dgst -sha256 -sign`
and exchanged for an OAuth token, so this runs on a bare ubuntu-latest runner
with no `pip install` step to break.

Subcommands:

  next-version-code   print (highest versionCode Play has ever seen) + 1
  upload              upload an AAB, point a track at it, commit the edit
  promote             move what is on one track to another (internal -> production)
  status              print what is on every track

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


def commit_edit(token, pkg, edit_id, sent_for_review):
    """Commit, refusing to touch anything that is already under review.

    `edits.commit` defaults to CANCEL_IN_REVIEW_AND_SUBMIT: it will cancel an
    in-progress review and submit *everything* pending in Publishing Overview,
    not just this release. That turns "upload a draft" into "submit whatever
    else was half-finished in the Console", which is the opposite of what a
    draft is for. ERROR_IF_IN_REVIEW makes it fail loudly instead.
    """
    params = ["changesInReviewBehavior=ERROR_IF_IN_REVIEW"]
    if not sent_for_review:
        params.append("changesNotSentForReview=true")
    url = "{}/applications/{}/edits/{}:commit?{}".format(
        API, pkg, edit_id, "&".join(params)
    )
    try:
        api(token, "POST", url)
    except SystemExit as exc:
        # Some Play accounts now send edits for review automatically and reject
        # changesNotSentForReview entirely. Keep ERROR_IF_IN_REVIEW on the retry:
        # if unrelated Console changes are already being reviewed, the upload
        # must fail safely instead of cancelling and resubmitting that review.
        if sent_for_review or "changesNotSentForReview must not be set" not in str(exc):
            raise
        print("commit  Play auto-review account; retrying without changesNotSentForReview")
        retry_url = "{}/applications/{}/edits/{}:commit?{}".format(
            API, pkg, edit_id, "changesInReviewBehavior=ERROR_IF_IN_REVIEW"
        )
        api(token, "POST", retry_url)


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

        # A track PUT replaces the whole release list. Sending only the new
        # release therefore *removes* whatever that track was already serving —
        # which is how Waqt's internal track ended up holding nothing but a
        # draft. Keep every other active release and replace only same-status
        # ones.
        existing = api(
            token,
            "GET",
            "{}/applications/{}/edits/{}/tracks/{}".format(API, args.package, edit_id, args.track),
        ).get("releases", [])
        keep = [r for r in existing if r.get("status") != args.status]
        if keep:
            print("track  keeping {} existing release(s): {}".format(
                len(keep),
                ", ".join("{}={}".format(r.get("status"), ",".join(r.get("versionCodes", [])))
                          for r in keep)))

        api(
            token,
            "PUT",
            "{}/applications/{}/edits/{}/tracks/{}".format(API, args.package, edit_id, args.track),
            body={"track": args.track, "releases": keep + [release]},
        )
        print("track  {} <- versionCode {} (status={})".format(args.track, version_code, args.status))

        # A draft is not a submission, so it must not be sent for review.
        commit_edit(token, args.package, edit_id, sent_for_review=args.status != "draft")
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
        track_after = api(
            token,
            "GET",
            "{}/applications/{}/edits/{}/tracks/{}".format(
                API, args.package, verify_edit, args.track),
        )
    finally:
        delete_edit(token, args.package, verify_edit)

    match = [b for b in bundles if b["versionCode"] == version_code]
    if not match:
        raise SystemExit("post-commit read-back: versionCode {} not on Play".format(version_code))
    if match[0].get("sha1", "").lower() != local_sha1.lower():
        raise SystemExit("post-commit read-back: sha1 drifted")

    # The bundle existing proves the upload; it says nothing about the track.
    # Assert the release actually landed where it was asked to land.
    on_track = [
        r for r in track_after.get("releases", [])
        if str(version_code) in r.get("versionCodes", [])
    ]
    if not on_track:
        raise SystemExit(
            "post-commit read-back: versionCode {} is not on the {} track".format(
                version_code, args.track)
        )
    if on_track[0].get("status") != args.status:
        raise SystemExit(
            "post-commit read-back: {} on {} has status {}, expected {}".format(
                version_code, args.track, on_track[0].get("status"), args.status)
        )
    print("verify versionCode {} on {} as {}, sha1 matches local build".format(
        version_code, args.track, args.status))

    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write("version_code={}\n".format(version_code))
            fh.write("sha1={}\n".format(local_sha1))


TRACKS = ("internal", "alpha", "beta", "production")


def read_track(token, pkg, edit_id, track):
    return api(
        token, "GET", "{}/applications/{}/edits/{}/tracks/{}".format(API, pkg, edit_id, track)
    )


def cmd_status(args):
    token = access_token(load_sa(args.service_account))
    edit_id = open_edit(token, args.package)
    try:
        for track in TRACKS:
            try:
                data = read_track(token, args.package, edit_id, track)
            except SystemExit:
                print("{:<11} —".format(track))
                continue
            releases = data.get("releases", [])
            if not releases:
                print("{:<11} (empty)".format(track))
            for rel in releases:
                codes = ",".join(rel.get("versionCodes", []) or ["—"])
                fraction = rel.get("userFraction")
                extra = " {:.0%}".format(fraction) if fraction else ""
                print("{:<11} {:<8} {}{}".format(
                    track, codes, rel.get("status", "?"), extra))
    finally:
        delete_edit(token, args.package, edit_id)


def cmd_promote(args):
    if args.status == "inProgress" and args.user_fraction is None:
        raise SystemExit("--status inProgress needs --user-fraction (e.g. 0.1)")
    if args.status != "inProgress" and args.user_fraction is not None:
        raise SystemExit("--user-fraction only applies to --status inProgress")

    token = access_token(load_sa(args.service_account))
    edit_id = open_edit(token, args.package)
    try:
        source = read_track(token, args.package, edit_id, args.source)
        releases = source.get("releases", [])
        if not releases:
            raise SystemExit("nothing on the {} track to promote".format(args.source))
        # Newest first is not guaranteed, so pick by versionCode.
        newest = max(
            releases, key=lambda r: max(int(c) for c in r.get("versionCodes", ["0"]))
        )
        codes = newest.get("versionCodes", [])
        if not codes:
            raise SystemExit("the {} release has no versionCodes".format(args.source))
        print("promote {} -> {}: versionCode {}".format(
            args.source, args.target, ",".join(codes)))

        release = {"status": args.status, "versionCodes": codes}
        if args.user_fraction is not None:
            release["userFraction"] = args.user_fraction
        if newest.get("releaseNotes"):
            # Carry the notes across rather than shipping a blank "What's new".
            release["releaseNotes"] = newest["releaseNotes"]
        if args.name:
            release["name"] = args.name
        elif newest.get("name"):
            release["name"] = newest["name"]

        api(
            token,
            "PUT",
            "{}/applications/{}/edits/{}/tracks/{}".format(
                API, args.package, edit_id, args.target
            ),
            body={"track": args.target, "releases": [release]},
        )
        commit_edit(token, args.package, edit_id, sent_for_review=args.status != "draft")
        print("commit OK")
    except BaseException:
        delete_edit(token, args.package, edit_id)
        raise

    # Read the committed state back through a fresh edit, same as upload does.
    verify_edit = open_edit(token, args.package)
    try:
        target = read_track(token, args.package, verify_edit, args.target)
    finally:
        delete_edit(token, args.package, verify_edit)

    live = [
        r for r in target.get("releases", [])
        if set(r.get("versionCodes", [])) == set(codes)
    ]
    if not live:
        raise SystemExit(
            "post-commit read-back: versionCode {} is not on {}".format(
                ",".join(codes), args.target)
        )
    if live[0].get("status") != args.status:
        raise SystemExit(
            "post-commit read-back: status is {}, expected {}".format(
                live[0].get("status"), args.status)
        )
    print("verify {} now holds versionCode {} ({})".format(
        args.target, ",".join(codes), args.status))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service-account", required=True, help="path to the SA JSON key")
    parser.add_argument("--package", required=True, help="applicationId, e.g. com.erekstudio.waqt")
    sub = parser.add_subparsers(dest="cmd", required=True)

    nvc = sub.add_parser("next-version-code")
    nvc.set_defaults(func=cmd_next_version_code)

    up = sub.add_parser("upload")
    up.add_argument("--aab", required=True)
    up.add_argument("--track", default="internal", choices=list(TRACKS))
    up.add_argument(
        "--status",
        default="draft",
        # inProgress is deliberately absent: it requires a userFraction, which
        # upload has no way to take, so offering it here would only produce a
        # 400. Staged rollout is what `promote` is for.
        choices=["draft", "completed"],
        help="draft never publishes anything; completed rolls out to the track",
    )
    up.add_argument("--name", default="", help="release name shown in Console")
    up.add_argument("--release-notes", default="", help="JSON: {\"en-US\": \"...\"}")
    up.set_defaults(func=cmd_upload)

    pr = sub.add_parser("promote")
    pr.add_argument("--source", default="internal", choices=list(TRACKS))
    pr.add_argument("--target", default="production", choices=list(TRACKS))
    pr.add_argument(
        "--status",
        default="completed",
        choices=["draft", "completed", "inProgress", "halted"],
        help="completed = everyone; inProgress = staged, needs --user-fraction",
    )
    pr.add_argument(
        "--user-fraction",
        type=float,
        default=None,
        help="staged rollout share, 0.0-1.0 (only with --status inProgress)",
    )
    pr.add_argument("--name", default="", help="release name shown in Console")
    pr.set_defaults(func=cmd_promote)

    st = sub.add_parser("status")
    st.set_defaults(func=cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
