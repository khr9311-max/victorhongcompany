# 아키텍처

단일 Python 3.12 서비스(asyncio) + SQLite(WAL) + FastAPI/Jinja2 대시보드. 마이크로서비스·Redis·프런트엔드 빌드 없음.

## 모듈 지도 (`src/aifund/`)

| 경로 | 책임 |
|---|---|
| `core/` | 시간(UTC 저장·KST 표시), Decimal 금액, 모드별 경로, 프로세스 잠금, 비밀 로딩·로그 마스킹 |
| `config/` | 설정 모델(pydantic, 검증 규칙), DB 버전 저장소(변경 이력·작성자·사유, 설정 파일 변경분 합치기) |
| `db/` | SQLite 접근(스레드별 커넥션, `BEGIN IMMEDIATE`, `synchronous=FULL`), 마이그레이션 |
| `markets/` | 호가 단위 규칙(업비트/KRX/미국), 거래 달력(exchange_calendars: XKRX·XNYS, 서머타임·휴장) |
| `brokers/` | `BrokerAdapter`·`MarketData` 인터페이스, 업비트, KIS, 키움(국내·미국 시세 조회 전용), 내부 모의체결(paper), 가짜 거래소(테스트), 중앙 호출 제한기 |
| `data/` | 스냅샷 수집(품질 검사), 시세 저장소, 환율, 뉴스(RSS·네이버 검색)·공시(DART), 데모/재생 데이터 |
| `strategies/` | 지표, 전략 인터페이스·등록부, 봇 A(추세), 봇 B(평균회귀) |
| `ai/` | LLMProvider(Claude/Gemini/Ollama/데모), 출력 스키마·결정적 검증, 증거 번들, 예산, 연구·검증 서비스, 버전 관리 프롬프트 |
| `portfolio/` | 조정기(전략 목표 → 상계 → 순주문) |
| `risk/` | 결정적 위험 검사 |
| `execution/` | 중앙 주문 실행기(예약·상태기계·체결 반영), 계좌 대사 |
| `ledger/` | 원장(현금·수량 = 원장 합), 평가·노출, 일손실·낙폭 기준 |
| `control/` | 영속 플래그·사고, LIVE 활성화·가드, 사전 점검, 정지·청산 동작, 자체검증 |
| `evaluation/` | A/B/C 지표, 백테스트, 개선 후보(승격·되돌리기) |
| `service/` | 앱 구성(모드별 격리·모의 원금 조정), 의사결정 사이클, 미국주식 모의 환전, 상시 런타임, 시뮬레이션, 과거 데이터 수집 |
| `web/` | 대시보드(인증·CSRF·Host 검사), 화면 데이터(`views.py`), 한국어 라벨(`labels.py`), 차트 데이터(`charts.py`), 템플릿·공용 조각(`templates/_ui.html`), 스타일·스크립트(`static/style.css`, `static/app.js` — 외부 라이브러리 없음) |
| `cli.py` | `aifund` 명령 |

## 의사결정 흐름

```
[시세·캔들 수집] → 스냅샷(ID·해시, 품질 이슈 기록)
      │
      ├─ 봇 A / 봇 B: 종목별 신호(매수·보유·축소·매도·관망 + 슬리브 대비 목표 비중)
      ├─ (B/C 설정) 연구 AI 보고서: 종목 입장·AI 슬리브 제안·전략 유효조건 — 매일 1회/이벤트
      │     └─ (C) 검증 AI: 독립 평가 → 제안별 채택/거절·반론
      ▼
[포트폴리오 조정기] 전략별 목표수량 − 현재수량 → 같은 종목 매수/매도 상계(내부 이전, 제로섬) → 순주문
      ▼
[위험 검사] 결정적 규칙(아래) → 의도(intent) 기록(승인/거부 사유)
      ▼
[중앙 주문 실행기] 원자적 예약 → 전송 → 상태 반영 → 원장 → 평가·손실 기준 갱신
```

운용 장부(`operating`)와 가상 장부 `shadow_A/B/C`, 기준선 `baseline_bh`(동일비중 매수·보유)·`baseline_cash`는 **같은 스냅샷**으로 각자 독립 판단·체결합니다. 가상 원금은 실제 예산과 합산하지 않습니다. 실주문은 운용 장부만 가능하고(live 모드 + LIVE 활성화), 가상 장부는 항상 내부 모의체결입니다.

시세 원천과 주문 경로는 분리되어 있습니다. 시장 설정 `data_provider`(`default`=업비트/KIS, `kiwoom`=키움 조회 전용)가 시세를, `broker`(`upbit`/`kis`/`paper`)와 모드가 주문 경로를 정합니다. 키움은 주문 API가 없으므로 키움 시세 시장의 주문은 내부 모의체결 또는 KIS입니다.

## 설정 파일과 버전

- `internal_paper`·`offline_demo`는 `config/paper.toml`(가상 원금 500만 원 멀티시장), `live`·`broker_sandbox`는 `config/config.toml`을 씁니다. 모드마다 DB가 따로라 한쪽 변경이 다른 쪽에 섞이지 않습니다.
- 시작할 때 설정 파일 해시가 직전 가져오기와 다르면 새 버전을 만듭니다. 이때 '직전에 가져온 파일 → 현재 파일'에서 바뀐 항목만 현재 설정(대시보드·CLI 변경 포함)에 덮어써, 파일이 건드리지 않은 대시보드 변경은 유지됩니다. 합친 결과가 검증(배정 합계 ≤ 원금 등)에 실패하면 파일 전체를 적용하고, 덮어쓴 항목을 버전 사유에 남깁니다. `aifund settings import <파일>`은 명시적 전체 가져오기입니다.
- 실행 중 설정 변경은 10초 안에 적용되지만, 시장 사용 여부·시세 공급자·모의 원금은 시작할 때 구성되므로 재시작이 필요합니다.

## 전략 슬리브와 AI의 역할

- 슬리브(기본): 봇 A 0.4, 봇 B 0.4, AI 0.2 (시장 배정액 대비). 전략 자금 = 시장 배정액 × 슬리브 + **그 시장에서** 난 해당 전략 손익(다른 시장 손익은 섞지 않음). A 설정에서는 AI 슬리브를 현금으로 둡니다 → A/B/C의 자본 배분이 같아 비교가 공정합니다.
- 연구 AI 입력에는 대표가 정한 연구 관심사(`ai.research_focus`, 있을 때만)가 들어갑니다. 관심사는 검토 방향일 뿐 허용 종목·한도·위험 검사를 바꾸지 못합니다.
- 정기 연구 일정(`AIService.research_slot`): 코인은 `daily_research_time_kst`부터 `crypto_research_interval_hours`마다, 주식은 거래일마다 거래소 개장 `stock_research_lead_min`분 전부터 판단 시각+2시간까지가 한 구간입니다(거래 달력으로 휴장·서머타임 반영). 구간마다 성공한 연구가 없을 때만 1회(실패 시 1시간 뒤 1회 재시도) 실행하고, 연구가 검증을 통과하면 검증 AI가 이어서 실행됩니다.

## 대시보드

서버가 원장·DB에서 숫자를 계산해 HTML로 그리고(스크립트 없이도 모든 숫자·표가 보임), `static/app.js`는 그래프(교차선·툴팁·키보드 이동), 테마 전환, 위험 동작 확인창, 자동 새로고침만 맡습니다. 그래프 데이터는 `<script type="application/json">`에 넣고 라벨은 `textContent`로만 삽입합니다. 평가 기록은 1분마다 쌓이므로 차트는 기간을 최대 약 240구간으로 나눠 구간별 마지막 값을 쓰고, 원금이 바뀐 시점 이전은 그리지 않습니다. 상태는 아이콘+문구로 표시하고(색만으로 구분하지 않음), 그래프 색은 색각 이상 검사를 통과한 고정 순서 팔레트를 씁니다(시장: 코인·국내·미국, 비교: A·B·C·매수보유).
- AI가 할 수 있는 일: AI 슬리브의 목표 비중 제안, 종목별 '반대 입장'으로 규칙 전략의 **신규 매수 보류**(`ai.veto_rule_buys`). AI는 규칙 전략에 매도를 강제하거나 가격·수량을 정하지 못합니다.
- C 설정: 검증 AI가 '채택 가능'으로 판정한 제안만, 그리고 검증 AI가 동의한 반대 입장만 반영.
- AI 근거가 만료되면(보고서 없음이 TTL을 넘기면) AI 슬리브 보유분을 정리합니다.

## 주문 상태 기계

`pending → submitted → partially_filled → filled`
`submitted/partially_filled → cancel_pending → canceled | filled`
`pending(전송 전) → rejected` / 전송 후 응답 유실 → `unknown → (조회) → submitted… | rejected(미접수 확정)`

- `pending`에 `submit_attempted_at`이 없으면 재시작 시 '미전송 확정'으로 정리, 있으면 `unknown`.
- 멱등 키: 주문 ID(`af…`)를 업비트 `identifier`로 사용. 같은 intent 재시도는 같은 주문을 돌려줌.
- `unknown`: 업비트는 identifier 조회 2회+10초 후 없으면 미접수 확정. KIS는 클라이언트 ID가 없어 당일 주문조회에서 종목·방향·수량·가격·시각이 **유일하게** 맞을 때만 연결, 해소 창(15분)·3회 조회 후에만 미접수 확정. 해소 전에는 해당 계좌의 위험 증가 주문 차단.
- 체결: 거래소 체결 ID(`fills(order_id, fill_key)` UNIQUE)로 중복 제거. 누적값만 주는 거래소는 누적 증가분만 반영(감소=순서 역전 → 무시). 거래소가 수수료를 주지 않으면 추정 수수료로 표시.
- 취소: 취소 요청 성공은 `cancel_pending`일 뿐이며, 거래소가 `cancel/done`을 확인한 뒤에만 종료·예약 해제. 취소 대기 중 체결도 반영.

## 자금 예약(동시성)

`reserve_and_create()`는 `BEGIN IMMEDIATE` 트랜잭션 안에서 ① 가용 현금 = 원장 현금 − 활성 예약, ② 총노출 = 보유 평가 + 매수 예약(수수료 여유 포함) + 신규 ≤ 한도, ③ 매도 가능 수량 = 봇 수량 − 매도 예약을 확인하고 주문·배분·예약을 함께 기록합니다. SQLite 쓰기 직렬화로 동시 전략·스레드·프로세스가 같은 현금을 중복 사용할 수 없습니다. 서비스 자체는 OS 파일 잠금으로 모드당 1개만 실행됩니다.

## 원장·손익 귀속

- 모든 현금·수량 변화는 `ledger_entries`(원금 배정, 체결, 수수료, 내부 이전). `positions`는 파생 상태이며 `verify()`로 재계산 검증.
- 체결은 주문의 전략별 요청 비율로 누적 기준 배분(반올림 오차 누적 없음), 수수료도 같은 비율.
- 전략 손익 합계 = 장부 실제 손익(내부 이전은 제로섬). 평균원가법(매수 수수료는 원가, 매도 수수료는 실현손익 차감).
- 입금은 운용 한도를 늘리지 않습니다(봇 자산은 원장 기준). 일 기준·고점 자산은 `risk_state`에 저장되어 재시작 후에도 유지.
- 모의 원금 조정(`internal_paper`·`offline_demo`만): 설정 원금이 장부 원금과 다르면 시작 시 차액을 `principal`(`ref_type='paper_capital'`)으로 기록하고 모의 잔고·`risk_state` 기준도 같은 금액만큼 옮깁니다. 줄일 때는 예약금을 뺀 원화 현금이 충분해야 하며, 부족하면 시작을 거부합니다.
- 미국주식 모의 환전: 미국 사이클마다 신선한 환율로 '시장 배정액 − 누적 환전액' 이내의 원화를 달러로 바꿉니다. 원장(`kind='fx'`, `ref_type='paper_fx'`, 원화 차감·달러 입금)과 모의 잔고를 한 트랜잭션에 기록하고, 실계좌 브로커에는 적용하지 않습니다. 환전 수수료는 반영하지 않습니다.

## 위험 검사(결정적)

모든 주문: 재시작 대사 완료, 계좌 차단(대사 불일치·인증 오류) 없음, (live 운용 장부) LIVE 활성 범위, 호가 신선도, 수량·호가 단위, 최소/최대 주문 금액. 매도: 봇 보유분 이내, 같은 종목 상태불명 없음.
위험 증가(매수) 추가: 상태불명 주문 없음, 신규 매수 중지 아님, 일손실·최대낙폭 정지 아님, AI 보류 정책, 데이터 품질, 장 운영, 상품 상태, 스프레드, 시계 오차, (외화) 환율 신선도, 계좌 인증, 1회 주문 한도, 최대 보유 종목 수. 현금·총노출은 실행기 예약 트랜잭션이 최종 판정.

## 보안·권한

- 비밀은 `.env`에서만, 모드에 필요한 키만 로딩(live 키는 live 모드에서만). 로그 필터가 등록된 비밀값·Bearer·JWT·`sk-ant-` 패턴을 마스킹.
- AI 호출에는 도구가 없고(주문·쉘 불가), 출력은 스키마·결정적 검증 후 '제안'으로만 쓰입니다. 설정 저장소는 AI 역할을 작성자로 받지 않습니다. 뉴스 텍스트는 `sources_UNTRUSTED_DATA` 영역의 데이터일 뿐입니다.
- 대시보드: 기본 127.0.0.1, Host/Origin 검사(DNS 재바인딩 방지), 상태 변경은 로그인 + CSRF. 원격 바인딩은 `AIFUND_ALLOW_REMOTE=1` + 32자 이상 토큰이 없으면 시작 거부, 원격일 때는 조회에도 로그인 필요.

## 저장소(주요 테이블)

`settings_versions`, `service_runs`, `control_flags`/`control_events`/`control_commands`, `live_activations`/`live_checks`/`selftest_runs`, `account_baselines`, `broker_health`, `books`, `instruments`, `candles`, `quotes`, `fx_rates`, `snapshots`, `sources`, `cycles`, `signals`, `ai_runs`/`ai_budget`/`ai_reports`, `proposals`, `intents`, `orders`/`order_events`/`order_allocations`/`fills`/`reservations`, `ledger_entries`/`positions`, `equity_snapshots`/`risk_state`, `reconciliations`, `incidents`, `strategy_candidates`, `notifications`, `paper_orders`/`paper_balances`.
추적성: 제안(`snapshot_id`, `settings_version`, `code_version`, `prompt_version`, `ai_report_id`) → 의도(위험 사유) → 주문(이벤트) → 체결 → 원장. 대시보드 **주문 → 상세**에서 한 화면으로 확인.
