"""
1일 1쇼츠 자동화 파이프라인 - 4단계 신규: 음성/이미지 생성

fetch_script.py가 만든 output/script_{date}.json(turns 배열)을 입력으로 받아,
turn마다:
  1) ElevenLabs로 그 줄의 대사를 TTS 음성 파일로 만들고
  2) 그 시점까지의 카카오톡풍 채팅 화면을 PIL로 직접 그려서 이미지로 저장한다
     (AI 이미지 생성 API를 쓰지 않으므로 이미지 생성 비용은 $0)
turn별 image_path/audio_path를 채운 output/manifest_{date}.json을 출력한다.
이 manifest.json은 기존 assemble_video.py가 그대로 읽어서 영상으로 조립한다.

필요 환경변수:
  ELEVENLABS_API_KEY

필요 패키지:
  pip install elevenlabs Pillow --break-system-packages

필요 폰트(한글 렌더링):
  시스템에 나눔고딕 등 한글 TTF가 있어야 한다.
  GitHub Actions(ubuntu-latest)라면 워크플로에 다음 스텝 추가 필요:
    sudo apt-get install -y fonts-nanum
"""

import os
import json
import datetime as dt

from elevenlabs.client import ElevenLabs
from elevenlabs import save
from PIL import Image, ImageDraw, ImageFont

SCRIPT_PATH_TEMPLATE = "output/script_{date}.json"
MANIFEST_PATH_TEMPLATE = "output/manifest_{date}.json"
SEGMENTS_DIR_TEMPLATE = "output/segments_{date}"

W, H = 1080, 1920
HEADER_H = 160
FEED_TOP = HEADER_H + 40
FEED_BOTTOM = H - 60  # 이 아래는 assemble_video.py가 자막(drawtext)을 그릴 여백으로 남겨둠
BUBBLE_MAX_WIDTH = 760
BUBBLE_PADDING = 28
BUBBLE_GAP = 22
AVATAR_SIZE = 56

BG_COLOR = (247, 247, 245)
HEADER_BG = (255, 255, 255)
HEADER_BORDER = (225, 225, 220)
TEXT_MUTED = (140, 140, 135)

SPEAKER_STYLE = {
    "reporter": {"side": "right", "bubble": (255, 255, 255), "text": (30, 30, 30), "avatar": None},
    "real":     {"side": "left",  "bubble": (198, 224, 251), "text": (12, 68, 124), "avatar": (55, 138, 221)},
    "empathy":  {"side": "left",  "bubble": (208, 233, 176), "text": (39, 80, 10),  "avatar": (99, 153, 34)},
    "rage":     {"side": "left",  "bubble": (247, 193, 193), "text": (121, 31, 31), "avatar": (226, 75, 74)},
    "question": {"side": "center", "bubble": (250, 199, 117), "text": (65, 36, 2),  "avatar": None},
}

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
]


def get_font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    raise SystemExit(
        "한글 TTF 폰트를 찾을 수 없습니다. "
        "GitHub Actions 워크플로에 'sudo apt-get install -y fonts-nanum' 스텝을 추가하세요."
    )


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    words = text.split(" ")
    lines, current = [], ""
    for w in words:
        trial = f"{current} {w}".strip()
        if draw.textlength(trial, font=font) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = w
    if current:
        lines.append(current)
    return lines


def measure_bubble(draw, text, font, line_height):
    lines = wrap_text(draw, text, font, BUBBLE_MAX_WIDTH - BUBBLE_PADDING * 2)
    text_w = max(draw.textlength(l, font=font) for l in lines)
    bubble_w = int(text_w) + BUBBLE_PADDING * 2
    bubble_h = int(len(lines) * line_height) + BUBBLE_PADDING * 2
    return lines, bubble_w, bubble_h


def draw_header(draw: ImageDraw.ImageDraw, font_title, font_sub):
    draw.rectangle([0, 0, W, HEADER_H], fill=HEADER_BG)
    draw.line([0, HEADER_H, W, HEADER_H], fill=HEADER_BORDER, width=2)
    draw.text((44, 46), "언니들 단톡방", font=font_title, fill=(20, 20, 20))
    draw.text((44, 100), "쓰레드에서 만난 사이 · 4", font=font_sub, fill=TEXT_MUTED)


def render_chat_frame(turns_so_far: list[dict], font, font_small, line_height) -> Image.Image:
    img = Image.new("RGB", (W, H), BG_COLOR)
    draw = ImageDraw.Draw(img)
    draw_header(draw, get_font(40), get_font(26))

    # 첫 메시지가 화면 위쪽(FEED_TOP)에서 시작해서, 새 메시지가 그 아래로 순서대로
    # 쌓이는 방식. 자동 스크롤 없음 - turn이 많아지면 마지막 몇 개는 화면 하단
    # 밖으로 나갈 수 있음(에피소드당 turn 수가 많으면 주의).
    blocks = []
    for turn in turns_so_far:
        style = SPEAKER_STYLE[turn["speaker"]]
        lines, bubble_w, bubble_h = measure_bubble(draw, turn["line"], font, line_height)
        blocks.append((turn, style, lines, bubble_w, bubble_h))

    y = FEED_TOP
    for turn, style, lines, bubble_w, bubble_h in blocks:
        side = style["side"]
        if side == "center":
            x0 = (W - bubble_w) // 2
        elif side == "right":
            x0 = W - 44 - bubble_w
        else:
            x0 = 44 + (AVATAR_SIZE + 16 if style["avatar"] else 0)

        x1 = x0 + bubble_w
        y0 = y
        y1 = y0 + bubble_h

        if style["avatar"] and side == "left":
            ax = 44
            ay = y0 + bubble_h - AVATAR_SIZE
            draw.ellipse([ax, ay, ax + AVATAR_SIZE, ay + AVATAR_SIZE], fill=style["avatar"])
            initial = turn["character_name"][0] if turn["character_name"] else "?"
            iw = draw.textlength(initial, font=font_small)
            draw.text((ax + AVATAR_SIZE / 2 - iw / 2, ay + AVATAR_SIZE / 2 - 16),
                       initial, font=font_small, fill=(255, 255, 255))

        radius = 26
        draw.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=style["bubble"])

        ty = y0 + BUBBLE_PADDING
        for line in lines:
            draw.text((x0 + BUBBLE_PADDING, ty), line, font=font, fill=style["text"])
            ty += line_height

        y = y1 + BUBBLE_GAP

    return img


def synth_audio(client: ElevenLabs, text: str, voice_id: str, out_path: str) -> None:
    audio = client.text_to_speech.convert(
        voice_id=voice_id,
        text=text,
        model_id="eleven_multilingual_v2",
    )
    save(audio, out_path)


def main():
    today = dt.date.today().isoformat()
    script_path = SCRIPT_PATH_TEMPLATE.format(date=today)
    if not os.path.exists(script_path):
        raise SystemExit(f"{script_path} 가 없습니다. fetch_script.py를 먼저 실행하세요.")

    with open(script_path, encoding="utf-8") as f:
        script = json.load(f)

    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise SystemExit("ELEVENLABS_API_KEY 환경변수가 필요합니다.")
    client = ElevenLabs(api_key=api_key)

    segments_dir = SEGMENTS_DIR_TEMPLATE.format(date=today)
    audio_dir = os.path.join(segments_dir, "audio")
    image_dir = os.path.join(segments_dir, "images")
    os.makedirs(audio_dir, exist_ok=True)
    os.makedirs(image_dir, exist_ok=True)

    font = get_font(36)
    font_small = get_font(24)
    line_height = 48

    turns = script["turns"]
    manifest_turns = []

    for i, turn in enumerate(turns):
        print(f"[{i + 1}/{len(turns)}] {turn['character_name'] or '질문'}: {turn['line'][:30]}...")

        audio_path = os.path.join(audio_dir, f"{turn['index']:02d}.mp3")
        synth_audio(client, turn["line"], turn["voice_id"], audio_path)

        image_path = os.path.join(image_dir, f"{turn['index']:02d}.png")
        frame = render_chat_frame(turns[: i + 1], font, font_small, line_height)
        frame.save(image_path)

        manifest_turns.append({
            "index": turn["index"],
            "character_name": turn["character_name"] or "질문",
            "line": turn["line"],
            "audio_path": audio_path,
            "image_path": image_path,
        })

    manifest = {"date": today, "turns": manifest_turns}
    out_path = MANIFEST_PATH_TEMPLATE.format(date=today)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"완료: {out_path} (turn {len(manifest_turns)}개)")


if __name__ == "__main__":
    main()
