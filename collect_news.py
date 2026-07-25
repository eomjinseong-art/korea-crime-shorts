"""
1일 1쇼츠 자동화 파이프라인 - 2~3단계
- 한국 뉴스 RSS에서 '사건사고' 카테고리 기사를 수집
- 화제성(키워드 가중치 + 최신성) 기준으로 랭킹
- Claude API로 원문 문장을 저장하지 않고 사실관계(5W1H)만 구조화 추출

이후 4단계(대본 생성)에서 이 스크립트의 출력 JSON을 입력으로 사용합니다.

필요 환경변수:
  ANTHROPIC_API_KEY   - Claude API 키

필요 패키지:
  pip install requests feedparser anthropic --break-system-packages
"""

import os
import json
import re
import datetime as dt
from dataclasses import dataclass, asdict

import feedparser
import requests
from anthropic import Anthropic

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------

# 한국 주요 언론사의 '사회' 섹션 RSS. 필요에 따라 추가/제거하세요.
RSS_FEEDS = [
    "https://www.yna.co.kr/rss/society.xml",       # 연합뉴스 사회
    "https://rss.donga.com/national.xml",          # 동아일보 사회
    "https://www.khan.co.kr/rss/rssdata/society_news.xml",  # 경향신문 사회
]

# 화제성 점수를 매길 때 가중치를 줄 키워드 (사건사고 성격이 강할수록 높은 점수)
KEYWORD_WEIGHTS = {
    "살인": 10, "사망": 8, "숨진": 8, "폭행": 6, "감금": 7,
    "화재": 6, "붕괴": 7, "실종": 6, "성폭행": 9, "마약": 6,
    "사기": 4, "음주운전": 6, "교통사고": 5, "추락": 6, "체포": 4,
}

# 하루에 최종적으로 대본화까지 진행할 후보 개수 (상위 N개 중 1개를 최종 선택)
TOP_N_CANDIDATES = 5

OUTPUT_PATH = "output/facts_{date}.json"


# ---------------------------------------------------------------------------
# 데이터 구조
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    title: str
    link: str
    published: str
    source: str
    score: int


@dataclass
class ExtractedFacts:
    who: str
    when: str
    where: str
    what: str
    outcome: str
    source_link: str  # 내부 추적용. 대본/영상에는 노출하지 않음


# ---------------------------------------------------------------------------
# 2단계: 소스 수집 & 랭킹
# ---------------------------------------------------------------------------

def fetch_candidates() -> list[Candidate]:
    """RSS 피드에서 오늘 발행된 기사를 모두 수집한다."""
    today = dt.date.today()
    candidates = []

    for feed_url in RSS_FEEDS:
        try:
            parsed = feedparser.parse(feed_url)
        except Exception as e:
            print(f"[warn] {feed_url} 파싱 실패: {e}")
            continue

        source_name = parsed.feed.get("title", feed_url)

        for entry in parsed.entries:
            published = entry.get("published", "")
            # 오늘 발행된 기사만 (파싱 실패 시 일단 포함해서 나중에 사람이 검수 없이도
            # 오래된 기사가 섞이지 않도록 최신성 점수에서 걸러지게 둔다)
            title = entry.get("title", "")
            link = entry.get("link", "")
            if not title or not link:
                continue

            score = score_candidate(title, published, today)
            candidates.append(
                Candidate(title=title, link=link, published=published,
                          source=source_name, score=score)
            )

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:TOP_N_CANDIDATES]


def score_candidate(title: str, published: str, today: dt.date) -> int:
    """키워드 가중치 + 최신성으로 화제성 점수를 매긴다.

    주의: 이건 '진짜 조회수'가 아니라 대체 지표입니다. 실시간 조회수 데이터가
    필요하면 네이버/다음 뉴스 랭킹 페이지나 유료 트렌드 API 연동으로 교체하세요.
    """
    score = 0
    for keyword, weight in KEYWORD_WEIGHTS.items():
        if keyword in title:
            score += weight

    # 최신성 보너스: 오늘 기사면 +5, 아니면 감점
    try:
        pub_date = dt.datetime(*feedparser._parse_date(published)[:6]).date()
        if pub_date == today:
            score += 5
        elif (today - pub_date).days > 1:
            score -= 5
    except Exception:
        pass  # 날짜 파싱 실패해도 키워드 점수만으로 랭킹

    return score


# ---------------------------------------------------------------------------
# 3단계: 사실관계 구조화 (저작권 세이프 - 원문 문장 저장 안 함)
# ---------------------------------------------------------------------------

def fetch_article_text(url: str, timeout: int = 10) -> str:
    """기사 본문을 가져온다. 이 텍스트는 Claude에 전달만 하고 디스크에 저장하지 않는다."""
    resp = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    # 실제 운영에서는 언론사별 본문 셀렉터를 붙인 파서(예: readability-lxml)를
    # 쓰는 걸 추천합니다. 여기서는 개념 증명 수준으로 태그만 대략 제거합니다.
    text = re.sub(r"<[^>]+>", " ", resp.text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:8000]  # 토큰 절약을 위해 앞부분만 사용


def extract_facts(client: Anthropic, article_text: str, source_link: str) -> ExtractedFacts:
    """Claude API로 사실관계(5W1H)만 뽑아낸다. 원문 문장은 절대 그대로 반환하지 않도록 지시한다."""
    system_prompt = (
        "당신은 뉴스 기사에서 핵심 사실관계만 추출하는 리서치 어시스턴트입니다. "
        "아래 규칙을 반드시 지키세요.\n"
        "1. 기사 원문의 문장이나 표현을 그대로 옮기지 말고, 완전히 당신의 언어로 요약하세요.\n"
        "2. 특정 개인의 실명이 있다면 실명 대신 '30대 남성', '피해자' 같은 익명 표현으로 바꾸세요.\n"
        "3. 반드시 JSON만 출력하세요. 다른 설명이나 마크다운은 포함하지 마세요.\n"
        "4. 출력 스키마: {\"who\": str, \"when\": str, \"where\": str, "
        "\"what\": str, \"outcome\": str}"
    )

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system=system_prompt,
        messages=[{"role": "user", "content": article_text}],
    )

    raw = "".join(block.text for block in message.content if block.type == "text")
    raw = raw.strip().removeprefix("```json").removesuffix("```").strip()
    data = json.loads(raw)

    return ExtractedFacts(
        who=data["who"], when=data["when"], where=data["where"],
        what=data["what"], outcome=data["outcome"], source_link=source_link,
    )


# ---------------------------------------------------------------------------
# 메인 파이프라인
# ---------------------------------------------------------------------------

def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY 환경변수가 필요합니다.")

    client = Anthropic(api_key=api_key)

    print("[1/3] 후보 기사 수집 중...")
    candidates = fetch_candidates()
    if not candidates:
        raise SystemExit("오늘 조건에 맞는 기사를 찾지 못했습니다. RSS_FEEDS나 키워드를 점검하세요.")

    print(f"[2/3] 상위 {len(candidates)}개 후보 확보. 최고 점수 기사로 사실관계 추출 중...")
    top = candidates[0]
    print(f"  선정: {top.title} (score={top.score}, source={top.source})")

    article_text = fetch_article_text(top.link)
    facts = extract_facts(client, article_text, top.link)

    print("[3/3] 결과 저장 중...")
    os.makedirs("output", exist_ok=True)
    out_path = OUTPUT_PATH.format(date=dt.date.today().isoformat())
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "candidates_considered": [asdict(c) for c in candidates],
                "selected_facts": asdict(facts),
            },
            f, ensure_ascii=False, indent=2,
        )

    print(f"완료: {out_path}")


if __name__ == "__main__":
    main()
