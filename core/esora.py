"""Image generation through the in-house Esora API.

Same shape as ``core.gemini``: give it a prompt and one or two RGB arrays, get an
RGB array back. Everything between -- uploading the references as assets, queuing
a generation, polling it, fetching the result -- is hidden here, because the rest
of the pipeline has no reason to know that this backend is asynchronous while the
Gemini one was not.

Authentication is not implemented here. ``esora-api auth login`` owns the Google
sign-in, the OS keyring and the refresh cycle; this module reads the ID token the
CLI cached and, when it has expired, shells out to the CLI to mint a new one.
That keeps one source of truth for credentials, and keeps this project's
``requirements.txt`` unchanged -- only the standard library is used.
"""

import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import cv2
import numpy as np

from .prompts import (  # noqa: F401  re-exported: callers use esora.KEY_PRESETS etc.
    KEY_PRESETS,
    compose_prompt,
    key_bg_instruction,
    key_only_prompt,
)

#: Image models in the Esora catalogue, most useful first. Refresh with
#: ``esora-api model list`` if the platform adds one.
IMAGE_MODELS = [
    ("azure_gpt_image_2", "GPT Image 2 (既定)"),
    ("gemini_nanobanana_2", "Nanobanana 2"),
    ("gemini_3_pro_image_preview", "Nanobanana Pro"),
    ("gemini_2_5_flash_image", "Nanobanana"),
    ("seedream_5_0_pro", "Seedream 5.0 Pro"),
    ("seedream_5_0_lite", "Seedream 5.0 Lite"),
    ("seedream_4_5", "Seedream 4.5"),
]

DEFAULT_MODEL = IMAGE_MODELS[0][0]

#: Matches what core.gemini asked Gemini for, so swapping the backend does not
#: silently change the size or framing the rest of the pipeline expects.
ASPECT_RATIO = "1:1"
IMAGE_SIZE = "2K"

DEFAULT_BASE_URL = "https://production.esora.gochipon.net"
POLL_INTERVAL = 3.0
POLL_TIMEOUT = 600.0
#: Refresh with time to spare: a token that expires mid-request surfaces as a
#: confusing 401 rather than as a refresh.
EXPIRY_SKEW_SECONDS = 300
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


class EsoraNotSignedIn(RuntimeError):
    """No usable session. The fix is always `esora-api auth login`."""


def _config_dir() -> Path:
    override = os.environ.get("ESORA_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".config" / "esora"


def _profile_name() -> str:
    return os.environ.get("ESORA_PROFILE") or "default"


def _read_token_cache() -> dict:
    path = _config_dir() / f"token-{_profile_name()}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _refresh_via_cli() -> None:
    """Make the CLI mint a fresh ID token and rewrite its cache.

    Any authenticated command does it; ``auth status`` is the cheapest. The
    refresh token lives in the OS keyring, which is precisely what this module
    does not want to touch itself.
    """
    try:
        subprocess.run(
            ["esora-api", "--json", "auth", "status"],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EsoraNotSignedIn(
            "esora-api コマンドを実行できませんでした。CLI がインストールされているか "
            "確認してください: " + str(exc)
        ) from None


def _session() -> tuple[str, str]:
    """Return ``(id_token, base_url)``, refreshing through the CLI when stale."""
    cache = _read_token_cache()
    expires_at = float(cache.get("expires_at") or 0)
    if not cache.get("id_token") or time.time() >= expires_at - EXPIRY_SKEW_SECONDS:
        _refresh_via_cli()
        cache = _read_token_cache()
        expires_at = float(cache.get("expires_at") or 0)

    token = cache.get("id_token")
    if not token or time.time() >= expires_at - EXPIRY_SKEW_SECONDS:
        raise EsoraNotSignedIn(
            "Esora にサインインしていません。ターミナルで `esora-api auth login` を "
            "実行してから、もう一度お試しください。"
        )
    base_url = os.environ.get("ESORA_BASE_URL") or cache.get("base_url") or DEFAULT_BASE_URL
    return token, base_url.rstrip("/")


def signed_in_email() -> str:
    """The signed-in address, or an empty string. Never raises -- it is for a label."""
    try:
        _session()
    except EsoraNotSignedIn:
        return ""
    return str(_read_token_cache().get("email") or "")


def _request(method: str, url: str, token: str, *, body: bytes = None,
             content_type: str = None, timeout: int = 300, retries: int = 3):
    """One HTTP call with the API's error envelope unpacked and 429 respected."""
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/json")
        req.add_header("X-Esora-App", "api")
        if content_type:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            # A 429 from the per-minute bucket clears within a minute; anything
            # longer is the daily bucket and not worth blocking the UI on.
            if exc.code == 429 and attempt < retries:
                wait = _retry_after(exc)
                if wait is not None and wait <= 90:
                    time.sleep(wait)
                    continue
            raise RuntimeError(f"Esora API {exc.code}: {_detail(raw)}") from None
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Esora API に接続できません: {exc.reason}") from None
    raise RuntimeError("Esora API: リトライ上限に達しました")


def _retry_after(exc: urllib.error.HTTPError):
    raw = exc.headers.get("Retry-After") or exc.headers.get("RateLimit-Reset")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _detail(raw: bytes) -> str:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return raw.decode("utf-8", "replace")[:800]
    if isinstance(payload, dict) and payload.get("detail"):
        return str(payload["detail"])[:800]
    return json.dumps(payload)[:800]


def _get_json(path: str, token: str, base_url: str) -> dict:
    return json.loads(_request("GET", f"{base_url}/api/v1{path}", token))


def _post_json(path: str, token: str, base_url: str, payload: dict = None) -> dict:
    body = json.dumps(payload or {}).encode("utf-8")
    raw = _request("POST", f"{base_url}/api/v1{path}", token,
                   body=body, content_type="application/json")
    return json.loads(raw)


_CAPABILITY_CACHE = {}


def _capabilities(model: str, token: str, base_url: str) -> dict:
    """What this model actually accepts, from the live catalogue.

    Cached for the process: the catalogue changes when the platform deploys, not
    between two generations, and a lookup per request would double the round
    trips for no new information.
    """
    if model not in _CAPABILITY_CACHE:
        items = _get_json("/models?task=image_generate", token, base_url).get("items", [])
        for item in items:
            _CAPABILITY_CACHE[item.get("id")] = item.get("capabilities") or {}
    return _CAPABILITY_CACHE.get(model, {})


def _sized_body(model: str, token: str, base_url: str) -> dict:
    """The size parameters this model takes, and only those.

    Not every model offers every tier -- `gemini_2_5_flash_image` declares
    `image_sizes: ["auto"]` -- and sending one a model does not declare is a 400,
    so an unsupported value is dropped and the server applies its own default
    rather than the request failing.
    """
    try:
        capabilities = _capabilities(model, token, base_url)
    except RuntimeError:
        # A catalogue that cannot be read is not a reason to refuse to generate;
        # fall back to asking for nothing and let the server choose.
        return {}
    body = {}
    if ASPECT_RATIO in (capabilities.get("aspect_ratios") or []):
        body["aspect_ratio"] = ASPECT_RATIO
    if IMAGE_SIZE in (capabilities.get("image_sizes") or []):
        body["image_size"] = IMAGE_SIZE
    return body


def _rgb_to_png_bytes(rgb: np.ndarray) -> bytes:
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    ok, buf = cv2.imencode(".png", bgr)
    if not ok:
        raise RuntimeError("failed to encode png")
    return buf.tobytes()


def _upload_reference(rgb: np.ndarray, name: str, token: str, base_url: str) -> str:
    """Upload one image and return its asset id.

    Esora references images by asset id rather than by inline bytes, so a
    reference is a real library asset: it has a hash, a permission model and a
    storage path, and the same picture is not re-sent on the next run.
    """
    png = _rgb_to_png_bytes(rgb)
    height, width = rgb.shape[:2]
    session = _post_json("/assets/uploads", token, base_url, {
        "kind": "image",
        "name": name,
        "mime_type": "image/png",
        "file_size": len(png),
        "duration_seconds": None,
        # Measured here so the engine can refuse an over-large image while the
        # session is still being opened, rather than after all the bytes arrive.
        "pixel_count": int(width) * int(height),
        "content_hash": hashlib.sha256(png).hexdigest(),
        "folder_id": None,
        "tags": ["img2psd"],
    })
    upload_id = session["upload_id"]
    part_size = int(session["part_size"])
    try:
        for index in range(int(session["max_parts"])):
            chunk = png[index * part_size:(index + 1) * part_size]
            if not chunk:
                break
            _request("PUT", f"{base_url}/api/v1/assets/uploads/{upload_id}/parts/{index + 1}",
                     token, body=chunk, content_type="application/octet-stream")
        done = _post_json(f"/assets/uploads/{upload_id}/complete", token, base_url)
    except BaseException:
        # A half-written session holds its parts in temp storage until the TTL
        # expires, so it is aborted even when the failure came from elsewhere.
        try:
            _request("DELETE", f"{base_url}/api/v1/assets/uploads/{upload_id}", token)
        except Exception:
            pass
        raise
    return done["asset_id"]


def _wait_for(generation_id: str, token: str, base_url: str,
              on_poll=None) -> dict:
    deadline = time.time() + POLL_TIMEOUT
    while True:
        record = _get_json(f"/generations/{generation_id}", token, base_url)
        if on_poll is not None:
            on_poll(record)
        if record.get("status") in TERMINAL_STATUSES:
            return record
        if time.time() >= deadline:
            # The generation itself is unaffected; only the waiting stopped, and
            # the id stays usable with `esora-api generate status`.
            raise RuntimeError(
                f"生成が {int(POLL_TIMEOUT)} 秒以内に終わりませんでした。"
                f"サーバ側では継続中です: {generation_id}"
            )
        time.sleep(POLL_INTERVAL)


def generate_image(model: str, prompt: str, source_rgb: np.ndarray,
                   lineart_rgb: np.ndarray = None, on_poll=None) -> np.ndarray:
    """Generate one image from ``prompt`` guided by ``source_rgb``.

    ``model`` takes the slot ``core.gemini.generate_image`` gave to the API key:
    Esora authenticates with a signed-in session rather than a per-call secret,
    so what a caller actually has to choose here is which model runs.

    Reference order is the wire contract and nothing labels the images for the
    model, so a prompt should say "1枚目の参照画像" rather than name a file.
    """
    token, base_url = _session()

    references = [_upload_reference(source_rgb, "img2psd_source.png", token, base_url)]
    if lineart_rgb is not None:
        references.append(
            _upload_reference(lineart_rgb, "img2psd_lineart.png", token, base_url))

    model = model or DEFAULT_MODEL
    body = {
        "type": "image",
        "prompt": prompt,
        "model": model,
        "reference_asset_ids": references,
        **_sized_body(model, token, base_url),
    }
    record = _post_json("/generations", token, base_url, body)
    record = _wait_for(record["id"], token, base_url, on_poll)

    if record.get("status") != "completed":
        raise RuntimeError(
            f"生成に失敗しました (status={record.get('status')}): "
            f"{record.get('error') or 'エラー詳細なし'}"
        )
    asset_ids = record.get("asset_ids") or []
    if not asset_ids:
        raise RuntimeError("生成は完了しましたが、画像が返りませんでした")

    raw = _request("GET", f"{base_url}/api/v1/assets/{asset_ids[0]}/content", token)
    bgr = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if bgr is None:
        raise RuntimeError("failed to decode generated image")
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
