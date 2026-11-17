"""Official HelixSR release metadata: GitHub backoff, the cached release.json and the update check."""
import http.client
import json
import math
from pathlib import Path
import re
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime

from compatibility_release import parse_version, validate_compatible_release
from fileops import atomic_json, https_context, load_json

REPO = 'lonewolf0622/HelixSR'
API = f'https://api.github.com/repos/{REPO}/releases/latest'
RELEASE_POLICY = 2


def version(value):
    m = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', value)
    if not m:
        raise ValueError('Only stable semantic-version releases are supported')
    return tuple(map(int, m.groups()))


def github_pause(error, now):
    """Honor server backoff headers from a GitHub API rate-limit or abuse response."""
    headers = error.headers or {}
    retry = headers.get('Retry-After')
    if retry is not None or error.code == 429:
        try:
            delay = float(retry) if str(retry).isdigit() else parsedate_to_datetime(retry).timestamp() - now
        except (TypeError, ValueError, OverflowError):
            delay = 60
        if not math.isfinite(delay):
            delay = 60
        return 'secondary', now + min(86400, max(60, delay))
    if error.code == 403 and headers.get('X-RateLimit-Remaining') == '0':
        try:
            reset = float(headers.get('X-RateLimit-Reset'))
            if math.isfinite(reset) and now < reset <= now + 86400:
                return 'primary', reset
        except (TypeError, ValueError):
            pass
    return 'blocked', now + 60


def load_release(settings):
    try:
        value = load_json(Path(settings) / 'release.json')
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def validate_release(release):
    if not isinstance(release, dict):
        raise ValueError('Invalid upstream release metadata')
    tag = 'v' + release.get('version', '') if isinstance(release.get('version'), str) else ''
    version(tag)
    digest, size = release.get('sha256'), release.get('size')
    if (not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest)
            or type(size) is not int or not 0 < size <= 512 << 20
            or release.get('url') != f'https://github.com/{REPO}/releases/download/{tag}/HelixSR-{tag[1:]}.zip'
            or release.get('page') != f'https://github.com/{REPO}/releases/tag/{tag}'):
        raise ValueError('Invalid official release URL, size or SHA256 digest')
    return release


def check_update(settings, force=False):
    """Fetch the official latest-release metadata from GitHub, keep it in release.json and return it."""
    old = load_release(settings)
    now = time.time()
    valid_cache = False
    try:
        validate_release(old)
        valid_cache = old.get('metadata_policy') == RELEASE_POLICY
    except (ValueError, TypeError):
        pass
    if (not force and valid_cache and not old.get('error')
            and isinstance(old.get('checked_at'), (int, float))
            and 0 <= now - old['checked_at'] < 6 * 3600):
        return old
    pause = old.get('api_retry_at')
    paused = (type(pause) in (int, float) and math.isfinite(pause)
              and now < pause <= now + 86400
              and old.get('api_limit_kind') in ('primary', 'secondary', 'blocked'))
    if paused:
        return old
    headers = {'User-Agent': 'HelixSR-Decky/0.1', 'Accept': 'application/vnd.github+json'}
    if valid_cache and isinstance(old.get('etag'), str):
        headers['If-None-Match'] = old['etag']
    try:
        with urllib.request.urlopen(urllib.request.Request(API, headers=headers), timeout=20, context=https_context()) as response:
            if response.geturl() != API:
                raise ValueError('Unexpected upstream metadata redirect')
            raw = response.read((2 << 20) + 1)
            if len(raw) > 2 << 20:
                raise ValueError('Upstream metadata is too large')
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get('draft') or data.get('prerelease'):
                raise ValueError('Expected an official stable HelixSR release')
            tag = data['tag_name']
            if not isinstance(tag, str):
                raise ValueError('Invalid release tag')
            version(tag)
            release_version = tag.removeprefix('v')
            if not isinstance(data.get('assets'), list):
                raise ValueError('Invalid upstream assets')
            asset = next((x for x in data['assets'] if isinstance(x, dict)
                          and x.get('name') == f'HelixSR-{release_version}.zip'), None)
            if (not asset or not isinstance(asset.get('digest'), str)
                    or not re.fullmatch(r'sha256:[a-fA-F0-9]{64}', asset['digest'])):
                raise ValueError('Official release lacks a usable SHA256 asset digest')
            current = dict(version=release_version, url=asset['browser_download_url'],
                           sha256=asset['digest'][7:].lower(), size=asset['size'],
                           checked_at=time.time(), etag=response.headers.get('ETag'),
                           page=data['html_url'], error=None, metadata_policy=RELEASE_POLICY,
                           metadata_source='github-api',
                           asset_id=asset.get('id'), asset_updated_at=asset.get('updated_at'))
            validate_release(current)
            # Network metadata is untrusted: accept only releases whose exact identity was reviewed.
            if parse_version(release_version) <= (1, 8, 1):
                validate_compatible_release({key: current[key] for key in ('version', 'url', 'page', 'sha256', 'size')})
            atomic_json(settings / 'release.json', current)
            return current
    except urllib.error.HTTPError as e:
        if e.code == 304 and valid_cache:
            old['checked_at'], old['error'] = time.time(), None
            old.pop('failed_at', None)
            atomic_json(settings / 'release.json', old)
            return old
        if e.code in (403, 429):
            kind, retry_at = github_pause(e, now)
            old.update(api_limit_kind=kind, api_retry_at=retry_at)
            old['error'] = (f'GitHub returned HTTP {e.code}; '
                            + ('its request limit is active. ' if kind in ('primary', 'secondary')
                               else 'access to its update API was denied. ')
                            + 'Retry later; reconnecting may not help.')
        else:
            old['error'] = f'GitHub returned HTTP {e.code}; retry later to check the latest HelixSR'
    except (OSError, ValueError, TypeError, KeyError, http.client.HTTPException) as e:
        old['error'] = str(e)
    if not valid_cache:
        old = {key: old[key] for key in ('error', 'api_retry_at', 'api_limit_kind') if key in old}
    old['failed_at'] = time.time()
    atomic_json(settings / 'release.json', old)
    return old
