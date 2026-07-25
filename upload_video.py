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
import textwrap
import datetime as dt

import requests
from PIL import Image, ImageDraw, ImageFont
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
THUMBNAIL_PATH_TEMPLATE = "output/thumbnail_{date}.png"

CHANNEL_HASHTAGS = "#Korea #KoreaNews #ShortsNews"

THUMBNAIL_SIZE = (1280, 720)  # 유튜브 권장 썸네일 해상도
FONT_PATH_CANDIDATES = [
    "assets/fonts/subtitle.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


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

    # 제목은 generate_script.py가 이미 만들어서 script.json에 저장해뒀다 -
    # 6단계 인트로 타이틀 카드와 여기 업로드 제목이 서로 달라지지 않도록 그 값을 그대로 쓴다.
    base_title = script.get("title")
    if not base_title:
        hook_line = next((t["line"] for t in script["turns"] if t["speaker"] == "customer"), lines[0])
        base_title = hook_line.rstrip("?.! ") + "?"
    title = f"{base_title} {CHANNEL_HASHTAGS.split()[0]}"

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
# 썸네일 생성 (제목 텍스트를 이미지로 렌더링)
# ---------------------------------------------------------------------------

def load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_PATH_CANDIDATES:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def build_thumbnail(title: str, out_path: str) -> None:
    """썸네일에 뜰 제목 이미지를 만든다. 인트로 타이틀 카드와 같은 문구를 쓴다."""
    img = Image.new("RGB", THUMBNAIL_SIZE, color=(10, 10, 10))
    draw = ImageDraw.Draw(img)

    font = load_font(size=72)
    wrapped_lines = textwrap.wrap(title, width=22)

    line_heights = []
    for line in wrapped_lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        line_heights.append(bbox[3] - bbox[1])
    line_spacing = 16
    total_height = sum(line_heights) + line_spacing * (len(wrapped_lines) - 1)

    y = (THUMBNAIL_SIZE[1] - total_height) // 2
    for line, h in zip(wrapped_lines, line_heights):
        bbox = draw.textbbox((0, 0), line, font=font)
        w = bbox[2] - bbox[0]
        x = (THUMBNAIL_SIZE[0] - w) // 2
        draw.text((x, y), line, font=font, fill=(255, 255, 255))
        y += h + line_spacing

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img.save(out_path)


def set_thumbnail(creds: Credentials, video_id: str, thumbnail_path: str) -> None:
    """업로드된 영상에 커스텀 썸네일을 지정한다.
    주의: 커스텀 썸네일 설정은 채널이 전화번호 인증(phone verification)을
    완료한 경우에만 가능하다. 안 되어 있으면 조용히 건너뛴다."""
    youtube = build("youtube", "v3", credentials=creds)
    try:
        youtube.thumbnails().set(
            videoId=video_id,
            media_body=MediaFileUpload(thumbnail_path, mimetype="image/png"),
        ).execute()
    except Exception as e:
        print(f"  [경고] 썸네일 설정 실패 (채널 전화번호 인증이 안 되어 있을 수 있습니다): {e}")


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

        print("[1/4] YouTube 인증 중...")
        creds = get_credentials()

        print("[2/4] 메타데이터(제목/설명/태그) 생성 중...")
        metadata = build_metadata(script)
        print(f"  제목: {metadata['title']}")

        print("[3/4] 업로드 중...")
        video_id = upload_video(creds, video_path, metadata)

        print("[4/4] 썸네일 생성 및 설정 중...")
        thumbnail_path = THUMBNAIL_PATH_TEMPLATE.format(date=today)
        build_thumbnail(script.get("title", metadata["title"]), thumbnail_path)
        set_thumbnail(creds, video_id, thumbnail_path)

        print(f"완료: https://youtube.com/shorts/{video_id}")

    except Exception as e:
        notify_failure(stage="upload_video", error=e)
        raise


if __name__ == "__main__":
    main()
