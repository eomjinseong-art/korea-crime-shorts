"""
1일 1쇼츠 자동화 파이프라인 - 6단계: 영상 조립 (단일 낭독 방식)

generate_media.py가 만든 output/manifest_{date}.json을 받아서:
  1) turn별 이미지를 그 turn에 배분된 duration만큼 보여주는 무음 영상
     세그먼트로 만들고
  2) 전부 이어붙인 뒤(여전히 무음)
  3) 인트로 타이틀 카드(무음) 앞에 붙이고
  4) 마지막에 낭독 음성(narration_audio) 하나를 통째로 입힌다
     (타이틀 카드 길이만큼 딜레이를 줘서 타이밍을 맞춤) + BGM(있으면)

기존 버전과의 차이: 예전엔 turn마다 오디오+이미지를 같이 붙인 세그먼트를
만들었지만, 이번엔 음성이 turn별로 쪼개져 있지 않고 하나뿐이라 영상을
전부 무음으로 조립한 다음 마지막에 음성 트랙 하나를 입히는 방식으로 바뀌었다.

필요 프로그램: ffmpeg, ffprobe (PATH에 있어야 함)

사전 준비물:
  assets/bgm.mp3      - 배경음악 (없으면 BGM 없이 렌더링)
  assets/subtitle.ttf - 한글 지원 폰트 (인트로 타이틀 카드용, 필수)

출력:
  output/final_{date}.mp4
"""

import os
import json
import subprocess
import datetime as dt
import tempfile

MANIFEST_PATH_TEMPLATE = "output/manifest_{date}.json"
SCRIPT_PATH_TEMPLATE = "output/script_{date}.json"
SEGMENTS_DIR_TEMPLATE = "output/segments_{date}"
FINAL_PATH_TEMPLATE = "output/final_{date}.mp4"
CONFIG_PATH = "config.json"

BGM_PATH = "assets/bgm.mp3"
FONT_PATH = "assets/subtitle.ttf"

VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
BGM_VOLUME = 0.15

TITLE_CARD_BG_COLOR = "black"


def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        raise SystemExit(f"{CONFIG_PATH} 가 없습니다. 리포지토리 루트에 config.json을 두세요.")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"명령 실행 실패: {' '.join(cmd)}\n--- stderr ---\n{result.stderr}"
        )


def get_duration(path: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe 실패: {result.stderr}")
    return float(result.stdout.strip())


# ---------------------------------------------------------------------------
# 세그먼트(turn 하나) 렌더링 - 이미지 + 무음, 오디오는 나중에 한 번에 입힘
# ---------------------------------------------------------------------------

def build_segment(image_path: str, duration: float, out_path: str) -> None:
    vf = (
        f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT}"
    )
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    run([
        "ffmpeg", "-y",
        "-loop", "1", "-i", image_path,
        "-vf", vf,
        "-t", str(duration),
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-an",
        out_path,
    ])


def build_title_card(title: str, out_path: str, duration: float) -> None:
    """영상 시작 전에 짧게 보여줄 타이틀 카드(검은 배경 + 제목 텍스트), 무음."""
    if not os.path.exists(FONT_PATH):
        raise SystemExit(
            f"{FONT_PATH} 가 없습니다. 한글 폰트 파일을 이 경로에 추가하세요 "
            "(그렇지 않으면 타이틀 카드의 한글이 깨져 보입니다)."
        )

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(title)
        title_file = tf.name

    drawtext = (
        f"drawtext=textfile='{title_file}':fontfile={FONT_PATH}:"
        "fontsize=56:fontcolor=white:box=0:"
        "x=(w-text_w)/2:y=(h-text_h)/2:line_spacing=10"
    )

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    run([
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"color=c={TITLE_CARD_BG_COLOR}:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:d={duration}",
        "-vf", drawtext,
        "-t", str(duration),
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-an",
        out_path,
    ])

    os.unlink(title_file)


def concat_segments(segment_paths: list[str], out_path: str) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as tf:
        for p in segment_paths:
            tf.write(f"file '{os.path.abspath(p)}'\n")
        list_file = tf.name

    run([
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", list_file,
        "-c", "copy",
        out_path,
    ])
    os.unlink(list_file)


def mux_narration(video_path: str, narration_path: str, delay_sec: float, out_path: str) -> None:
    """무음 영상에 낭독 음성을 delay_sec 만큼 늦춰서 입히고, BGM이 있으면 같이 믹싱."""
    total_duration = get_duration(video_path)
    delay_ms = max(int(round(delay_sec * 1000)), 0)

    if os.path.exists(BGM_PATH):
        filter_complex = (
            f"[1:a]adelay={delay_ms}|{delay_ms}[voice];"
            f"[2:a]volume={BGM_VOLUME},atrim=0:{total_duration},aloop=loop=-1:size=2e9[bgm];"
            "[voice][bgm]amix=inputs=2:duration=first:dropout_transition=2[aout]"
        )
        run([
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", narration_path,
            "-stream_loop", "-1", "-i", BGM_PATH,
            "-filter_complex", filter_complex,
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac",
            "-shortest",
            out_path,
        ])
    else:
        filter_complex = f"[1:a]adelay={delay_ms}|{delay_ms}[aout]"
        run([
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", narration_path,
            "-filter_complex", filter_complex,
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac",
            "-shortest",
            out_path,
        ])


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------

def main():
    today = dt.date.today().isoformat()
    manifest_path = MANIFEST_PATH_TEMPLATE.format(date=today)
    if not os.path.exists(manifest_path):
        raise SystemExit(f"{manifest_path} 가 없습니다. generate_media.py를 먼저 실행하세요.")

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    script_path = SCRIPT_PATH_TEMPLATE.format(date=today)
    title = None
    if os.path.exists(script_path):
        with open(script_path, "r", encoding="utf-8") as f:
            title = json.load(f).get("title")

    config = load_config()
    title_card_duration = config.get("title_card_duration", 2.5)

    segments_dir = SEGMENTS_DIR_TEMPLATE.format(date=today)
    segment_paths = []
    lead_in = 0.0

    if title:
        print("인트로 타이틀 카드 렌더링 중...")
        title_card_path = os.path.join(segments_dir, "00_title.mp4")
        build_title_card(title, title_card_path, title_card_duration)
        segment_paths.append(title_card_path)
        lead_in = title_card_duration

    for turn in manifest["turns"]:
        seg_path = os.path.join(segments_dir, f"{turn['index']:02d}.mp4")
        print(f"[{turn['index'] + 1}/{len(manifest['turns'])}] 세그먼트 렌더링: "
              f"{turn['character_name']} ({turn['duration']:.2f}초)")
        build_segment(
            image_path=turn["image_path"],
            duration=turn["duration"],
            out_path=seg_path,
        )
        segment_paths.append(seg_path)

    concat_path = os.path.join(segments_dir, "_concat.mp4")
    print("세그먼트 이어붙이는 중(무음)...")
    concat_segments(segment_paths, concat_path)

    final_path = FINAL_PATH_TEMPLATE.format(date=today)
    print(f"낭독 음성 입히는 중 (시작 지연 {lead_in:.2f}초)...")
    mux_narration(concat_path, manifest["narration_audio"], lead_in, final_path)

    print(f"완료: {final_path}")


if __name__ == "__main__":
    main()
