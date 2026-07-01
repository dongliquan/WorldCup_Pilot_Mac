# World Cup Pilot

로컬/개인용 FIFA 월드컵 뷰어 + **예측 엔진**. `pywebview` 네이티브 창 + 백그라운드 로컬 서버(`127.0.0.1:8770`) +
단일 HTML UI + PyInstaller `.app` 번들 패턴입니다. **무료 공개 소스만** 조합해 일정·결과·순위·대진·선수·영상과
**자체 예측·AI 예측·예측 정확도 비교**까지 보여주며, **역대 월드컵(1930~2026)** 을 회차 선택으로 볼 수 있습니다.
**macOS(.app) 전용.** 기능은 Windows 판(`WorldCup_Pilot`)과 동일합니다. 상용 배포 용도가 아닙니다.

## 구성

| 파일 | 역할 |
|------|------|
| `worldcup.py`   | 런처 — 로컬 서버를 스레드로 띄우고 네이티브 WebKit 창을 엶. Dock 아이콘, 데스크톱 알림(osascript), 영상 팝업, 시작 시 정적 데이터 프리빌드 |
| `server.py`     | `127.0.0.1:8770` 로컬 서버 — `/`(UI) + `/api/*`(여러 소스 프록시·정규화·디스크 캐시·예측·튜닝) |
| `worldcup.html` | 단일 페이지 UI (다크/라이트, 일정·조별·대진·예측성적, 팀/선수/경기 상세, 예측·예상 라인업, 영상, 알림, 4개국어) |
| `config.json`   | 시즌·캐시 설정 + (선택) AI 키 — **gitignore됨** |
| `assets/`       | 정적 데이터(JSON) + 아이콘/로고 |
| `model_params.json` / `tune_state.json` | 자가 튜닝된 예측 파라미터 + 마지막 튜닝 지문 (캐시 밖, 영구) |
| `worldcup.spec` / `build.sh` | **macOS** .app 번들 빌드 |
| `icon.ico` / `icon.icns` | 앱 아이콘 원본(FIFA 26 엠블럼) / 그로부터 생성한 macOS 번들 아이콘 |

### assets (정적 데이터 — 모두 편집 가능)
- `country_info.json` — 국가별 수도·인구·면적·ISO2(국기/지도)·월드컵 역대 성적
- `fifa_ranking.json` / `fifa_ranking_history.json` — 현재 / 회차별 FIFA 랭킹(1993~)
- `venues.json` — 개최 도시→시간대 + 경기 id→도시 매핑(현지시간용)
- `wc_editions.json` — 역대 대회(개최국·우승/준우승·MVP·골든부트)
- `icon.png` / `logo.png` — 대체 아이콘 + 헤더 엠블럼 (앱 아이콘은 루트 `icon.ico`→`icon.icns`)

## 데이터 소스 (전부 무료, 토큰 불필요 — football-data.org / API-Football 미사용)

| 데이터 | 소스 |
|------|------|
| 일정·스코어·순위·대진·팀(감독)·LIVE·배당(DraftKings) | **ESPN** (키 불필요) |
| 경기 이벤트(골·카드)·경기장 도시·선수 경기 스탯 | **ESPN** summary |
| 선수단(이름·나이·생일·신장·체중·부상) — 당시 시즌 기준 | **ESPN** roster (`?season=연도`) |
| 선수 사진·소속팀·소속팀 소재국 | **TheSportsDB** → **Wikipedia** 폴백 (전역 스로틀) |
| 국기 | **flagcdn** (영국 구성국 gb-eng/gb-sct/gb-wls/gb-nir 포함) |
| 경기장 이미지·과거 대회 엠블럼 | **Wikipedia** |
| 온도·습도·풍속 | **Open-Meteo** (키 불필요) |
| 국가 지도 윤곽 | **mapsicon** (GitHub, ISO2) |
| 개막식·결승·하이라이트 영상 | **YouTube**(공식 FIFA 검색 1순위) → 앱 내 팝업 재생 |
| AI 예측 | **Groq**(Llama 3.3, 무료) · **ChatGPT**(OpenAI 또는 GitHub Models) |

> **회차 선택 시 모든 정보가 당시 기준**(선수·나이·순위·결과). FIFA 랭킹은 1993년 시작 → 이전 대회는 미표시.

## 예측 (🔮 Elo x Score) — 자체 알고리즘

킥오프 전 정보만으로 승/무/패 확률 + 예상 스코어(포아송)를 산출합니다. 가중치는 UI 슬라이더로 실시간 조정 가능.

반영 요소:
- **Elo** — FIFA 랭킹 시드 + 대회 결과로 갱신
- **최근 폼** — ATT/MID/DEF/GK 4지표, 최근 경기 가중(shrinkage 혼합)
- **부상 / 출전정지** — **선수 평점(ESPN 경기 스탯 산출)으로 가중** (핵심 선수 결장 = 더 큰 약화)
- **카드 규칙(FIFA)** — 경고 2장 누적 → 다음 1경기 정지(소화 후 리셋), 레드 → 정지, **8강 종료 후 단일 경고 소멸**
- **실제 라인업** — 공식 선발 XI 공시 시, 베스트 XI 대비 실제 출전 멤버 평점 차이만큼 전력 보정
- **홈 이점 · 휴식일 · 이동거리 · 날씨**

**자가 튜닝**: 헤더 ↻(Refresh) → 9개 월드컵(2026+과거 8개) 실제 결과로 모양 파라미터(avg/tiltScale/tiltCap/formK) 그리드 서치 →
`model_params.json` 저장. **새 결과가 있을 때만** 재튜닝(스로틀), 재계산은 백그라운드(버튼은 항상 가벼움).

## AI 예측 (⚡ Groq · 💬 ChatGPT)

실시간 LLM에게 동일한 사전 브리핑을 주고 예측을 받습니다. **키 없이도** 캐시된 예측은 표시되며, 키를 넣으면 새 경기도 실시간 호출.
- **Groq** — `console.groq.com` 무료 키(`gsk_…`), Llama 3.3 70B
- **ChatGPT** — OpenAI 키(`sk-…`, 유료 크레딧) 또는 **GitHub 토큰(`ghp_…`, 무료 GitHub Models)**
- 캐시 정책: **stale-while-revalidate** — 캐시 즉시 표시 → 백그라운드 재검증 → 성공 시에만 교체(실패/한도여도 안 비움)

## 예측성적 (📊 Accuracy)

완료된 경기에서 4개 예측기(🔮 Elo x Score / 💰 DraftKings / ⚡ Groq / 💬 ChatGPT)를 실제 결과와 채점.
- 차전·스테이지별 아코디언(최신 위), **조별(A~L) 그룹핑**, 리더보드(정확도→스코어 적중 순)
- 승부 적중(초록) / 정확 스코어(🎯) / 무승부 색상 일치
- **예정 라운드도 예측 미리 표시**(채점 전), AI 픽은 백그라운드 워밍
- 현재 대회(2026) 전용

## 조별 (▦) — 실시간 순위 + 진출 현황

- 조 카드 헤더에 **조별 고유 색**(인접 조 대비), 일정 카드 좌측 컬러바 + 조 글자
- **2026 포맷**: 각 조 1·2위 + **3위 중 상위 8팀** → 32강. 미니표/순위표에 진출(🟢)·3위 진출권(🟡) 표시
- 경기 상세에 **라이브 조 미니표**(공식 순위 기준 + 진행 중 경기만 실시간 오버레이): 순위·득점·실점·득실·승점·카드·진출상태

## 예상 라인업 (🔮 / 👥)

- 공시 전 → **예상 XI**: 부상·정지 제외, 빈 자리는 평점 높은 선수 우선, 포메이션은 상대 전력차에 맞춤
- 공시 후(킥오프 ~1시간 전) → **실제 발표 XI**(선발 + 교체 + 카드)로 자동 전환(2분 내)
- 실제 포메이션(3-4-2-1 등) 피치 렌더, 양 팀 라인 정렬

## 설정 (config.json)

`config.example.json` → `config.json` 복사. **AI 키는 선택**(없어도 Elo x Score·DraftKings·캐시된 AI 픽은 동작):

```json
{
  "groq_api_key": "",
  "openai_api_key": "",
  "competition": "WC",
  "season": 2026,
  "cache_ttl_seconds": 60,
  "venue_timezone": "America/New_York",
  "use_mock_when_unavailable": false
}
```
- `cache_ttl_seconds`(기본 60): 라이브용 짧은 캐시. **정적 데이터·완료 경기·예측은 영구 캐시**.
- 키 변경 후에는 앱 재시작 필요.

## 실행 / 빌드

**개발 모드**
```
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python worldcup.py
```
서버만: `.venv/bin/python server.py` → http://127.0.0.1:8770

> 시스템 Python 3.9는 pyobjc 빌드가 안 돼 **brew Python 3.12** 로 `.venv` 사용.

**macOS 빌드**
```
./build.sh                                      # -> dist/World Cup Pilot.app (아이콘: icon.icns)
open "dist/World Cup Pilot.app"
```

## API (로컬)

GET: `/api/status` · `/api/matches[?year=]` · `/api/standings[?year=]` · `/api/team?id=|name=[&year=]` ·
`/api/match?id=` · `/api/predict?id=` · `/api/lineup?id=` · `/api/aipick?id=&p=groq|openai` ·
`/api/accuracy` · `/api/model-params` · `/api/playerclub?name=` · `/api/highlight?q=` · `/api/wiki-image?title=`
POST: `/api/refresh`(데이터 재검증 + 모델 재튜닝) · `/api/grade-ai`(AI 픽 백그라운드 채점) · `/api/save-edition?year=`

## 캐시 / 유지보수

- 캐시: `cache/*.json` + 사진 `cache/img/`. 헤더 ↻ = 재검증 + 모델 재튜닝(캐시는 안 비움 → UI 안 깜빡임).
- 정적 JSON(`assets/*.json`) 값이 틀리면 직접 편집 → 재시작 시 반영.
- TheSportsDB 무료 한도가 빡빡 → **요청 간 전역 스로틀** + 사진 백그라운드 워밍(받는 즉시 영구 저장).
- **배포**: `dist/World Cup Pilot.app` 을 압축/전달. 배포 시 `config.json`의 실제 키는 반드시 제거.
