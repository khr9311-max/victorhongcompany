# Gemini와 네이버 뉴스

Gemini를 기존 연구·독립 평가·반론 검토 공급자로 선택할 수 있습니다. 네이버 검색 결과는 RSS와 함께 기존 근거 번들과 연구 화면에 표시됩니다. 원문 URL, 발표 시각, 수집 시각을 보관하고 같은 시장의 동일 URL은 중복 저장하지 않습니다.

## 설정

`.env`에 `GEMINI_API_KEY`, `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`을 설정합니다. 거래소 키는 필요하지 않습니다. 기존 환경변수가 `.env`보다 우선합니다. 변경한 키는 서비스 재시작 후 적용됩니다.

내부 모의운용(`config/paper.toml`)은 이미 Gemini·네이버를 쓰도록 설정되어 있습니다. 다른 모드나 기존 DB에서 바꾸려면 둘 중 하나를 사용합니다.

- **대시보드 설정 화면**: AI 공급자 `gemini` + 모델 `gemini-3.8-flash`(공급자만 바꾸고 모델을 그대로 두면 'AI 사용 불가: 공급자·모델 불일치'로 호출하지 않음), 추론 강도, 연구 일정, '네이버 뉴스 수집' 체크, 시장별 '네이버 뉴스 검색어', '연구 관심사'.
- **설정 파일**: 모드에 맞는 파일(`internal_paper`·`offline_demo` → `config/paper.toml`, `live`·`broker_sandbox` → `config/config.toml`)을 고치고 서비스를 재시작합니다. 파일에서 바뀐 항목만 반영되고 대시보드에서 바꾼 다른 값은 유지됩니다. `settings import`는 파일 전체를 덮어쓰므로 보통은 필요 없습니다.

```toml
[ai]
provider = "gemini"
model = "gemini-3.8-flash"
effort = "high"                                    # Gemini 추론 강도(thinkingLevel: low·medium·high)
research_focus = "AI·반도체·IT를 우선 검토한다."   # 선택: 연구·검증 AI에 전달할 대표 지시
use_server_fallbacks = false                       # Claude 전용 기능
monthly_budget_krw = "150000"                      # 호출 폭주 방지용 상한
max_output_tokens = 16000                          # 추론 토큰 포함
crypto_research_interval_hours = 6                 # 코인: daily_research_time_kst부터 6시간마다
stock_research_lead_min = 30                       # 주식: 거래일 개장 30분 전

[ai.pricing."gemini-3.8-flash"]
input_usd_per_mtok = "0.75"
output_usd_per_mtok = "3.75"
source = "https://ai.google.dev/gemini-api/docs/pricing"
checked_at = "2026-09-30"
changes_on = "2027-01-01"                          # 예고된 요율 변경일부터 아래 요율로 자동 계산
new_input_usd_per_mtok = "1.50"
new_output_usd_per_mtok = "7.50"

[news]
naver_enabled = true
[news.naver_queries]   # 시장별 최대 10개
crypto = ["비트코인", "이더리움", "리플"]
kr_stock = ["삼성전자 반도체", "SK하이닉스 HBM"]
us_stock = ["엔비디아 AI"]
```

네이버는 활성 시장별 검색어(최대 10개, 넘으면 설정 저장 거부)를 기존 뉴스 수집 주기에 조회합니다. 기본 주기는 60분이며 API 실패는 연구 화면의 수집 상태에 HTTP 상태 코드(예: 401 키 오류, 429 한도 초과)로 표시합니다. AI 보고서가 없어도 연구 화면에서 수집 뉴스를 볼 수 있습니다.

## 비용과 동작

요율이 없는 모델은 자동 호출을 차단합니다. 호출 전 입력·스키마의 UTF-8 길이를 이용해 보수적으로 예산을 예약하고, 응답의 입력 및 출력·추론 토큰으로 정산합니다. 캐시 할인은 적용하지 않는 보수적 계산입니다. 무료 티어도 설정 요율로 기록하므로 이 금액은 공급자의 청구서와 다를 수 있습니다. 사용량 없는 응답·타임아웃은 예약액으로 정산하며, 인증·요청·요청 제한 오류는 예약을 해제합니다. 출력 잘림과 거절은 AI 제안으로 채택하지 않습니다.

### 모델·빈도 선택(2026-09-30, 비용보다 분석 품질 우선)

- 모델: `gemini-3.8-flash` — 공식 문서 기준 최신 정식(Stable) Flash(2026-09-02 출시, 종료 일정 없음, 입력 1M·출력 64k 토큰). Pro 계열은 미리보기(`gemini-3.1-pro-preview`)뿐이라 상시 운영에 쓰지 않았습니다.
- 추론 강도: 설정 `ai.effort`를 Gemini 3의 `generationConfig.thinkingConfig.thinkingLevel`로 보냅니다(low·medium·high, xhigh·max는 high). 3.8 Flash 기본값은 medium이며 내부 모의·LIVE 설정은 high입니다. Gemini 2.5 이하 모델에는 보내지 않습니다.
- 연구 시각: 코인은 08:50부터 6시간마다(08:50·14:50·20:50·02:50), 국내·미국주식은 각 거래소 개장 30분 전(국내 08:30, 미국 한국시간 22:00·서머타임 해제 시 23:00)에 연구하고 이어서 검증합니다. 이전처럼 모든 시장을 08:50 한 번에 연구하면 미국주식은 14시간 지난 자료로 판단하게 됩니다.
- 분량: 출력 상한 16,000토큰(추론 포함, 잘린 응답은 제안으로 쓰지 않음), 입력 최대 60,000자·뉴스 60건, 급변 시 시장별 하루 최대 3회 추가 연구.
- 비용 추정: 호출 1회 입력 2~3만·출력 수천 토큰이면 약 50~70원, 하루 약 6세트(연구+독립 평가+검증)로 월 3~4만 원 안팎입니다. 2027-01-01부터 요율이 2배가 되어 자동으로 그 요율로 계산합니다. 월 한도 150,000원은 폭주 방지용입니다.
- 참고 실측(이전 모델 `gemini-3.5-flash-lite`, 2026-09-29 실행): 호출 1회 입력 약 1.2~1.5만·출력 0.4~1.6천 토큰, 약 7~11원, 3개 시장 한 세트 약 75원.

REST generateContent를 사용하므로 google-genai SDK에 의존하지 않습니다(의존성 추가 없음). 이번 구현에는 Google 검색 Grounding, 네이버 데이터랩, 별도 거시 지표, 공급자가 다른 검증 AI는 포함하지 않았습니다. 연구와 검증은 선택한 동일 공급자를 사용합니다. 실거래 활성화와는 별개이며 `offline_demo`에서는 Gemini·네이버 키를 읽거나 해당 API를 호출하지 않습니다(데모 가짜 응답 사용).

## 확인한 공식 문서

- [Gemini 구조화 출력](https://ai.google.dev/gemini-api/docs/generate-content/structured-output)
- [Gemini 요율](https://ai.google.dev/gemini-api/docs/pricing): 2026-09-24 갱신본을 2026-09-30 확인, 무료/유료 티어의 데이터 사용 조건도 이 페이지에서 확인할 수 있습니다.
- [Gemini 모델 목록](https://ai.google.dev/gemini-api/docs/models) · [지원 종료 일정](https://ai.google.dev/gemini-api/docs/deprecations) · [Gemini 3.8 Flash 변경 사항](https://ai.google.dev/gemini-api/docs/generate-content/latest-model)(thinkingLevel, 출력 64k): 2026-09-30 확인.
- [네이버 뉴스 검색](https://github.com/naver/naver-openapi-guide/blob/master/ko/service-apis/search/news/news.md)
