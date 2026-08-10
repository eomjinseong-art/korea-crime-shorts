"""
1일 1쇼츠 자동화 파이프라인 - 3단계 교체: 구글 시트에서 사연 가져오기

기존 generate_script.py(편의점 알바생+손님 대화 생성, Anthropic API 호출)를
대체한다. Claude를 부르지 않고, 미리 만들어둔 구글 스프레드시트
("언니삼총사 사연 시트")에서 Status="대기"인 사연 하나를 가져와
기존과 같은 출력 계약(output/script_{date}.json, turns 배열)으로 저장한다.

뒷단(generate_media.py, assemble_video.py)은 이 출력 형식만 맞으면
원래 코드를 그대로 재사용할 수 있다.

turns 구성:
  사연 문장마다 1개(speaker="reporter") + 현실언니 1개 + 공감언니 1개 +
  폭주언니 1개 + 질문 1개. 전원 같은 voice_id(ElevenLabs Rachel)를 쓴다.

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

# ElevenLabs Rachel - 4명 전원 동일 목소리(비용 최소화를 위해 내레이터 1명 방식)
VOICE_ID = "21m00Tcm4TlvDq8ikWAM"

CHARACTER_NAMES = {
    "reporter": "제보자",
    "real": "현실언니",
    "empathy": "공감언니",
    "rage": "폭주언니",
    "question": "",
}

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
    story_lines = [s.strip() for s in str(row["사연"]).split("\n") if s.strip()]
    if not story_lines:
        raise SystemExit("사연 컬럼이 비어 있습니다.")

    turns = []
    for line in story_lines:
        turns.append({"speaker": "reporter", "line": line})
    turns.append({"speaker": "real", "line": str(row["현실언니"]).strip()})
    turns.append({"speaker": "empathy", "line": str(row["공감언니"]).strip()})
    turns.append({"speaker": "rage", "line": str(row["폭주언니"]).strip()})
    turns.append({"speaker": "question", "line": str(row["질문"]).strip()})

    for i, t in enumerate(turns):
        t["index"] = i
        t["character_name"] = CHARACTER_NAMES[t["speaker"]]
        t["voice_id"] = VOICE_ID

    return turns


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

    today = dt.date.today().isoformat()
    output = {
        "date": today,
        "ep": ep,
        "title": f"EP.{ep} {title_raw}",
        "turns": turns,
    }

    os.makedirs("output", exist_ok=True)
    out_path = SCRIPT_PATH_TEMPLATE.format(date=today)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # 처리한 행은 다음 실행 때 건너뛰도록 완료 처리
    ws.update_cell(row_index, 1, "완료")

    print(f"완료: {out_path}")
    print(f"EP.{ep} {title_raw} - turn {len(turns)}개")


if __name__ == "__main__":
    main()
