# 외부 연결 범위·제약 (확인일: 2026-09-29)

표기: **확인** = 공식 문서/예제에서 확인하고 이 환경에서 실제 호출까지 성공 · **문서 확인** = 공식 자료로 확인했으나 키가 없어 계약 테스트(가짜 HTTP)만 통과 · **미확정** = 공식 자료가 상충하거나 모호.

## 업비트 (docs.upbit.com/kr, llms.txt 색인 기준)

| 항목 | 내용 | 상태 |
|---|---|---|
| 공개 시세 | `/v1/market/all?isDetails=true`, `/v1/candles/minutes/{unit}`(1,3,5,10,15,30,60,240)·`/days`(최대 200개, `to`로 과거 페이지), `/v1/ticker`, `/v1/orderbook`, `/v1/orderbook/instruments`(tick_size) | 확인(실호출) |
| 인증 | JWT HS512, payload `access_key`·`nonce`(UUID)·`query_hash`(비인코딩 쿼리 문자열 SHA512)·`query_hash_alg`, `Authorization: Bearer` | 문서 확인 |
| 주문 | `POST /v1/orders` (`ord_type=limit`, `identifier`=클라이언트 주문 ID). 중복 identifier는 `duplicated_identifier` 오류 | 문서 확인 |
| 주문 검증 | `POST /v1/orders/test` — 실제 주문 없이 형식·가능 여부 검증(LIVE 사전 점검에 사용, 별도 identifier 사용) | 문서 확인 |
| 조회·취소 | `GET/DELETE /v1/order`(uuid 또는 identifier), `GET /v1/orders/open`(`states[]`), `GET /v1/orders/chance`(수수료·최소 주문), `GET /v1/accounts`(포켓 잔고), `GET /v1/api_keys`(만료일) | 문서 확인 |
| 요청 제한 | 시세 그룹별 초당 10회(IP), Exchange `default` 초당 30회·`order` 초당 12회(2026-08-21 상향)·`order-test` 8회, **포켓 단위**(2026-06-25 변경). `Remaining-Req` 헤더 반영, 429 일시 중지, 418 장기 차단 | 문서 확인 |
| 원화마켓 호가 단위 | 2025-07-31 개편 표(문서 2026-05-04 갱신본). API tick_size가 더 크면 더 큰 단위 사용 | 확인 |
| 최소 주문 | 원화마켓 5,000원(문서), 실제로는 `orders/chance`의 `min_total` 사용 | 문서 확인 |
| 포켓 | 메인 1개 + 서브 최대 5개. 서브포켓 키는 외부 입출금 권한 없음 → **봇 전용 서브포켓 권장**(자금·보유 격리) | 문서 확인 |
| 테스트넷 | 공식 테스트넷 확인되지 않음 → `broker_sandbox`에서 코인 미지원, 모의는 내부 paper만 | - |
| 거래소 측 손절 | 주문 API에 스톱/보호 주문 유형 확인 안 됨 → 사용하지 않음(`server_side_stop=False`) | - |
| 공지(WebSocket) | 공식 WebSocket 공지 스트림은 있으나 이번 구현은 사용하지 않음. 비공식 공지 REST는 사용하지 않음 | - |

## 한국투자증권 KIS Open API (github.com/koreainvestment/open-trading-api `examples_llm`, 최신 커밋 2026-09-28 기준 / apiportal.koreainvestment.com)

도메인: 실전 `https://openapi.koreainvestment.com:9443`, 모의 `https://openapivts.koreainvestment.com:29443`. 토큰 `POST /oauth2/tokenP`(유효 1일, 파일 캐시로 재발급 최소화 — 발급 빈도 제한 1분 1회로 가정). 헤더 `authorization`·`appkey`·`appsecret`·`tr_id`·`custtype=P`·`tr_cont`. 예제의 호출 간격(실전 0.05초, 모의 0.5초)에 맞춰 실전 초당 15회·모의 초당 1.8회로 제한.

| 기능 | 경로 | TR(실전/모의) | 상태 |
|---|---|---|---|
| 국내 현금주문 | `/uapi/domestic-stock/v1/trading/order-cash` | 매수 TTTC0012U/VTTC0012U, 매도 TTTC0011U/VTTC0011U (`ORD_DVSN=00` 지정가, `EXCG_ID_DVSN_CD=KRX`) | 문서 확인 |
| 국내 정정취소 | `.../order-rvsecncl` | TTTC0013U/VTTC0013U (`KRX_FWDG_ORD_ORGNO`는 일별체결의 `ord_gno_brno` 사용 — 모의투자에서 확인 필요) | 문서 확인 |
| 국내 일별 체결 | `.../inquire-daily-ccld` | TTTC0081R/VTTC0081R (3개월 이내) | 문서 확인 |
| 국내 잔고 / 주문가능 | `.../inquire-balance`, `.../inquire-psbl-order` | TTTC8434R/VTTC8434R, TTTC8908R/VTTC8908R(`nrcvb_buy_amt`=미수 없는 매수금액 사용) | 문서 확인 |
| 국내 미체결 | `.../inquire-psbl-rvsecncl` | TTTC0084R (**실전 전용**) → 모의는 일별체결로 대체 | 문서 확인 |
| 국내 시세 | `inquire-price` FHKST01010100(`aspr_unit` 호가단위), 호가 `inquire-asking-price-exp-ccn` FHKST01010200, 일봉 `inquire-daily-itemchartprice` FHKST03010100 | 공통 | 문서 확인 |
| 미국 주문 | `/uapi/overseas-stock/v1/trading/order` | 매수 TTTT1002U/VTTT1002U, 매도 TTTT1006U/**VTTT1001U** | **미확정**: 공식 예제 주석은 VTTT1001U, 같은 예제 코드는 'V'+TTTT1006U를 만듦 → 모의투자에서 실제 확인 필요 |
| 미국 취소 | `.../order-rvsecncl` | TTTT1004U/VTTT1004U | 문서 확인 |
| 미국 미체결·체결 | `inquire-nccs` TTTS3018R(실전 전용), `inquire-ccnl` TTTS3035R/VTTS3035R(주문번호 검색 불가) | | 문서 확인 |
| 미국 잔고·주문가능 | `inquire-balance` TTTS3012R/VTTS3012R, `inquire-psamount` TTTS3007R/VTTS3007R(`ord_psbl_frcr_amt`=보유 외화 기준) | | 문서 확인 |
| 미국 시세 | `overseas-price/.../price` HHDFS00000300, 호가 `inquire-asking-price` HHDFS76200100, 일봉 `dailyprice` HHDFS76240000 (거래소코드 주문 NASD/NYSE/AMEX ↔ 시세 NAS/NYS/AMS) | | 문서 확인 |

제약: 클라이언트 주문 ID 없음(응답 유실은 당일 주문조회 유일 매칭), 체결은 누적 수량·금액만(수수료 추정), 소수점 주식 주문·자동 환전은 가정하지 않음(보유 외화 안에서만 주문), 토큰 만료 코드 EGW00123/EGW00121·초당 제한 EGW00201은 관례상 코드로 처리(공식 표 재확인 권장).
KRX 호가 단위: 2023-01-25 시행 표(KIS `aspr_unit`이 있으면 우선). 매도 세금·수수료율은 연도별로 바뀌므로 설정 `execution.paper.kr_sell_tax_rate`를 사용자가 확인.

## Anthropic Claude API (platform.claude.com)

- SDK `anthropic` 1.9.0(httpx2 기반) — 설치본에서 `AsyncAnthropic`, `beta.messages.create(fallbacks=…, output_config=…)`, `messages.count_tokens`, `transform_schema`, `usage.iterations`(모델별 사용량) 시그니처 확인.
- 구조화 출력: `output_config.format = {type: json_schema, schema}` + 코드 측 pydantic·결정적 검증.
- 거절 대비 서버측 폴백: 베타 `server-side-fallback-2026-07-01`, `fallbacks: "default"`(설정 `ai.use_server_fallbacks`). 비용은 `usage.iterations`의 모델별 토큰 × 해당 모델 요율로 정산.
- 요율(2026-09-29 공식 가격표): Opus 5 $5/$25, Opus 4.8 $5/$25, Sonnet 5 $2/$10, Haiku 4.5 $1/$5 (입력/출력, 백만 토큰당). 기본 모델 `claude-opus-5`, effort `medium`.
- 실제 API 호출은 키가 없어 **미검증**(가짜 HTTP로 요청 형식만 확인).

## 기타

| 대상 | 내용 | 상태 |
|---|---|---|
| 환율 | Frankfurter `https://api.frankfurter.dev/v1/latest?base=USD&symbols=KRW` (ECB 기준환율, 무료·키 없음, 영업일 1회 → 주말 3일 이상 지연 정상). 수동 입력 가능. 출처·기준시각·수집시각 기록 | 확인(실호출) |
| 거래 달력 | `exchange_calendars` 4.13.2 XKRX·XNYS (추석·추수감사절·조기폐장·서머타임 테스트) | 확인 |
| 뉴스 | CoinDesk RSS(기본, 무료). 사용자가 피드 추가 가능. 실패는 '자료 없음'으로 명시 | 설정만(리다이렉트 응답 확인) |
| 공시 | DART `opendart.fss.or.kr/api/list.json`(키 필요, 선택) | 미검증 |
| 로컬 모델 | Ollama `/api/chat`(format=JSON 스키마, 선택) | 미검증 |
| 알림 | 텔레그램 봇 `sendMessage`, 일반 웹훅 POST — 본인 수신처 설정 시에만 | 미검증 |
| 키움 / 바이낸스 | 기존 연결 코드가 없어 구현하지 않음. 선물·COIN-M은 초기 LIVE 대상 아님 | 범위 외 |
