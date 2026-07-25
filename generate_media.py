"""
1일 1쇼츠 자동화 파이프라인 - 4단계: 대본 생성

collect_news.py가 만든 output/facts_{date}.json (사실관계 5W1H)을 입력으로 받아,
'편의점 알바생 + 택시기사' 두 사람의 대화 형식 영어 대본을 생성한다.

캐릭터/톤/캐치프레이즈 등은 이 파일이 아니라 리포지토리 루트의 config.json에서
관리한다. 대시보드에서 config.json을 직접 수정할 수 있어서, 캐릭터를 바꾸고
싶으면 이 코드가 아니라 config.json만 고치면 된다.

출력은 화자별 대사 리스트(JSON)이며, 5단계(음성 생성)에서 화자별로 다른 TTS
보이스를 매핑하는 데 그대로 쓸 수 있다.

필요 환경변수:
  ANTHROPIC_API_KEY

필요 패키지:
  pip install anthropic --break-system-packages
"""

import os
import json
import datetime as dt

from anthropic import Anthropic

CONFIG_PATH = "config.json"
FACTS_PATH_TEMPLATE = "output/facts_{date}.json"
SCRIPT_PATH_TEMPLATE = "output/script_{date}.json"


# ---------------------------------------------------------------------------
# 설정 로드
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        raise SystemExit(f"{CONFIG_PATH} 가 없습니다. 리포지토리 루트에 config.json을 두세요.")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 대본 생성
# ---------------------------------------------------------------------------

def build_system_prompt(config: dict) -> str:
    clerk = config["clerk"]
    customer = config["customer"]
    catchphrase = config["catchphrase"]
    target_word_count = config.get("target_word_count", "130-150")

    return (
        "당신은 영어권 해외 시청자를 위한 유튜브 쇼츠 대본 작가입니다. "
        "한국 편의점을 배경으로, 알바생과 손님 두 사람의 짧은 대화 형식으로 "
        "오늘의 사건 사실관계를 자연스럽게 전달하는 60초 이내 영어 대본을 씁니다.\n\n"
        f"등장인물:\n"
        f"1) {clerk['name']} (speaker id: clerk) - {clerk['persona']}\n"
        f"2) {customer['name']} (speaker id: customer) - {customer['persona']}\n\n"
        "작성 규칙:\n"
        "1. 입력으로 주어지는 사실관계 외의 사실을 지어내지 마세요.\n"
        "2. 기사 원문 표현을 그대로 쓰지 말고, 대화체로 완전히 새로 쓰세요.\n"
        "3. 실명이 있다면 익명 표현으로 바꾸세요 (예: '30대 남성').\n"
        "4. 대화는 계산대에서 스몰토크로 시작 -> 손님이 사건을 언급 -> "
        "알바생이 되묻거나 팩폭 리액션 -> 사실관계 자연스럽게 전달 -> "
        f"알바생이 아래 캐치프레이즈로 마무리하는 흐름을 따르세요: \"{catchphrase}\"\n"
        f"5. 전체 대사 총합은 영어 단어 기준 {target_word_count}단어 내외로 하세요.\n"
        "6. 반드시 JSON만 출력하세요. 다른 설명이나 마크다운은 포함하지 마세요.\n"
        "7. 출력 스키마: {\"turns\": [{\"speaker\": \"clerk\"|\"customer\", "
        "\"line\": str}, ...]}"
    )


def generate_dialogue(client: Anthropic, facts: dict, config: dict) -> dict:
    system_prompt = build_system_prompt(config)
    user_content = json.dumps(facts, ensure_ascii=False)

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )

    raw = "".join(block.text for block in message.content if block.type == "text")
    raw = raw.strip().removeprefix("```json").removesuffix("```").strip()
    data = json.loads(raw)

    if "turns" not in data or not isinstance(data["turns"], list):
        raise ValueError(f"예상치 못한 응답 형식: {data}")

    return data


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------

def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY 환경변수가 필요합니다.")

    client = Anthropic(api_key=api_key)
    today = dt.date.today().isoformat()
    config = load_config()

    facts_path = FACTS_PATH_TEMPLATE.format(date=today)
    if not os.path.exists(facts_path):
        raise SystemExit(f"{facts_path} 가 없습니다. collect_news.py를 먼저 실행하세요.")

    with open(facts_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    facts = payload["selected_facts"]

    print("[1/1] 대화 대본 생성 중...")
    dialogue = generate_dialogue(client, facts, config)

    clerk = config["clerk"]
    customer = config["customer"]
    speaker_map = {
        "clerk": {"name": clerk["name"], "voice_id": clerk["voice_id"]},
        "customer": {"name": customer["name"], "voice_id": customer["voice_id"]},
    }
    for turn in dialogue["turns"]:
        turn["character_name"] = speaker_map[turn["speaker"]]["name"]
        turn["voice_id"] = speaker_map[turn["speaker"]]["voice_id"]

    # 손님의 첫 대사(보통 사건을 언급하는 훅 문장)를 제목으로 쓴다.
    # 인트로 타이틀 카드, 썸네일, 유튜브 업로드 제목이 전부 이 값을 그대로 같이 쓴다.
    hook_line = next((t["line"] for t in dialogue["turns"] if t["speaker"] == "customer"),
                      dialogue["turns"][0]["line"])
    title = hook_line.rstrip("?.! ")
    if len(title) > 90:
        title = title[:87] + "..."
    title = f"{title}?"

    output = {
        "date": today,
        "title": title,
        "customer_character": customer["name"],
        "customer_character_en": customer["name_en"],
        "source_link": facts.get("source_link"),
        "turns": dialogue["turns"],
    }

    os.makedirs("output", exist_ok=True)
    out_path = SCRIPT_PATH_TEMPLATE.format(date=today)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"완료: {out_path}")
    total_words = sum(len(t["line"].split()) for t in dialogue["turns"])
    print(f"총 대사 단어 수: {total_words}")


if __name__ == "__main__":
    main()
