#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

BASE = Path(__file__).resolve().parent
CREDENTIALS_FILE = BASE / "credentials.json"
TOKEN_FILE = BASE / "token.json"
SCOPES = [
    "https://www.googleapis.com/auth/youtube",
]

def svc():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)

def whoami(y):
    me = y.channels().list(part="id,snippet", mine=True).execute()
    it = (me.get("items") or [None])[0]
    if not it:
        return None, None
    return it["id"], it["snippet"]["title"]

def main():
    if len(sys.argv) < 2:
        print("Uso: python check_video.py <VIDEO_ID>")
        sys.exit(2)
    vid = sys.argv[1]

    y = svc()
    ch_id, ch_title = whoami(y)
    print(f"👤 Canal autenticado: {ch_title} ({ch_id})")

    r = y.videos().list(part="status,snippet", id=vid).execute()
    it = (r.get("items") or [None])[0]
    if not it:
        print("⚠️  No existe o no tienes permiso para ver ese video.")
        sys.exit(1)

    vis = it["status"]["privacyStatus"]
    v_ch = it["snippet"]["channelId"]
    v_ch_title = it["snippet"]["channelTitle"]
    print(f"🆔 Video: {vid}")
    print(f"🔎 Visibilidad: {vis}")
    print(f"📺 Canal del video: {v_ch_title} ({v_ch})")
    print(f"🔗 Watch: https://youtu.be/{vid}")
    print(f"🎛️ Studio: https://studio.youtube.com/video/{vid}/edit")

if __name__ == "__main__":
    main()