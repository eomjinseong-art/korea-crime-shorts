"""
1일 1쇼츠 자동화 파이프라인 - 4단계: 음성/이미지 생성 (단일 낭독 방식)

fetch_script.py가 만든 output/script_{date}.json을 입력으로 받는다.

[방식] script["narration"] 전체를 ElevenLabs에 딱 한 번만 호출해서
음성 파일 하나(narration.mp3)로 만든다(유니가 팟캐스트 진행자처럼
전체를 이어서 낭독). 화면은 기존처럼 turn(말풍선) 단위로 카톡 UI를
PIL로 그리되, 각 turn이 화면에 떠있는 시간은 그 turn의 weight_text
길이가 전체 낭독 텍스트에서 차지하는 비율만큼을 전체 낭독 길이에서
나눠 배분한다(정확한 단어 단위 타임스탬프는 아니지만 충분히 자연스러운
근사치).

출력: output/manifest_{date}.json
  {
    "narration_audio": "...",
    "narration_duration": 12.3,
    "turns": [{"index","image_path","duration","character_name","line"}, ...]
  }

필요 환경변수:
  ELEVENLABS_API_KEY

필요 패키지:
  pip install elevenlabs Pillow --break-system-packages

필요 폰트(한글 렌더링):
  GitHub Actions(ubuntu-latest) 워크플로에 다음 스텝 필요:
    sudo apt-get install -y fonts-nanum
"""

import os
import json
import subprocess
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
FEED_BOTTOM = H - 60
BUBBLE_MAX_WIDTH = 760
BUBBLE_PADDING = 28
BUBBLE_GAP = 22
AVATAR_SIZE = 56

MIN_TURN_DURATION = 1.2  # 아무리 짧아도 최소 이 정도는 화면에 보여줌(초)

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


def get_audio_duration(path: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe 실패: {result.stderr}")
    return float(result.stdout.strip())


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

    all_blocks = []
    for turn in turns_so_far:
        style = SPEAKER_STYLE[turn["speaker"]]
        lines, bubble_w, bubble_h = measure_bubble(draw, turn["line"], font, line_height)
        all_blocks.append((turn, style, lines, bubble_w, bubble_h))

    total_height = sum(b[4] + BUBBLE_GAP for b in all_blocks)
    available_height = FEED_BOTTOM - FEED_TOP

    if total_height <= available_height:
        blocks = all_blocks
        y_start = FEED_TOP
    else:
        blocks = []
        y_cursor = FEED_BOTTOM
        for turn, style, lines, bubble_w, bubble_h in reversed(all_blocks):
            block_h = bubble_h + BUBBLE_GAP
            if y_cursor - block_h < FEED_TOP and blocks:
                break
            blocks.append((turn, style, lines, bubble_w, bubble_h))
            y_cursor -= block_h
        blocks.reverse()
        y_start = max(y_cursor, FEED_TOP)

    y = y_start
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


def allocate_durations(turns: list[dict], total_duration: float) -> list[float]:
    """turn별 weight_text 글자수 비율로 total_duration을 나눠 배분.
    최소 MIN_TURN_DURATION은 보장하고, 남는/모자란 시간은 마지막 turn에서 보정."""
    weights = [max(len(t["weight_text"]), 1) for t in turns]
    total_weight = sum(weights)
    raw = [total_duration * (w / total_weight) for w in weights]
    durations = [max(d, MIN_TURN_DURATION) for d in raw]

    # 최소값 보정으로 합계가 total_duration을 넘어가면, 넘는 만큼을 가장 긴 turn들에서 비례 차감
    diff = sum(durations) - total_duration
    if diff > 0:
        adjustable_idx = [i for i, d in enumerate(durations) if d > MIN_TURN_DURATION]
        adjustable_total = sum(durations[i] for i in adjustable_idx) or 1
        for i in adjustable_idx:
            durations[i] -= diff * (durations[i] / adjustable_total)
        durations = [max(d, 0.4) for d in durations]

    return durations


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

    # 1) 낭독 전체를 한 번에 TTS
    print("낭독 음성 생성 중 (유니, 1회 호출)...")
    narration_audio_path = os.path.join(audio_dir, "narration.mp3")
    synth_audio(client, script["narration"], script["voice_id"], narration_audio_path)
    total_duration = get_audio_duration(narration_audio_path)
    print(f"낭독 길이: {total_duration:.2f}초")

    # 2) turn별 표시 시간 배분
    turns = script["turns"]
    durations = allocate_durations(turns, total_duration)

    # 3) turn별 누적 카톡 화면 렌더링
    font = get_font(36)
    font_small = get_font(24)
    line_height = 48

    manifest_turns = []
    for i, turn in enumerate(turns):
        print(f"[{i + 1}/{len(turns)}] {turn['character_name'] or '질문'}: "
              f"{turn['line'][:30]}... ({durations[i]:.2f}초)")

        image_path = os.path.join(image_dir, f"{turn['index']:02d}.png")
        frame = render_chat_frame(turns[: i + 1], font, font_small, line_height)
        frame.save(image_path)

        manifest_turns.append({
            "index": turn["index"],
            "character_name": turn["character_name"] or "질문",
            "line": turn["line"],
            "image_path": image_path,
            "duration": durations[i],
        })

    manifest = {
        "date": today,
        "narration_audio": narration_audio_path,
        "narration_duration": total_duration,
        "turns": manifest_turns,
    }
    out_path = MANIFEST_PATH_TEMPLATE.format(date=today)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"완료: {out_path} (turn {len(manifest_turns)}개, 낭독 {total_duration:.2f}초)")


if __name__ == "__main__":
    main()
