# erek-ci

Reusable GitHub Actions workflows for the Erek Studio app fleet, plus the two
store drivers they call.

This repo is **public on purpose**: a private reusable workflow can only be
checked out by a caller that carries a personal access token, and none of the
files here are secret. Every credential lives in the *caller* repo's Actions
secrets and is passed in through `secrets:`.

## Workflows

| Workflow | Runner | Cost | Trigger |
|---|---|---|---|
| `flutter-ci.yml` | ubuntu | 1x | push / PR |
| `android-release.yml` | ubuntu | 1x | dispatch |
| `ios-release.yml` | macos-15 | **10x** | dispatch only |

macOS minutes bill at ten times the ubuntu rate. A Flutter iOS build takes
roughly 20 minutes of wall clock, which is about 200 minutes of allowance. That
is why `ios-release.yml` never runs on push.

## Calling them

```yaml
jobs:
  release:
    uses: mustafasalimerek-bit/erek-ci/.github/workflows/android-release.yml@main
    with:
      package_name: com.erekstudio.waqt
      track: internal
      status: draft
    secrets: inherit
```

## Scripts

| Script | What it does |
|---|---|
| `scripts/play_publish.py` | Play Android Publisher: next versionCode, upload AAB, set track, commit |
| `scripts/asc_publish.py` | App Store Connect: next build number, wait for processing |

Both are stdlib-only. Auth is a JWT signed with `openssl dgst`, so there is no
`pip install` step that can break a release at 2am.

Both verify rather than trust. `play_publish.py` compares the sha1 Play reports
against the local file and re-reads the committed edit afterwards; a 200 only
means the request was accepted. `asc_publish.py` polls until App Store Connect
reports `VALID`, so a binary Apple rejects cannot read as a green run.

## Secrets each caller repo needs

Android:

- `ANDROID_KEYSTORE_B64` — `base64 -i upload.jks`
- `ANDROID_STORE_PASSWORD`, `ANDROID_KEY_PASSWORD`, `ANDROID_KEY_ALIAS`
- `PLAY_SERVICE_ACCOUNT_JSON` — the full service-account JSON

iOS:

- `ASC_KEY_ID`, `ASC_ISSUER_ID`, `ASC_KEY_P8` — App Store Connect API key
- `IOS_DIST_P12_B64`, `IOS_DIST_P12_PASSWORD` — Apple Distribution certificate

The distribution certificate is imported rather than created. Passing only the
API key would let Xcode mint a new one, and Apple caps the account at three.

## apps.json

The fleet manifest — one entry per shipping app, read by the dispatcher in
`erek-ops`. `null` in `keystore` means that app has no upload key yet and
cannot produce a release AAB.
