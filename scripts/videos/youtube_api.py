# /// script
# requires-python = ">=3.12"
# dependencies = ["google-api-python-client>=2.150", "google-auth-oauthlib>=1.2", "google-auth-httplib2>=0.2"]
# ///
"""Talk to the YouTube Data API for `python -m analysis.videos publish` (#395).

Runs under `uv run --script`, in its own environment, so the project doesn't
depend on Google's client libraries.

Reads one JSON request on stdin:
    {"secrets": dir holding client_secret.json (and token.json, written here),
     "video": id of a video already up, or absent with "upload",
     "upload": {"file": mp4, "status": {...},      # optional: a new video
                "notify": bool, "recording_date": "YYYY-MM-DDT00:00:00Z"},
     "snippet": {...},                              # title, description, ...
     "thumbnail": jpg, "captions": {"file": srt, "language": "en"},
     "playlist": "playlist title"}                  # optional, uploads only
and prints {"video": id}, with "error" too if a step failed (the video
may be up already, so its id still matters). With "video", only the snippet is updated: the
status (privacy above all) is left as it is in Studio.

The first run opens a browser to sign in to the channel's Google account
(or prints the URL to open); the token is kept in token.json beside the
client file.
"""

import contextlib
import json
import sys
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
CHUNK = 16 * 1024 * 1024


def credentials(secrets: Path) -> Credentials:
    token = secrets / "token.json"
    creds = Credentials.from_authorized_user_file(str(token), SCOPES) if token.is_file() else None
    if creds and creds.valid:
        return creds
    try:
        if not (creds and creds.expired and creds.refresh_token):
            raise RefreshError("no token to refresh")
        creds.refresh(Request())
    except RefreshError:
        # First use, or a sign-in Google has ended (after seven days while
        # the consent screen is in testing): sign in again.
        client = secrets / "client_secret.json"
        if not client.is_file():
            raise SystemExit(f"no {client}: download the OAuth client (Desktop app) there; see docs/videos/README.md")
        flow = InstalledAppFlow.from_client_secrets_file(str(client), SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True,
                                      authorization_prompt_message="Sign in to the channel's account: {url}\n")
    secrets.mkdir(parents=True, exist_ok=True)
    token.write_text(creds.to_json(), encoding="utf-8")
    token.chmod(0o600)
    return creds


def upload(yt, request: dict) -> str:
    body = {"snippet": request["snippet"], "status": request["upload"]["status"],
            "recordingDetails": {"recordingDate": request["upload"]["recording_date"]}}
    media = MediaFileUpload(request["upload"]["file"], mimetype="video/mp4", chunksize=CHUNK, resumable=True)
    call = yt.videos().insert(part="snippet,status,recordingDetails", body=body, media_body=media,
                              notifySubscribers=request["upload"]["notify"])
    response = None
    while response is None:
        status, response = call.next_chunk(num_retries=5)
        if status:
            print(f"  {int(status.progress() * 100)}%", file=sys.stderr)
    return response["id"]


def captions(yt, video: str, srt: str, language: str) -> None:
    body = {"snippet": {"videoId": video, "language": language, "name": "", "isDraft": False}}
    yt.captions().insert(part="snippet", body=body,
                         media_body=MediaFileUpload(srt, mimetype="application/octet-stream")).execute()


def playlist_id(yt, title: str) -> str:
    page = None
    while True:
        response = yt.playlists().list(part="snippet", mine=True, maxResults=50, pageToken=page).execute()
        for item in response.get("items", []):
            if item["snippet"]["title"] == title:
                return item["id"]
        page = response.get("nextPageToken")
        if not page:
            raise SystemExit(f"no playlist titled {title!r} on the channel (youtube.toml names it)")


def add_to_playlist(yt, video: str, title: str) -> None:
    playlist = playlist_id(yt, title)
    have = yt.playlistItems().list(part="id", playlistId=playlist, videoId=video).execute().get("items")
    if not have:
        body = {"snippet": {"playlistId": playlist, "resourceId": {"kind": "youtube#video", "videoId": video}}}
        yt.playlistItems().insert(part="snippet", body=body).execute()


def main() -> None:
    request = json.load(sys.stdin)
    answer: dict = {"video": request.get("video")}
    # Only the answer goes to stdout: the sign-in flow and the libraries
    # print there too (the sign-in URL above all), so send that to stderr.
    with contextlib.redirect_stdout(sys.stderr):
        try:
            run(request, answer)
        except (HttpError, SystemExit) as e:
            # The video may be up already: say so, so its id is kept.
            answer["error"] = f"{e.resp.status}: {e.reason}" if isinstance(e, HttpError) else str(e)
    print(json.dumps(answer))
    sys.exit(1 if answer.get("error") else 0)


def run(request: dict, answer: dict) -> None:
    yt = build("youtube", "v3", credentials=credentials(Path(request["secrets"])), cache_discovery=False)
    if request.get("upload"):
        answer["video"] = video = upload(yt, request)
        yt.thumbnails().set(videoId=video, media_body=MediaFileUpload(request["thumbnail"],
                                                                      mimetype="image/jpeg")).execute()
        captions(yt, video, request["captions"]["file"], request["captions"]["language"])
        if request.get("playlist"):
            add_to_playlist(yt, video, request["playlist"])
    else:
        yt.videos().update(part="snippet", body={"id": answer["video"], "snippet": request["snippet"]}).execute()


if __name__ == "__main__":
    main()
