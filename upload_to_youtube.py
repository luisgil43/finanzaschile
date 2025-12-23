#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import base64
import datetime as dt
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

try:
    import importlib.metadata as _im
    _ = _im.packages_distributions
except Exception:
    try:
        import importlib.metadata as _im

        import importlib_metadata as _imb
        _im.packages_distributions = _imb.packages_distributions  # type: ignore[attr-defined]
    except Exception:
        try:
            import importlib.metadata as _im
            _im.packages_distributions = lambda: {}  # type: ignore[attr-defined]
        except Exception:
            pass

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

BASE = Path(__file__).resolve().parent
CREDENTIALS_FILE = BASE / "credentials.json"
TOKEN_FILE = BASE / "token.json"

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]

SHORTS_MAX_SECONDS = float(os.getenv("YT_SHORTS_MAX_SECONDS", "60"))

UPLOAD_NORMAL = os.getenv("UPLOAD_NORMAL", "1") == "1"
UPLOAD_SHORT = os.getenv("UPLOAD_SHORT", "1") == "1"


def _env_b64_present(name: str) -> bool:
    v = os.getenv(name, "")
    return bool(v and v.strip())


def _write_env_b64(name: str, path: Path):
    raw = base64.b64decode(os.getenv(name, "").encode("utf-8"))
    path.write_bytes(raw)


def get_service():
    creds = None

    credentials_file = CREDENTIALS_FILE
    token_file = TOKEN_FILE

    using_env = _env_b64_present("YT_CREDENTIALS_JSON_B64") and _env_b64_present("YT_TOKEN_JSON_B64")
    if using_env:
        runtime = BASE / ".runtime"
        runtime.mkdir(exist_ok=True)
        credentials_file = runtime / "credentials.json"
        token_file = runtime / "token.json"
        _write_env_b64("YT_CREDENTIALS_JSON_B64", credentials_file)
        _write_env_b64("YT_TOKEN_JSON_B64", token_file)

    if token_file.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
        except Exception as e:
            print(f"⚠️ No se pudo leer token.json ({e}), pidiendo login nuevo...")
            creds = None

    headless = bool(os.getenv("RENDER")) or bool(os.getenv("RENDER_SERVICE_ID")) or (os.getenv("HEADLESS") == "1") or (not sys.stdin.isatty())

    if not creds or not creds.valid:
        try:
            if creds and creds.expired and creds.refresh_token:
                print("🔄 Intentando refrescar token de YouTube...")
                creds.refresh(Request())
            else:
                raise RefreshError("No hay refresh_token, hay que reautenticar.")
        except RefreshError as e:
            if using_env or headless:
                raise RuntimeError(
                    "Token inválido/expirado y estoy en modo headless (Render). "
                    "Genera token.json LOCAL con navegador, conviértelo a Base64 y súbelo a "
                    "YT_TOKEN_JSON_B64 / YT_CREDENTIALS_JSON_B64."
                ) from e

            print(f"⚠️ Token inválido/expirado ({e}). Eliminando token.json y pidiendo login de nuevo...")
            try:
                if token_file.exists():
                    token_file.unlink()
            except Exception as rm_err:
                print(f"⚠️ No se pudo borrar token.json: {rm_err}")

            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), SCOPES)
            creds = flow.run_local_server(port=0)

        token_file.write_text(creds.to_json(), encoding="utf-8")

    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def whoami(youtube):
    me = youtube.channels().list(part="id,snippet", mine=True).execute()
    items = me.get("items") or []
    if not items:
        return None, None
    ch = items[0]
    return ch.get("id"), ch.get("snippet", {}).get("title")


def upload_video(youtube, video_path: Path, title: str, description: str, privacy: str = "public") -> Optional[str]:
    body = {
        "snippet": {"title": title, "description": description, "categoryId": "22"},
        "status": {"privacyStatus": privacy},
    }
    media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True)
    req = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = req.next_chunk()
        if status:
            print(f"⏫ Upload {int(status.progress() * 100)}%")

    return response.get("id")


def _ffprobe_duration_seconds(path: Path) -> Optional[float]:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return float(out) if out else None
    except Exception:
        return None


def main():
    youtube = get_service()
    ch_id, ch_title = whoami(youtube)
    print(f"👤 Canal autenticado: {ch_title} ({ch_id})")

    video = BASE / "out" / "finanzas_hoy.mp4"
    short_video = BASE / "out" / "finanzas_hoy_short.mp4"

    # ✅ FIX: DD-MM-YYYY
    date_str = dt.datetime.now().strftime("%d-%m-%Y")

    title = os.getenv("YT_TITLE_TEMPLATE", "Finanzas Hoy Chile - {date}").format(date=date_str)
    description = os.getenv("YT_DESCRIPTION")
    privacy = os.getenv("YT_PRIVACY", "public")
    short_title = os.getenv("YT_SHORT_TITLE_TEMPLATE", "Finanzas Hoy Chile - {date} #Shorts").format(date=date_str)

    try:
        if UPLOAD_NORMAL:
            if not video.exists():
                print(f"❌ No existe el video normal: {video}")
            else:
                vid = upload_video(youtube, video, title=title, description=description, privacy=privacy)
                print(f"✅ Video subido. ID: {vid} | privacidad: {privacy}")
                if vid:
                    print(f"UPLOAD_RESULT kind=normal id={vid} privacy={privacy}")

        if UPLOAD_SHORT:
            if not short_video.exists():
                print(f"❌ No existe el short: {short_video}")
            else:
                dur = _ffprobe_duration_seconds(short_video)
                if dur is None:
                    print("⚠️ No pude leer duración del short con ffprobe. No lo subo por seguridad.")
                elif dur > SHORTS_MAX_SECONDS:
                    print(f"⚠️ Short NO subido: dura {dur:.1f}s y el máximo es {SHORTS_MAX_SECONDS:.0f}s.")
                else:
                    desc_short = description or ""
                    if "#shorts" not in desc_short.lower():
                        desc_short = desc_short.rstrip() + "\n\n#Shorts"

                    vid_s = upload_video(youtube, short_video, title=short_title, description=desc_short, privacy=privacy)
                    print(f"✅ Short subido. ID: {vid_s} | privacidad: {privacy}")
                    if vid_s:
                        print(f"UPLOAD_RESULT kind=short id={vid_s} privacy={privacy}")

    except HttpError as e:
        print(f"❌ Error YouTube API: {e}")
        raise


if __name__ == "__main__":
    main()