"""
1일 1쇼츠 자동화 파이프라인 - 0단계(선택): 사연 재고 자동 보충

"언니삼총사 사연 시트"에서 EP.030의 Status가 "완료"로 바뀌었는지 확인하고,
그렇다면(그리고 아직 EP.051이 없다면) Claude에게 새 사연 50개(EP.051~100)를
표준 포맷 그대로 만들게 해서 시트에 자동으로 추가한다.

[동작 방식]
매 실행(하루 3회)마다 이 스크립트가 먼저 돈다. 대부분의 실행에서는 시트를
한 번 읽어보는 것 말고는 아무 일도 안 하고 조용히 끝난다(비용 거의 없음).
"EP.030 완료 + EP.051 없음" 조건이 맞는 딱 그 순간에만 Claude API를
한 번 호출해서 50개를 한꺼번에 만들고 끝낸다. 그 다음 실행부터는
EP.051이 이미 있으니 다시 트리거되지 않는다(중복 생성 방지).

트리거 조건을 바꾸고 싶으면(예: 30번째가 아니라 40번째에 보충) 아래
TRIGGER_EP, BATCH_START_EP 값만 수정하면 된다.

필요 환경변수:
  ANTHROPIC_API_KEY
  GOOGLE_SHEETS_CREDENTIALS

필요 패키지:
  pip install anthropic gspread google-auth --break-system-packages
"""

import os
import re
import json
import sys

import gspread
from google.oauth2.service_account import Credentials
from anthropic import Anthropic

SHEET_ID = "1AOvI5ExbZ4j_BJHZvZnWnXExCDOebjxgsiRr7SWfuDE"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

TRIGGER_EP = "030"       # 이 회차가 "완료"되면 보충을 시작한다
BATCH_SIZE = 50          # 한 번에 몇 개를 새로 만들지
COLUMNS = ["Status", "EP", "제목", "사연", "현실언니", "공감언니", "폭주언니", "질문"]

SYSTEM_PROMPT = """당신은 한국어 유튜브 쇼츠/틱톡용 "사연" 콘텐츠 작가입니다.
스레드에서 만난 친한 언니들이 사연에 반응하는 콘텐츠의 대본을 씁니다.

각 사연은 반드시 이 구조를 따릅니다:
- title: 사연 제목 (10자 내외, 명사형)
- story_lines: 상황을 설명하는 문장 6~8개 (배열), 1인칭 시점, 각 문장은
  한 줄짜리 캡션에 어울리게 짧고 명확하게. 도입(상황)→전개→반전/문제
  제시 순서로 구성
- real: 현실언니 대사 1줄 - 실용적이고 구체적인 조언
- empathy: 공감언니 대사 1줄 - 감정에 공감하는 위로
- rage: 폭주언니 대사 1줄 - 과격하고 재치있는 팩폭/드립
- question: 마무리 질문 1줄 - "여러분이라면 ~하시겠어요?" 형태

소재는 배우자/애인/친구/가족/직장 인간관계에서 벌어지는 배신, 비밀 발견,
경계 침범, 부당한 요구 등을 다룹니다. 자살, 미성년자 관련 성적 내용,
실존 인물 지칭은 절대 포함하지 않습니다.

반드시 JSON 배열만 출력하세요. 다른 설명이나 마크다운은 포함하지 마세요.
스키마: [{"title": str, "story_lines": [str, ...], "real": str,
"empathy": str, "rage": str, "question": str}, ...]
"""


def load_client() -> gspread.Client:
    creds_json = os.environ.get("GOOGLE_SHEETS_CREDENTIALS")
    if not creds_json:
        raise SystemExit("GOOGLE_SHEETS_CREDENTIALS 환경변수가 필요합니다.")
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


ORIGINAL_BATCH_SIZE = 50  # 처음 채워둔 사연 개수. 이 숫자를 넘는 EP가 이미 있으면 "이미 보충됨"으로 판단


def should_generate(records: list[dict]) -> tuple[bool, list[str], int]:
    """(생성해야 하는지, 기존 제목 목록, 다음 EP 시작 번호) 반환."""
    trigger_row = next((r for r in records if str(r.get("EP", "")).strip() == TRIGGER_EP), None)
    if not trigger_row or str(trigger_row.get("Status", "")).strip() != "완료":
        return False, [], 0

    existing_eps = [str(r.get("EP", "")).strip() for r in records if str(r.get("EP", "")).strip()]
    ep_numbers = [int(e) for e in existing_eps if e.isdigit()]
    max_ep_num = max(ep_numbers, default=0)

    # 이미 원래 50개(ORIGINAL_BATCH_SIZE)를 넘는 EP가 존재하면 = 이전에 이미 보충된 것 -> 재생성 방지
    if max_ep_num > ORIGINAL_BATCH_SIZE:
        return False, [], 0

    next_ep_num = max_ep_num + 1
    titles = [str(r.get("제목", "")).strip() for r in records if str(r.get("제목", "")).strip()]
    return True, titles, next_ep_num


def generate_stories(client: Anthropic, existing_titles: list[str], count: int) -> list[dict]:
    used_titles_text = "\n".join(f"- {t}" for t in existing_titles) or "(없음)"
    user_prompt = (
        f"아래는 이미 사용된 제목 목록입니다. 겹치지 않는 완전히 새로운 소재로 "
        f"{count}개를 만들어주세요.\n\n{used_titles_text}\n\n"
        f"JSON 배열 {count}개, 스키마 그대로 출력하세요."
    )

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = "".join(block.text for block in message.content if block.type == "text")
    raw = raw.strip().removeprefix("```json").removesuffix("```").strip()
    data = json.loads(raw)

    if not isinstance(data, list):
        raise ValueError(f"예상치 못한 응답 형식(배열이 아님): {type(data)}")

    return data


def validate_story(story: dict, idx: int) -> list[str]:
    errors = []
    required = ["title", "story_lines", "real", "empathy", "rage", "question"]
    for key in required:
        if key not in story or not story[key]:
            errors.append(f"{idx}번째 항목: '{key}' 필드 누락/비어있음")
    if "story_lines" in story:
        n = len(story["story_lines"])
        if not (5 <= n <= 9):
            errors.append(f"{idx}번째 항목: story_lines {n}줄 (권장 6~8줄)")
    return errors


def stories_to_rows(stories: list[dict], start_ep_num: int) -> list[list[str]]:
    rows = []
    for i, s in enumerate(stories):
        ep_label = f"{start_ep_num + i:03d}"
        story_text = "\n".join(s["story_lines"])
        rows.append(["대기", ep_label, s["title"], story_text,
                     s["real"], s["empathy"], s["rage"], s["question"]])
    return rows


def main():
    gc = load_client()
    sh = gc.open_by_key(SHEET_ID)
    ws = sh.sheet1

    records = ws.get_all_records()
    trigger, existing_titles, next_ep_num = should_generate(records)

    if not trigger:
        print(f"보충 조건 미충족(EP.{TRIGGER_EP} 미완료 또는 이미 보충됨) - 건너뜀.")
        return

    print(f"EP.{TRIGGER_EP} 완료 확인, EP.{next_ep_num:03d}부터 {BATCH_SIZE}개 생성 시작...")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY 환경변수가 필요합니다.")
    client = Anthropic(api_key=api_key)

    stories = generate_stories(client, existing_titles, BATCH_SIZE)

    all_errors = []
    for i, s in enumerate(stories, start=1):
        all_errors.extend(validate_story(s, i))
    if all_errors:
        raise SystemExit(f"생성된 사연 검증 실패:\n" + "\n".join(all_errors))

    if len(stories) != BATCH_SIZE:
        print(f"[경고] 요청 {BATCH_SIZE}개 중 {len(stories)}개만 생성됨. 있는 만큼만 추가합니다.")

    rows = stories_to_rows(stories, next_ep_num)
    ws.append_rows(rows, value_input_option="USER_ENTERED")

    print(f"완료: EP.{next_ep_num:03d}~EP.{next_ep_num + len(rows) - 1:03d} "
          f"({len(rows)}개) 시트에 추가함")


if __name__ == "__main__":
    main()
