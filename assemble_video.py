"""
1일 1쇼츠 자동화 파이프라인 - 6단계: 영상 조립

generate_media.py가 만든 output/manifest_{date}.json (turn별 오디오/이미지 경로)을
받아서, 각 turn을 이미지+음성+자막이 있는 세그먼트로 만들고 이어 붙인 뒤
배경음악(BGM)을 낮은 볼륨으로 섞어 최종 세로형(9:16) mp4를 만든다.

필요 프로그램: ffmpeg, ffprobe (PATH에 있어야 함)

사전 준비물:
  assets/bgm.mp3          - 배경음악 (없으면 BGM 없이 렌더링)
  assets/fonts/subtitle.ttf - 자막 폰트 (없으면 시스템 기본 폰트 사용)

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

BGM_PATH = "assets/bgm.mp3"
FONT_PATH = "assets/fonts/subtitle.ttf"

VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
BGM_VOLUME = 0.15  # 대사 대비 배경음악 볼륨 비율

# 자막 세로 위치: 화면 높이 대비 비율. 0.5 = 정중앙, 값이 클수록 아래로 내려감.
# 지금은 "화면 중간에서 약간 하단"에 맞춰 0.58로 설정. 필요하면 이 숫자만 바꾸면 됨.
SUBTITLE_Y_RATIO = 0.58

# 영상 시작 전에 보여줄 타이틀 카드 길이(초)
TITLE_CARD_DURATION = 2.5
TITLE_CARD_BG_COLOR = "black"


# ---------------------------------------------------------------------------
# 유틸
# ---------------------------------------------------------------------------

def run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"명령 실행 실패: {' '.join(cmd)}\n--- stderr ---\n{result.stderr}"
        )


def get_audio_duration(audio_path: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", audio_path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe 실패: {result.stderr}")
    return float(result.stdout.strip())


# ---------------------------------------------------------------------------
# 세그먼트(turn 하나) 렌더링
# ---------------------------------------------------------------------------

def build_segment(image_path: str, audio_path: str, line: str,
                   character_name: str, out_path: str) -> None:
    duration = get_audio_duration(audio_path)

    # drawtext에 특수문자(콜론, 따옴표 등) 이스케이프 문제를 피하려고
    # 텍스트를 파일로 빼서 textfile= 옵션으로 넘긴다.
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(f"{character_name}: {line}")
        subtitle_file = tf.name

    fontfile_opt = f":fontfile={FONT_PATH}" if os.path.exists(FONT_PATH) else ""

    drawtext = (
        f"drawtext=textfile='{subtitle_file}'{fontfile_opt}:"
        "fontsize=44:fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=18:"
        f"x=(w-text_w)/2:y=h*{SUBTITLE_Y_RATIO}-text_h/2:line_spacing=8"
    )

    vf = (
        f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},{drawtext}"
    )

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    run([
        "ffmpeg", "-y",
        "-loop", "1", "-i", image_path,
        "-i", audio_path,
        "-vf", vf,
        "-t", str(duration),
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-shortest",
        out_path,
    ])

    os.unlink(subtitle_file)


# ---------------------------------------------------------------------------
# 인트로 타이틀 카드
# ---------------------------------------------------------------------------

def build_title_card(title: str, out_path: str) -> None:
    """영상 시작 전에 짧게 보여줄 타이틀 카드(검은 배경 + 제목 텍스트)를 만든다."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(title)
        title_file = tf.name

    fontfile_opt = f":fontfile={FONT_PATH}" if os.path.exists(FONT_PATH) else ""

    drawtext = (
        f"drawtext=textfile='{title_file}'{fontfile_opt}:"
        "fontsize=56:fontcolor=white:box=0:"
        "x=(w-text_w)/2:y=(h-text_h)/2:line_spacing=10"
    )

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    run([
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"color=c={TITLE_CARD_BG_COLOR}:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:d={TITLE_CARD_DURATION}",
        "-f", "lavfi",
        "-i", f"anullsrc=channel_layout=stereo:sample_rate=44100",
        "-vf", drawtext,
        "-t", str(TITLE_CARD_DURATION),
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-shortest",
        out_path,
    ])

    os.unlink(title_file)


# ---------------------------------------------------------------------------
# 세그먼트 이어붙이기 + BGM 믹싱
# ---------------------------------------------------------------------------

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


def mix_bgm(video_path: str, out_path: str) -> None:
    if not os.path.exists(BGM_PATH):
        print("  BGM 파일이 없어 대사 오디오만으로 최종본을 만듭니다.")
        run(["ffmpeg", "-y", "-i", video_path, "-c", "copy", out_path])
        return

    total_duration = get_audio_duration(video_path)

    filter_complex = (
        f"[1:a]volume={BGM_VOLUME},atrim=0:{total_duration}[bgm];"
        "[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=2[aout]"
    )

    run([
        "ffmpeg", "-y",
        "-i", video_path,
        "-stream_loop", "-1", "-i", BGM_PATH,
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

    # 인트로에 쓸 제목은 generate_script.py가 만들어둔 script.json의 title을 그대로 쓴다
    # (유튜브 업로드 제목과 통일하기 위해). 없으면 카드 없이 진행한다.
    script_path = SCRIPT_PATH_TEMPLATE.format(date=today)
    title = None
    if os.path.exists(script_path):
        with open(script_path, "r", encoding="utf-8") as f:
            title = json.load(f).get("title")

    segments_dir = SEGMENTS_DIR_TEMPLATE.format(date=today)
    segment_paths = []

    if title:
        print("인트로 타이틀 카드 렌더링 중...")
        title_card_path = os.path.join(segments_dir, "00_title.mp4")
        build_title_card(title, title_card_path)
        segment_paths.append(title_card_path)

    for turn in manifest["turns"]:
        seg_path = os.path.join(segments_dir, f"{turn['index']:02d}.mp4")
        print(f"[{turn['index']+1}/{len(manifest['turns'])}] 세그먼트 렌더링: "
              f"{turn['character_name']}")
        build_segment(
            image_path=turn["image_path"],
            audio_path=turn["audio_path"],
            line=turn["line"],
            character_name=turn["character_name"],
            out_path=seg_path,
        )
        segment_paths.append(seg_path)

    concat_path = os.path.join(segments_dir, "_concat.mp4")
    print("세그먼트 이어붙이는 중...")
    concat_segments(segment_paths, concat_path)

    final_path = FINAL_PATH_TEMPLATE.format(date=today)
    print("BGM 믹싱 및 최종 렌더링 중...")
    mix_bgm(concat_path, final_path)

    print(f"완료: {final_path}")


if __name__ == "__main__":
    main()
