"""
1일 1쇼츠 자동화 파이프라인 - 7단계: YouTube 업로드 + 모니터링

assemble_video.py가 만든 output/final_{date}.mp4 를 YouTube Shorts로 업로드하고,
제목/설명/태그를 자동 생성한다. 실패 시 Slack(또는 지정한 웹훅)으로 알림을 보낸다.

[수정 1] 날짜를 dt.date.today()로 새로 계산하지 않는다. build job과 upload job은
서로 다른 시점(특히 승인 대기 중 자정을 넘기는 경우)에 실행될 수 있어서,
"오늘 날짜"를 다시 계산하면 build가 실제로 만든 파일과 어긋난다. 대신
output/final_*.mp4 를 직접 찾아서 그 파일명에서 날짜를 읽어온다.

[수정 2] build_metadata()가 예전 "편의점 알바생 + 영어 해외채널" 버전 필드
(customer_character_en, speaker=="customer")를 쓰고 있었는데, 지금 콘텐츠
(언니삼총사 사연, 한국어)의 turns 구조와 안 맞아서 제목/설명 생성 코드를
새로 작성했다.

사전 준비물 (1회성, 사람이 직접 해야 하는 부분):
  1. Google Cloud Console에서 프로젝트 생성 -> YouTube Data API v3 활성화
  2. OAuth 2.0 클라이언트 ID(데스크톱 앱) 생성 -> client_secret.json 다운로드
  3. 이 스크립트를 로컬에서 한 번 실행해서 브라우저 인증을 완료하면
     token.json이 생성됨 (refresh token 포함)
  4. token.json을 GitHub Actions 시크릿(예: YOUTUBE_TOKEN_JSON)으로 등록해서
     무인 자동화 시 재사용

필요 환경변수:
  SLACK_WEBHOOK_URL   (선택 - 실패 알림용. 없으면 콘솔에만 출력)

필요 패키지:
  pip install google-auth-oauthlib google-api-python-client Pillow --break-system-packages
"""

import os
import re
import glob
import json
import textwrap

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

HASHTAGS = "#사연 #고민상담 #카톡썰 #언니들 #사이다"

THUMBNAIL_SIZE = (1280, 720)  # 유튜브 권장 썸네일 해상도
FONT_PATH_CANDIDATES = [
    "assets/subtitle.ttf",
    "assets/fonts/subtitle.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


# ---------------------------------------------------------------------------
# 날짜/파일 찾기
# ---------------------------------------------------------------------------

def find_latest_date() -> str:
    """output/final_*.mp4 파일명에서 날짜를 읽어온다(오늘 날짜를 새로 계산하지 않음).
    build job이 실제로 만든 그 날짜를 그대로 쓰기 위함."""
    paths = glob.glob(FINAL_VIDEO_PATH_TEMPLATE.format(date="*"))
    dates = []
    for p in paths:
        m = re.search(r"final_(\d{4}-\d{2}-\d{2})\.mp4$", os.path.basename(p))
        if m:
            dates.append(m.group(1))
    if not dates:
        raise FileNotFoundError(
            "output/final_*.mp4 파일을 찾을 수 없습니다. "
            "assemble_video.py를 먼저 실행했는지, artifact가 정상적으로 전달됐는지 확인하세요."
        )
    return sorted(dates)[-1]


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
# 제목/설명/태그 자동 생성 (언니삼총사 사연 콘텐츠용)
# ---------------------------------------------------------------------------

def build_metadata(script: dict) -> dict:
    title = script.get("title") or "언니들의 사연"

    story_lines = [t["line"] for t in script["turns"] if t["speaker"] == "reporter"]
    story_summary = " ".join(story_lines)
    if len(story_summary) > 300:
        story_summary = story_summary[:297] + "..."

    question = next((t["line"] for t in script["turns"] if t["speaker"] == "question"), "")

    description = (
        f"{story_summary}\n\n"
        f"{question}\n\n"
        "쓰레드에서 만난 언니들의 진짜 조언 💬\n\n"
        f"{HASHTAGS}"
    )

    tags = ["사연", "고민상담", "카톡썰", "언니들", "사이다", "썰", "인간관계"]

    return {"title": title, "description": description, "tags": tags}


# ---------------------------------------------------------------------------
# 썸네일 생성 (제목 텍스트를 이미지로 렌더링)
# ---------------------------------------------------------------------------

def load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_PATH_CANDIDATES:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    raise SystemExit(
        "썸네일용 한글 폰트를 찾을 수 없습니다. assets/subtitle.ttf 가 있는지 확인하세요."
    )


def build_thumbnail(title: str, out_path: str) -> None:
    img = Image.new("RGB", THUMBNAIL_SIZE, color=(10, 10, 10))
    draw = ImageDraw.Draw(img)

    font = load_font(size=72)
    wrapped_lines = textwrap.wrap(title, width=18)

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
            "categoryId": "24",  # Entertainment
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
    try:
        date = find_latest_date()
        print(f"대상 날짜: {date}")

        video_path = FINAL_VIDEO_PATH_TEMPLATE.format(date=date)
        script_path = SCRIPT_PATH_TEMPLATE.format(date=date)

        if not os.path.exists(script_path):
            raise FileNotFoundError(f"{script_path} 가 없습니다. fetch_script.py를 먼저 실행하세요.")

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
        thumbnail_path = THUMBNAIL_PATH_TEMPLATE.format(date=date)
        build_thumbnail(script.get("title", metadata["title"]), thumbnail_path)
        set_thumbnail(creds, video_id, thumbnail_path)

        print(f"완료: https://youtube.com/shorts/{video_id}")

    except Exception as e:
        notify_failure(stage="upload_video", error=e)
        raise


if __name__ == "__main__":
    main()
