"""
1일 1쇼츠 자동화 파이프라인 - 4단계: 대본 생성

collect_news.py가 만든 output/facts_{date}.json (사실관계 5W1H)을 입력으로 받아,
'편의점 알바생 + 단골 손님'의 대화 형식 영어 대본을 생성한다.

캐릭터 구조:
  - 알바생 (고정 1명): 채널의 얼굴. 덤덤하게 팩폭하는 톤.
  - 손님 (고정 로테이션 2~3명): 그날 사건과 어울리는 손님을 배정.

출력은 화자별 대사 리스트(JSON)이며, 5단계(음성 생성)에서 화자별로 다른 TTS
보이스를 매핑하는 데 그대로 쓸 수 있다.

필요 환경변수:
  ANTHROPIC_API_KEY

필요 패키지:
  pip install anthropic --break-system-packages
"""

import os
import json
import random
import datetime as dt

from anthropic import Anthropic

# ---------------------------------------------------------------------------
# 캐릭터 설정 - 여기 값만 바꾸면 캐릭터 톤/로스터를 조정할 수 있다.
# ---------------------------------------------------------------------------

CLERK = {
    "name": "알바생",
    "voice_id": "clerk_voice",       # 5단계 TTS 매핑용 식별자
    "persona": (
        "24시간 편의점에서 일하는 20대 알바생. 놀라운 얘기를 들어도 크게 동요하지 "
        "않고 덤덤하게 팩트로 받아치는 성격. 가끔 시니컬한 유머를 섞는다."
    ),
}

CUSTOMER_ROSTER = [
    {
        "name": "택시기사",
        "name_en": "a taxi driver",
        "voice_id": "customer_taxi_voice",
        "persona": "매일 밤 편의점에 들르는 단골 택시기사. 서울 곳곳 소문에 빠삭하다.",
    },
    {
        "name": "취준생",
        "name_en": "a job-seeker",
        "voice_id": "customer_student_voice",
        "persona": "편의점 근처 고시원에 사는 취업준비생. 뉴스에 예민하게 반응한다.",
    },
    {
        "name": "야간근무자",
        "name_en": "a night-shift worker",
        "voice_id": "customer_worker_voice",
        "persona": "야간 근무 마치고 들르는 직장인. 피곤하지만 할 말은 하는 스타일.",
    },
]

CATCHPHRASE = "That's why you always lock up properly around here."

FACTS_PATH_TEMPLATE = "output/facts_{date}.json"
SCRIPT_PATH_TEMPLATE = "output/script_{date}.json"

TARGET_WORD_COUNT = "130-150"


# ---------------------------------------------------------------------------
# 대본 생성
# ---------------------------------------------------------------------------

def pick_customer() -> dict:
    """오늘 대화 상대로 쓸 손님을 로테이션에서 고른다.

    지금은 랜덤 선택이지만, 사건 카테고리(예: 야간 사건이면 야간근무자)에
    맞춰 규칙 기반으로 바꿔도 된다.
    """
    return random.choice(CUSTOMER_ROSTER)


def build_system_prompt(customer: dict) -> str:
    return (
        "당신은 영어권 해외 시청자를 위한 유튜브 쇼츠 대본 작가입니다. "
        "한국 편의점을 배경으로, 알바생과 손님 두 사람의 짧은 대화 형식으로 "
        "오늘의 사건 사실관계를 자연스럽게 전달하는 60초 이내 영어 대본을 씁니다.\n\n"
        f"등장인물:\n"
        f"1) {CLERK['name']} (speaker id: clerk) - {CLERK['persona']}\n"
        f"2) {customer['name']} (speaker id: customer) - {customer['persona']}\n\n"
        "작성 규칙:\n"
        "1. 입력으로 주어지는 사실관계 외의 사실을 지어내지 마세요.\n"
        "2. 기사 원문 표현을 그대로 쓰지 말고, 대화체로 완전히 새로 쓰세요.\n"
        "3. 실명이 있다면 익명 표현으로 바꾸세요 (예: '30대 남성').\n"
        "4. 대화는 계산대에서 스몰토크로 시작 -> 손님이 사건을 언급 -> "
        "알바생이 되묻거나 팩폭 리액션 -> 사실관계 자연스럽게 전달 -> "
        f"알바생이 아래 캐치프레이즈로 마무리하는 흐름을 따르세요: \"{CATCHPHRASE}\"\n"
        f"5. 전체 대사 총합은 영어 단어 기준 {TARGET_WORD_COUNT}단어 내외로 하세요.\n"
        "6. 반드시 JSON만 출력하세요. 다른 설명이나 마크다운은 포함하지 마세요.\n"
        "7. 출력 스키마: {\"turns\": [{\"speaker\": \"clerk\"|\"customer\", "
        "\"line\": str}, ...]}"
    )


def generate_dialogue(client: Anthropic, facts: dict, customer: dict) -> dict:
    system_prompt = build_system_prompt(customer)

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

    facts_path = FACTS_PATH_TEMPLATE.format(date=today)
    if not os.path.exists(facts_path):
        raise SystemExit(f"{facts_path} 가 없습니다. collect_news.py를 먼저 실행하세요.")

    with open(facts_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    facts = payload["selected_facts"]

    print("[1/2] 오늘의 손님 캐릭터 선택 중...")
    customer = pick_customer()
    print(f"  선택된 손님: {customer['name']}")

    print("[2/2] 대화 대본 생성 중...")
    dialogue = generate_dialogue(client, facts, customer)

    # 화자 id를 실제 캐릭터 정보(이름, 보이스 id)로 확장해서 5단계에서 바로 쓸 수 있게 한다
    speaker_map = {
        "clerk": {"name": CLERK["name"], "voice_id": CLERK["voice_id"]},
        "customer": {"name": customer["name"], "voice_id": customer["voice_id"]},
    }
    for turn in dialogue["turns"]:
        turn["character_name"] = speaker_map[turn["speaker"]]["name"]
        turn["voice_id"] = speaker_map[turn["speaker"]]["voice_id"]

    output = {
        "date": today,
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
