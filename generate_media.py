"""
1일 1쇼츠 자동화 파이프라인 - 5단계: 음성(TTS) + 캐릭터 이미지 생성

generate_script.py가 만든 output/script_{date}.json (대사 turn 리스트)을 입력으로 받아,
1) 각 대사를 화자별 목소리로 TTS 생성 (ElevenLabs)
2) 각 대사에 맞는 캐릭터 이미지 생성 (OpenAI images edit - 고정 레퍼런스 이미지 기반)
을 수행하고, 6단계(영상 조립)가 바로 쓸 수 있는 manifest.json을 만든다.

사전 준비물 (중요):
  assets/character_refs/ 아래에 캐릭터별 레퍼런스 이미지를 미리 넣어둬야 한다.
    - assets/character_refs/clerk_voice.png       (알바생)
    - assets/character_refs/customer_taxi_voice.png
    - assets/character_refs/customer_student_voice.png
    - assets/character_refs/customer_worker_voice.png
  레퍼런스 이미지는 캐릭터 컨셉이 확정되면 한 번만 만들어서 고정해두는 게 핵심이다.
  (이 스크립트는 매번 새로 그리지 않고, 이 레퍼런스를 기반으로 표정/포즈만 살짝 바꾼다.)

필요 환경변수:
  ELEVENLABS_API_KEY
  OPENAI_API_KEY

필요 패키지:
  pip install requests --break-system-packages

주의: 이 컨테이너는 네트워크가 막혀 있어서 실제 호출은 사용자의 실행 환경(로컬/GitHub
Actions)에서 진행해야 한다.
"""

import os
import json
import base64
import datetime as dt

import requests

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------

SCRIPT_PATH_TEMPLATE = "output/script_{date}.json"
AUDIO_DIR_TEMPLATE = "output/audio_{date}"
IMAGE_DIR_TEMPLATE = "output/images_{date}"
MANIFEST_PATH_TEMPLATE = "output/manifest_{date}.json"

CHARACTER_REF_DIR = "assets/character_refs"

# 내부 voice_id -> 실제 ElevenLabs voice_id. 콘솔에서 캐릭터 목소리를 만든 뒤 채워 넣으세요.
ELEVENLABS_VOICE_MAP = {
    "clerk_voice": "5DWGv3VDkihNUcbvaonB",
    "customer_taxi_voice": "CxErO97xpQgQXYmapDKX",
    "customer_student_voice": "70DeQK5Ztp7WmEGGysLT",
    "customer_worker_voice": "mK6Q1HRYYwUJwQGwMPYw",
}

ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
OPENAI_IMAGE_EDIT_URL = "https://api.openai.com/v1/images/edits"

IMAGE_SIZE = "1024x1536"  # 쇼츠 세로 비율에 가까운 사이즈


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------

def generate_tts(text: str, internal_voice_id: str, out_path: str, api_key: str) -> None:
    real_voice_id = ELEVENLABS_VOICE_MAP.get(internal_voice_id)
    if not real_voice_id or real_voice_id.startswith("REPLACE_WITH"):
        raise ValueError(
            f"'{internal_voice_id}'에 해당하는 ElevenLabs voice_id가 설정되지 않았습니다. "
            "ELEVENLABS_VOICE_MAP을 채워주세요."
        )

    resp = requests.post(
        ELEVENLABS_TTS_URL.format(voice_id=real_voice_id),
        headers={
            "xi-api-key": api_key,
            "content-type": "application/json",
        },
        json={
            "text": text,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        },
        timeout=30,
    )
    resp.raise_for_status()

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(resp.content)


# ---------------------------------------------------------------------------
# 이미지 생성 (레퍼런스 이미지 기반 - 캐릭터 일관성 유지)
# ---------------------------------------------------------------------------

def build_scene_prompt(character_name: str, line: str) -> str:
    """대사 내용에 맞춰 표정/포즈 디렉션만 살짝 바꾸는 프롬프트.
    캐릭터 자체의 외형은 레퍼런스 이미지가 고정해주므로 여기서는 장면 지시만 준다."""
    return (
        f"Same character as the reference image, in a Korean convenience store. "
        f"Keep the exact same face, hairstyle, and outfit as the reference. "
        f"Adjust only the expression and pose to match this line naturally: \"{line}\". "
        f"Vertical 9:16 composition, flat clean lighting, no text overlay."
    )


def generate_character_image(internal_voice_id: str, character_name: str,
                              line: str, out_path: str, api_key: str) -> None:
    ref_path = os.path.join(CHARACTER_REF_DIR, f"{internal_voice_id}.png")
    if not os.path.exists(ref_path):
        raise FileNotFoundError(
            f"레퍼런스 이미지가 없습니다: {ref_path}. "
            "캐릭터 시트를 먼저 assets/character_refs/ 에 준비해주세요."
        )

    prompt = build_scene_prompt(character_name, line)

    last_error = None
    for attempt in range(1, 3):  # 최대 2회 시도 (첫 시도 + 재시도 1회)
        try:
            with open(ref_path, "rb") as ref_file:
                resp = requests.post(
                    OPENAI_IMAGE_EDIT_URL,
                    headers={"Authorization": f"Bearer {api_key}"},
                    files={"image": ref_file},
                    data={"model": "gpt-image-1", "prompt": prompt, "size": IMAGE_SIZE, "n": 1},
                    timeout=180,
                )
            if not resp.ok:
                raise RuntimeError(
                    f"OpenAI 이미지 편집 요청 실패: HTTP {resp.status_code} - {resp.text[:500]}"
                )
            data = resp.json()
            b64_image = data["data"][0]["b64_json"]
            break
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_error = e
            print(f"  [경고] 이미지 요청 시도 {attempt}회차 실패({type(e).__name__}), 재시도합니다...")
    else:
        raise RuntimeError(f"이미지 생성이 재시도 후에도 실패했습니다: {last_error}")


    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(base64.b64decode(b64_image))


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------

def main():
    eleven_key = os.environ.get("ELEVENLABS_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")
    if not eleven_key or not openai_key:
        raise SystemExit("ELEVENLABS_API_KEY와 OPENAI_API_KEY 환경변수가 모두 필요합니다.")

    today = dt.date.today().isoformat()
    script_path = SCRIPT_PATH_TEMPLATE.format(date=today)
    if not os.path.exists(script_path):
        raise SystemExit(f"{script_path} 가 없습니다. generate_script.py를 먼저 실행하세요.")

    with open(script_path, "r", encoding="utf-8") as f:
        script = json.load(f)

    audio_dir = AUDIO_DIR_TEMPLATE.format(date=today)
    image_dir = IMAGE_DIR_TEMPLATE.format(date=today)

    manifest_turns = []

    for i, turn in enumerate(script["turns"]):
        voice_id = turn["voice_id"]
        character_name = turn["character_name"]
        line = turn["line"]

        audio_path = os.path.join(audio_dir, f"{i:02d}_{voice_id}.mp3")
        image_path = os.path.join(image_dir, f"{i:02d}_{voice_id}.png")

        print(f"[{i+1}/{len(script['turns'])}] {character_name}: {line[:30]}...")

        print("  음성 생성 중...")
        generate_tts(line, voice_id, audio_path, eleven_key)

        print("  이미지 생성 중...")
        generate_character_image(voice_id, character_name, line, image_path, openai_key)

        manifest_turns.append({
            "index": i,
            "speaker": turn["speaker"],
            "character_name": character_name,
            "line": line,
            "audio_path": audio_path,
            "image_path": image_path,
        })

    manifest = {
        "date": today,
        "customer_character": script["customer_character"],
        "turns": manifest_turns,
    }

    manifest_path = MANIFEST_PATH_TEMPLATE.format(date=today)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"완료: {manifest_path}")


if __name__ == "__main__":
    main()
