"""
1일 1쇼츠 자동화 파이프라인 - 7단계: YouTube 업로드 + 모니터링

assemble_video.py가 만든 output/final_{date}.mp4 를 YouTube Shorts로 업로드하고,
제목/설명/태그를 자동 생성한다. 실패 시 Slack(또는 지정한 웹훅)으로 알림을 보낸다.

사전 준비물 (1회성, 사람이 직접 해야 하는 부분):
  1. Google Cloud Console에서 프로젝트 생성 -> YouTube Data API v3 활성화
  2. OAuth 2.0 클라이언트 ID(데스크톱 앱) 생성 -> client_secret.json 다운로드
  3. 이 스크립트를 로컬에서 한 번 실행해서 브라우저 인증을 완료하면
     token.json이 생성됨 (refresh token 포함)
  4. token.json을 GitHub Actions 시크릿(예: YOUTUBE_TOKEN_JSON)으로 등록해서
     무인 자동화 시 재사용

이 인증 단계는 보안상 사람이 최소 1회는 직접 브라우저로 로그인/승인해야 해서
완전히 자동화할 수 없다. 그 이후부터는 refresh token으로 무인 갱신된다.

필요 환경변수:
  SLACK_WEBHOOK_URL   (선택 - 실패 알림용. 없으면 콘솔에만 출력)

필요 패키지:
  pip install google-auth-oauthlib google-api-python-client --break-system-packages
"""

import os
import json
import datetime as dt

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

CLIENT_SECRET_PATH = "client_secret.json"
TOKEN_PATH = "token.json"

FINAL_VIDEO_PATH_TEMPLATE = "output/final_{date}.mp4"
SCRIPT_PATH_TEMPLATE = "output/script_{date}.json"

CHANNEL_HASHTAGS = "#Korea #KoreaNews #ShortsNews"


# ---------------------------------------------------------------------------
# 인증
# ---------------------------------------------------------------------------

def get_credentials() -> Credentials:
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CLIENT_SECRET_PATH):
                raise SystemExit(
                    f"{CLIENT_SECRET_PATH} 가 없습니다. Google Cloud Console에서 "
                    "OAuth 클라이언트를 만들고 다운로드한 파일을 이 경로에 두세요."
                )
            print("최초 1회 인증이 필요합니다. 브라우저가 열립니다...")
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_PATH, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_PATH, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    return creds


# ---------------------------------------------------------------------------
# 제목/설명/태그 자동 생성
# ---------------------------------------------------------------------------

def build_metadata(script: dict) -> dict:
    """제목/설명은 (한국어일 수 있는) facts가 아니라, 이미 영어로 만들어진
    대사(script.json turns)에서 뽑는다. 해외 시청자 대상 채널이라 메타데이터에
    한국어가 섞여 나오면 안 된다."""
    lines = [t["line"] for t in script["turns"]]
    customer_char = script.get("customer_character_en", "a regular")

    # 손님의 첫 대사(보통 사건을 언급하는 훅 문장)를 제목 재료로 사용
    hook_line = next((t["line"] for t in script["turns"] if t["speaker"] == "customer"), lines[0])
    title = hook_line.rstrip("?.! ")
    if len(title) > 90:
        title = title[:87] + "..."
    title = f"{title}? {CHANNEL_HASHTAGS.split()[0]}"

    transcript = "\n".join(f"- {l}" for l in lines)
    description = (
        f"A late-night conversation at a Korean convenience store, with {customer_char}.\n\n"
        f"{transcript}\n\n"
        f"{CHANNEL_HASHTAGS}\n\n"
        "This is a dramatized retelling based on public news reports. "
        "Names and identifying details have been altered."
    )
    tags = ["Korea", "Korea news", "shorts", "true story", "convenience store"]

    return {"title": title, "description": description, "tags": tags}


# ---------------------------------------------------------------------------
# 업로드
# ---------------------------------------------------------------------------

def upload_video(creds: Credentials, video_path: str, metadata: dict) -> str:
    youtube = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": metadata["title"],
            "description": metadata["description"],
            "tags": metadata["tags"],
            "categoryId": "25",  # News & Politics
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"  업로드 진행률: {int(status.progress() * 100)}%")

    return response["id"]


# ---------------------------------------------------------------------------
# 실패 알림
# ---------------------------------------------------------------------------

def notify_failure(stage: str, error: Exception) -> None:
    message = f":rotating_light: 쇼츠 자동화 파이프라인 실패 - [{stage}] {error}"
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")

    print(message)
    if webhook_url:
        try:
            requests.post(webhook_url, json={"text": message}, timeout=10)
        except Exception as e:
            print(f"  Slack 알림 전송도 실패했습니다: {e}")


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------

def main():
    today = dt.date.today().isoformat()
    video_path = FINAL_VIDEO_PATH_TEMPLATE.format(date=today)
    script_path = SCRIPT_PATH_TEMPLATE.format(date=today)

    try:
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"{video_path} 가 없습니다. assemble_video.py를 먼저 실행하세요.")
        if not os.path.exists(script_path):
            raise FileNotFoundError(f"{script_path} 가 없습니다. generate_script.py를 먼저 실행하세요.")

        with open(script_path, "r", encoding="utf-8") as f:
            script = json.load(f)

        print("[1/3] YouTube 인증 중...")
        creds = get_credentials()

        print("[2/3] 메타데이터(제목/설명/태그) 생성 중...")
        metadata = build_metadata(script)
        print(f"  제목: {metadata['title']}")

        print("[3/3] 업로드 중...")
        video_id = upload_video(creds, video_path, metadata)

        print(f"완료: https://youtube.com/shorts/{video_id}")

    except Exception as e:
        notify_failure(stage="upload_video", error=e)
        raise


if __name__ == "__main__":
    main()
