"""
1일 1쇼츠 자동화 파이프라인 - 6단계: 영상 조립

generate_media.py가 만든 output/manifest_{date}.json (turn별 오디오/이미지 경로)을
받아서, 각 turn을 이미지+음성이 있는 세그먼트로 만들고 이어 붙인 뒤
배경음악(BGM)을 낮은 볼륨으로 섞어 최종 세로형(9:16) mp4를 만든다.

[수정] 이미지 자체(generate_media.py가 PIL로 그린 카톡 화면)에 이미 대사
텍스트가 들어있으므로, 여기서 별도 자막(drawtext)을 중복으로 그리지 않는다.
예전 버전은 화면 중앙에 "화자명: 대사"를 drawtext로 한 번 더 그렸는데,
그 폰트가 한글을 지원하지 않아 깨져 보였고 이미지 위에 겹쳐 보이는
문제가 있었다. 인트로 타이틀 카드는 여전히 drawtext를 쓰므로
assets/fonts/subtitle.ttf 에 실제 한글 폰트 파일이 있어야 한다.

필요 프로그램: ffmpeg, ffprobe (PATH에 있어야 함)

사전 준비물:
  assets/bgm.mp3            - 배경음악 (없으면 BGM 없이 렌더링)
  assets/fonts/subtitle.ttf - 한글 지원 폰트 (인트로 타이틀 카드용, 필수)

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
BGM_VOLUME = 0.15  # 대사 대비 배경음악 볼륨 비율

TITLE_CARD_BG_COLOR = "black"


def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        raise SystemExit(f"{CONFIG_PATH} 가 없습니다. 리포지토리 루트에 config.json을 두세요.")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


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
# 세그먼트(turn 하나) 렌더링 - 이미지에 이미 텍스트가 있으므로 자막 없이 합성만
# ---------------------------------------------------------------------------

def build_segment(image_path: str, audio_path: str, out_path: str) -> None:
    duration = get_audio_duration(audio_path)

    vf = (
        f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT}"
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


# ---------------------------------------------------------------------------
# 인트로 타이틀 카드
# ---------------------------------------------------------------------------

def build_title_card(title: str, out_path: str, duration: float) -> None:
    """영상 시작 전에 짧게 보여줄 타이틀 카드(검은 배경 + 제목 텍스트)를 만든다."""
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
        "-f", "lavfi",
        "-i", f"anullsrc=channel_layout=stereo:sample_rate=44100",
        "-vf", drawtext,
        "-t", str(duration),
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

    # 인트로에 쓸 제목은 fetch_script.py가 만들어둔 script.json의 title을 그대로 쓴다
    # (유튜브 업로드 제목과 통일하기 위해). 없으면 카드 없이 진행한다.
    script_path = SCRIPT_PATH_TEMPLATE.format(date=today)
    title = None
    if os.path.exists(script_path):
        with open(script_path, "r", encoding="utf-8") as f:
            title = json.load(f).get("title")

    config = load_config()
    title_card_duration = config.get("title_card_duration", 2.5)

    segments_dir = SEGMENTS_DIR_TEMPLATE.format(date=today)
    segment_paths = []

    if title:
        print("인트로 타이틀 카드 렌더링 중...")
        title_card_path = os.path.join(segments_dir, "00_title.mp4")
        build_title_card(title, title_card_path, title_card_duration)
        segment_paths.append(title_card_path)

    for turn in manifest["turns"]:
        seg_path = os.path.join(segments_dir, f"{turn['index']:02d}.mp4")
        print(f"[{turn['index'] + 1}/{len(manifest['turns'])}] 세그먼트 렌더링: "
              f"{turn['character_name']}")
        build_segment(
            image_path=turn["image_path"],
            audio_path=turn["audio_path"],
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
