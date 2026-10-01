# 키움 API 조회 연결

기존 키움봇에서 API 요청 형식만 참고하여 현재 프로젝트의 비동기 조회 모듈로 구현했습니다. 전략·주문·스케줄러·서버 배포는 이식하지 않았습니다. 별도 SDK나 Windows COM 없이 Windows/macOS에서 같은 REST 코드를 사용합니다.

## 사용 범위

- OAuth 인증: 토큰은 메모리에서 재사용하고 만료 시 갱신합니다. HTTP 401 및 응답 본문의 토큰 오류는 한 번 재인증합니다.
- `ka10007`: 현재가와 매수·매도 1호가. 가격 앞의 상승·하락 부호는 제거합니다.
- `ka10081`: 국내주식 일봉. UTC 시각과 KRX 거래 달력을 사용합니다.
- `kt00001`, `kt00018`: 예수금과 보유종목 조회. 계좌 자료는 모의 원장에 합산하지 않습니다.
- 연속조회가 불완전하면 오류 처리합니다. 국내·미국 조회는 토큰을 공유하며 요청 간격을 앱키·환경별 최소 2초로 제한합니다. 429 응답은 최대 2회 재시도하고, 그래도 실패하면 그 주기의 해당 시장 시세만 '실패'로 표시하고 다음 주기에 다시 조회합니다(다른 시장은 계속). mock 서버에서 429가 반복된 기록이 있으니 대시보드 오류 표시를 확인하세요.

허용 목록 밖의 API는 전송 전에 차단됩니다. 키 자체가 조회 전용 권한이라는 뜻은 아니며, 이 프로젝트 클라이언트가 주문 API를 제공하지 않는 구조입니다. 기존 키움봇은 수정하지 않았고 공유 폴더나 원격 서버에 의존하지 않습니다.

## 설정과 점검

`.env`의 `KIWOOM_DATA_ENV`는 `mock` 또는 `real`(비워 두면 `mock`), 인증 정보는 `KIWOOM_DATA_APP_KEY`와 `KIWOOM_DATA_APP_SECRET`입니다. 환경을 바꾸면 반드시 그 환경에 맞는 키도 변경해야 합니다. 키가 있는데 환경 값이 `mock`/`real`이 아니면 시작 시 오류로 멈춥니다. `offline_demo`에서는 키를 읽지 않습니다.

이번 연결은 기존 폴더의 **mock 키**를 사용했습니다. 인증·현재가·일봉·계좌 조회는 실제 모의 API로 검증했으며 실전 API와 macOS 실기동은 미검증입니다.

점검(주문 없음, 키·토큰은 출력하지 않음):

```powershell
.\scripts\windows\dev.ps1 kiwoom-check                              # 삼성전자 현재가·일봉
.\scripts\windows\dev.ps1 kiwoom-check --symbol 000660 --account    # + 국내 예수금·보유종목
.\scripts\windows\dev.ps1 kiwoom-check --symbol NASD:NVDA           # 미국주식(계좌 조회는 미지원)
```

macOS는 `bash scripts/macos/aifund.sh kiwoom-check` 형식으로 같은 인자를 씁니다.

`config/paper.toml`은 국내·미국주식을 모두 활성화하고 `data_provider = "kiwoom"`, `broker = "paper"`를 사용합니다. 양 시장 모두 일봉(`candle = "1d"`)만 지원하며, 다른 봉이나 코인에 `kiwoom`을 지정하면 설정 저장이 거부됩니다. LIVE용 `config/config.toml`은 별도 설정입니다.

다른 모드의 DB에서 바꾸려면 대시보드 설정의 '시세 공급자'(국내·미국주식) 또는 `aifund.sh settings set markets.kr_stock.data_provider=kiwoom`으로 변경한 뒤 서비스를 재시작합니다. 시세 공급자 선택은 KIS 주문 브로커를 키움 주문 브로커로 바꾸지 않습니다. `data_provider`는 LIVE 활성화 범위에 포함되어, 바꾸면 그 시장의 LIVE는 '재확인 필요'가 됩니다. live 모드는 `KIWOOM_DATA_ENV=mock` 시세를 쓰지 않습니다(시장 '미연결'). 모의 조회 자료를 실거래 판단에 사용하지 마세요.

공식 API 근거: [키움증권 REST API 저장소](https://github.com/Kiwoom-Securities/Kiwoom-REST-API). 로컬 `trading_bot/core/kiwoom_client.py`의 요청 형식도 대조했습니다.

## 미국주식 내부 모의운용

- 공식 `usa20101` API로 USD 현재가·매수/매도 호가·잔량을 조회합니다.
- 공식 `usa06012` API로 수정 일봉을 조회합니다. 환율 적용은 끄고 USD로 보관합니다. 미국 거래 달력의 정규장 마감 시각을 기준으로 완성봉을 판단합니다.
- 거래소 매핑: NASD → ND, NYSE → NY, AMEX → NA. 기존 보유분 식별자는 유지됩니다.
- 호가 원시 시각의 시간대가 확인되지 않아 현재는 수집시각으로 신선도를 판단합니다. 공급자의 지연 시세 여부를 독립적으로 보장하지는 않습니다.
- 기존 mock 키로 NVDA·MSFT 호가와 각 120개 일봉 응답을 확인했습니다(2026-09-29). 실전 서버 연결은 별도 확인이 필요합니다.
- 미국 계좌 조회·실주문·송금 API는 추가하지 않았습니다. 주문은 내부 가상 체결이며, 달러는 미국 사이클에서 모의 환전으로 마련합니다([멀티시장 모의운용](multi-market-paper.md#자금과-기록)).
- 2026-09-29 `internal_paper` 실행에서 이 시세로 미국주식 사이클(일봉 판단 → 모의 환전 → 모의 체결)이 완료된 기록이 있습니다.

공식 근거: [키움 REST API 스펙](https://github.com/Kiwoom-Securities/Kiwoom-REST-API/blob/main/kiwoom/_data/kiwoom_api_spec.json).
