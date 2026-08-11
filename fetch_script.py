"""
1일 1쇼츠 자동화 파이프라인 - 3단계 교체: 구글 시트에서 사연 가져오기

Claude를 부르지 않고, 미리 만들어둔 구글 스프레드시트
("언니삼총사 사연 시트")에서 Status="대기"인 사연 하나를 가져와
output/script_{date}.json 을 만든다.

[방식] 유니(ElevenLabs) 한 명이 팟캐스트 진행자처럼 사연 전체를 이어서
낭독하는 방식. 화면(카톡 채팅 UI)은 기존처럼 turn(말풍선) 단위로 나뉘어
있지만, 음성은 turn마다 따로 만들지 않고 전체를 한 번에 이어붙인
"narration" 텍스트 하나로 통째로 TTS를 한 번만 호출한다.

각 turn에는 화면에 보일 원문(line)과, 그 turn이 낭독 전체에서 차지하는
분량을 가늠하기 위한 weight_text(내레이션에는 포함되지만 화면에는 안
보이는 연결어구까지 포함)를 같이 저장한다. weight_text 길이 비율로
전체 음성 길이를 turn별로 나눠서 화면 타이밍을 맞춘다(4단계에서 사용).

필요 환경변수:
  GOOGLE_SHEETS_CREDENTIALS  - 서비스 계정 JSON 전체 내용(문자열)

필요 패키지:
  pip install gspread google-auth --break-system-packages
"""

import os
import json
import sys
import datetime as dt

import gspread
from google.oauth2.service_account import Credentials

SHEET_ID = "1AOvI5ExbZ4j_BJHZvZnWnXExCDOebjxgsiRr7SWfuDE"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SCRIPT_PATH_TEMPLATE = "output/script_{date}.json"

# ElevenLabs Yooni - 팟캐스트 진행자처럼 사연 전체를 혼자 낭독
VOICE_ID = "n2fbxG88jqAoaVPUy3IG"

CHARACTER_NAMES = {
    "reporter": "제보자",
    "real": "현실언니",
    "empathy": "공감언니",
    "rage": "폭주언니",
    "question": "",
}

INTRO = "안녕하세요, 오늘도 사연 하나 들고 왔습니다."
BRIDGE = "이 얘기를 들은 언니들의 반응은 이랬습니다."

REQUIRED_COLUMNS = ["Status", "EP", "제목", "사연", "현실언니", "공감언니", "폭주언니", "질문"]


def load_client() -> gspread.Client:
    creds_json = os.environ.get("GOOGLE_SHEETS_CREDENTIALS")
    if not creds_json:
        raise SystemExit("GOOGLE_SHEETS_CREDENTIALS 환경변수가 필요합니다.")
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def find_pending_row(ws: gspread.Worksheet):
    """Status가 '대기'인 첫 번째 행을 찾는다. (행 번호, 행 데이터) 반환, 없으면 (None, None)."""
    records = ws.get_all_records()
    for i, row in enumerate(records, start=2):  # 1행은 헤더라서 데이터는 2행부터
        if str(row.get("Status", "")).strip() == "대기":
            return i, row
    return None, None


def validate_row(row: dict, ep_label: str) -> None:
    missing = [c for c in REQUIRED_COLUMNS if c not in row or str(row[c]).strip() == ""]
    if missing:
        raise SystemExit(f"EP.{ep_label} 행에 빈 컬럼이 있습니다: {missing}")


def build_turns(row: dict) -> list[dict]:
    """화면(카톡 UI)에 쓸 turn 목록. line=화면에 보일 원문, weight_text=낭독 전체에서
    이 turn이 차지하는 분량(타이밍 계산용, 연결어구 포함)."""
    story_lines = [s.strip() for s in str(row["사연"]).split("\n") if s.strip()]
    if not story_lines:
        raise SystemExit("사연 컬럼이 비어 있습니다.")

    real = str(row["현실언니"]).strip()
    empathy = str(row["공감언니"]).strip()
    rage = str(row["폭주언니"]).strip()
    question = str(row["질문"]).strip()

    turns = []
    for i, line in enumerate(story_lines):
        weight_text = f"{INTRO} {line}" if i == 0 else line
        turns.append({"speaker": "reporter", "line": line, "weight_text": weight_text})

    turns.append({
        "speaker": "real", "line": real,
        "weight_text": f"{BRIDGE} 현실언니는 이렇게 말합니다. {real}",
    })
    turns.append({
        "speaker": "empathy", "line": empathy,
        "weight_text": f"공감언니는 이렇게 말합니다. {empathy}",
    })
    turns.append({
        "speaker": "rage", "line": rage,
        "weight_text": f"그리고 폭주언니는 이렇게 말합니다. {rage}",
    })
    turns.append({"speaker": "question", "line": question, "weight_text": question})

    for i, t in enumerate(turns):
        t["index"] = i
        t["character_name"] = CHARACTER_NAMES[t["speaker"]]

    return turns


def build_narration(turns: list[dict]) -> str:
    """turn별 weight_text를 순서대로 이어붙이면 낭독 전체 텍스트가 된다."""
    return " ".join(t["weight_text"] for t in turns)


def main():
    gc = load_client()
    sh = gc.open_by_key(SHEET_ID)
    ws = sh.sheet1

    row_index, row = find_pending_row(ws)
    if row is None:
        print("처리할 대기 상태 에피소드가 없습니다. 시트를 채워주세요.")
        sys.exit(1)

    ep = str(row["EP"]).strip()
    validate_row(row, ep)

    title_raw = str(row["제목"]).strip()
    turns = build_turns(row)
    narration = build_narration(turns)

    today = dt.date.today().isoformat()
    output = {
        "date": today,
        "ep": ep,
        "title": f"EP.{ep} {title_raw}",
        "voice_id": VOICE_ID,
        "narration": narration,
        "turns": turns,
    }

    os.makedirs("output", exist_ok=True)
    out_path = SCRIPT_PATH_TEMPLATE.format(date=today)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # 처리한 행은 다음 실행 때 건너뛰도록 완료 처리 + Status 셀을 초록색으로 표시
    ws.update_cell(row_index, 1, "완료")
    ws.format(f"A{row_index}", {
        "backgroundColor": {"red": 0.71, "green": 0.84, "blue": 0.66}
    })

    print(f"완료: {out_path}")
    print(f"EP.{ep} {title_raw} - turn {len(turns)}개, 낭독 {len(narration)}자")


if __name__ == "__main__":
    main()
