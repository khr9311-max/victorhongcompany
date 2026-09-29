# victorhongcompany

## 빅터홍컴퍼니 AI 투자회사

맥북(Apple Silicon M1)에 상시 띄워 두는 **1인 대표 + 전략 봇 + AI 연구/검증** 구조의 소액 실험용 자동매매 서비스입니다.
대표(사용자)는 대시보드에서 현황을 보고, 설정·LIVE 전환·정지를 결정합니다.

> - 실험 원금은 기본 **300,000원 상당**이며 전액 손실 가능성을 전제로 합니다. 추가 원금·차입·레버리지는 지원하지 않습니다(레버리지 1·차입/공매도 없음 고정).
> - 전략 봇 2개(추세·평균회귀)는 **비교 실험용 초기 전략**이며 검증된 수익 전략이 아닙니다. 설정값은 엔지니어링 프리셋입니다.
> - **실제 주문은 기본 OFF**입니다. 짧은 실행무결성 검증과 계좌 연결 확인 후 사용자가 명시적으로 켤 때만 실주문합니다.
> - 24시간 가동을 보장하지 않습니다. 맥북이 꺼지거나 잠자기에 들어가면 로컬 손절·감시도 멈춥니다(아래 운영 절 참고).

---

### 1. 회사 구성

| 역할 | 구현 | 권한 |
|---|---|---|
| 대표(사용자) | 한국어 대시보드 `http://127.0.0.1:8765`, CLI `aifund` | 설정·LIVE·정지·청산·후보 승격 |
| 연구 AI | 매일 08:50(KST) 1회 + 급변 이벤트 시 제한적 추가, 주 1회 전략 검토 | 제안만(주문·쉘·설정 변경 권한 없음) |
| 검증 AI | 같은 원자료를 먼저 독립 평가 → 연구 AI 제안에 반론(근거 오류·상충·미래정보·비용 누락·집중) | 제안만 |
| 봇 A(추세) / 봇 B(평균회귀) | 완성봉 기반 동일 인터페이스, 등록 방식으로 확장 | 목표 비중 신호만 |
| 포트폴리오 조정기 | 상충 제안을 내부 상계 → 순주문, 전략별 가상 원장 | - |
| 위험관리 | 결정적 규칙(한도·데이터 신선도·손실 정지 등) | AI가 바꿀 수 없음 |
| 중앙 주문 실행기 | 원자적 자금 예약, 멱등 키, 응답 유실·부분체결·취소 처리, 재시작 대사 | 유일한 주문 경로 |

흐름: **데이터 스냅샷 → 전략별 독립 제안 → (B/C) AI 검토 → 포트폴리오 조정 → 위험 검사 → 중앙 주문 실행 → 체결·성과 기록**.
자세한 구조는 [docs/architecture.md](docs/architecture.md).

### 2. 모드(모드마다 DB·원장·캐시·로그·토큰 캐시가 `var/<모드>/`로 분리)

| 모드 | 데이터 | 주문 | 용도 |
|---|---|---|---|
| `offline_demo` | 합성(가짜) 데이터, 화면에 '데모' 표시 | 내부 모의체결 | 키 없이 구조 확인 |
| `internal_paper` (**기본**) | 실제 공개 시세(업비트) 또는 명시적 재생 데이터 | 내부 모의체결(수수료·스프레드·불리한 체결·부분체결 반영) | 실험 운용 |
| `broker_sandbox` | 증권사 공식 모의투자(KIS만) | KIS 모의 | 공식 모의 |
| `live` | 실제 시세 | **LIVE를 켠 시장만** 실제 계좌 | 실거래 |

기본 활성 시장은 **코인(업비트 공개 시세 기반 internal_paper)** 1개, 전략 봇 2개입니다. 국내·미국 주식(KIS) 모듈은 구현되어 있으나 키가 없으면 '미연결'로 표시됩니다.

### 3. 맥북에서 설치·시작·중지 (그대로 실행)

```bash
# 0) Python 3.12 (없으면): brew install python@3.12
# 1) 코드 받기
git clone https://github.com/khr9311-max/victorhongcompany.git
cd victorhongcompany
# 2) 설치(가상환경 + 해시 고정 의존성 + .env/관리자 토큰 생성 + DB 초기화 + 자체검증)
bash scripts/macos/setup.sh
# 3) 점검
bash scripts/macos/doctor.sh
# 4-a) 포그라운드 실행(터미널 창을 닫으면 멈춤)
bash scripts/macos/run.sh
# 4-b) 상시 실행(launchd, 로그인한 동안 유지·비정상 종료 시 자동 재실행)
bash scripts/macos/install_launchd.sh --mode internal_paper
# 상태 / 정상 종료 / 백업
bash scripts/macos/status.sh
bash scripts/macos/stop.sh
bash scripts/macos/backup.sh
# 상시 실행 해제
bash scripts/macos/uninstall_launchd.sh
```

대시보드: `http://127.0.0.1:8765` (조회는 로그인 없이, 제어·설정 변경은 `.env`의 `AIFUND_ADMIN_TOKEN`으로 로그인).
키 없이 전체 흐름을 보려면: `bash scripts/macos/aifund.sh demo --hours 72` 후 `bash scripts/macos/run.sh --mode offline_demo`.

Windows(개발 PC)에서는 `.\scripts\windows\dev.ps1 setup`, `.\scripts\windows\dev.ps1 test`, `.\scripts\windows\dev.ps1 run --mode offline_demo`.

### 4. 주요 명령 (`scripts/macos/aifund.sh <명령>` = `python -m aifund <명령>`)

| 명령 | 설명 |
|---|---|
| `setup` / `doctor` | 초기 설정 / 환경·연결·DB·launchd 점검 |
| `run [--mode M] [--no-web] [--replay 파일]` | 서비스 실행(단일 프로세스 잠금) |
| `status` / `stop` | 상태(하트비트·자산·플래그·미체결) / 정상 종료 |
| `backup [--keep 14]` / `restore <파일> --yes` | 온라인 백업 / 서비스 정지 상태에서 복구 |
| `selftest` | 실행무결성 자체검증(수 초, 실주문 없음) — LIVE 전제조건 |
| `demo --hours N` | 오프라인 데모 시뮬레이션 |
| `halt all\|crypto [--reason]` / `resume ...` | 신규 매수 중지 / 재개(재시작해도 유지) |
| `cancel-open [--market]` | 봇 미체결 주문만 취소 |
| `liquidate <시장> [--confirm "청산 <시장>"]` | 미리보기 → 확인 후 봇 보유분만 지정가 청산 |
| `reconcile` | 거래소 대사 |
| `live status\|check\|enable\|disable\|baseline <시장>` | LIVE 관리 |
| `settings show\|history\|set k=v\|import 파일` | 설정(버전 기록) |
| `report` | A/B/C·기준선 비교 |
| `backtest --market crypto --days 60 --split YYYY-MM-DD --fetch` | 규칙 전략 백테스트(개발/평가 구간 분리) |
| `candidates list\|add\|backtest\|promote\|rollback` | 전략 개선 후보 |
| `ai status` / `ai research <시장>` | AI 상태·예산 / 수동 연구(예산 차감) |

### 5. 계좌·API 키 설정 위치

모든 비밀은 프로젝트 루트의 **`.env`** 한 곳에만 둡니다(Git 제외, 권한 600, 화면·로그·AI 입력에 표시 안 됨). 항목은 [.env.example](.env.example).

- **업비트(코인 실거래)**: `UPBIT_LIVE_ACCESS_KEY`, `UPBIT_LIVE_SECRET_KEY` — live 모드에서만 읽힘.
  권장: 업비트 앱/웹에서 **봇 전용 서브포켓**을 만들고 실험 원금만 이전한 뒤, 그 포켓의 API 키를 **[자산조회][주문조회][주문하기]** 권한으로만 발급(출금 권한 금지, 서브포켓은 외부 출금 자체가 제한). 키는 등록한 IP에서만 동작합니다.
- **KIS(국내·미국 주식)**: 실거래 `KIS_LIVE_*`, 공식 모의 `KIS_SANDBOX_*`, 시세 전용 `KIS_DATA_*`.
- **Claude API(선택)**: `ANTHROPIC_API_KEY`. 없으면 AI 없이 규칙 전략만 동작(`ai.when_unavailable` 설정대로).
- 설정값(한도·종목·주기·모델)은 `config/config.toml` 또는 대시보드 **설정** 화면. 모든 변경은 버전으로 기록되며 AI는 바꿀 수 없습니다.

### 6. LIVE 활성화·정지·복구 절차 (상세: [docs/live-trading.md](docs/live-trading.md))

1. `.env`에 해당 거래소 키 입력(키만으로는 절대 실주문하지 않음).
2. `bash scripts/macos/aifund.sh selftest` (24시간 이내 통과 필요).
3. live 모드로 서비스 실행: `bash scripts/macos/install_launchd.sh --mode live` (또는 `run.sh --mode live`).
4. 사전 점검: `aifund.sh --mode live live check crypto --ack-no-withdraw` — 모드, 키, 계좌 인증, 상품 메타·최소주문, 시계 오차, 주문 가능 금액, **거래소 주문 검증 API(실제 주문 없음)**, 외부 미체결 주문, 대사, 상태불명 주문을 확인.
5. 활성화: `aifund.sh --mode live live enable crypto --ack-no-withdraw --confirm "LIVE crypto upbit-main"` (대시보드 **제어**에서도 가능).
   활성화 시점의 기존 보유분은 '기존 보유분'으로 기록되어 봇이 매도하지 않습니다. 이후 범위 안 주문은 건별 승인 없이 실행됩니다.
6. 한도·종목·계좌·운용 설정이 바뀌면 그 시장 LIVE는 자동으로 '재확인 필요'가 되어 실주문이 멈춥니다(5번 반복).
- **정지**: 신규 매수 중지 `halt crypto` / 미체결 취소 `cancel-open --market crypto` / 보유분 청산 `liquidate crypto --confirm "청산 crypto"` / LIVE 끄기 `live disable crypto`. 각 동작은 설명된 일만 하며, 정지는 재시작해도 유지됩니다.
- **복구**: 재시작 시 자동으로 미전송/전송중단 주문을 정리하고 거래소와 대사한 뒤에만 신규 주문을 허용합니다. 대사 불일치(`recon_block`)가 뜨면 화면의 해결 안내를 따르고 `aifund.sh reconcile`로 재확인.

### 7. 맥북 상시 운용 시 알아둘 점 (상세: [docs/operations-macos.md](docs/operations-macos.md))

- **전원**: 어댑터를 꽂아 두세요. 배터리 모드에서는 잠자기·네트워크 절전이 더 공격적입니다.
- **디스플레이 꺼짐 ≠ 잠자기**: 화면만 꺼지는 것은 괜찮습니다. **시스템 잠자기**에 들어가면 프로세스·네트워크가 멈춰 시세·주문 감시·로컬 손절이 모두 중단됩니다. 이 프로그램은 잠자기 설정을 바꾸지 않습니다. 필요하면 사용자가 직접 `시스템 설정 > 배터리/에너지`에서 조정하거나, `install_launchd.sh --caffeinate`(서비스 실행 중에만 유휴 잠자기 방지, 설정 변경 없음)를 선택하세요.
- **뚜껑 닫힘**: 외부 모니터·전원·키보드가 연결된 클램셸 상태가 아니면 뚜껑을 닫는 순간 잠자기에 들어갑니다(`caffeinate`로도 막히지 않을 수 있음).
- **재부팅·로그인**: LaunchAgent는 사용자가 **로그인한 뒤에만** 실행됩니다. FileVault가 켜져 있으면 재부팅 후 디스크 잠금 해제·로그인 전에는 서비스가 뜨지 않습니다.
- **네트워크 끊김**: 조회 실패는 기록·경보되고 위험 증가 주문이 차단됩니다. 전송 직후 끊긴 주문은 실패로 단정하지 않고 거래소 조회로 확인될 때까지 해당 계좌의 신규 위험 주문을 막습니다.
- **공인 IP 변경**: 업비트·KIS 키가 IP 제한이면 집 공인 IP가 바뀔 때 인증 오류가 나고 신규 주문이 멈춥니다. 거래소에서 새 IP를 등록하세요.
- **외부 감시 한계**: 맥북 전체가 꺼지면 맥북 스스로는 장애 알림을 보낼 수 없습니다. 필요하면 별도 외부 감시(예: 휴대폰에서 주기적으로 대시보드 원격 확인 — 원격 접속은 선택 기능)를 두세요.
- 로컬 손절(평균회귀 봇의 손절)은 **거래소 보호 주문이 아닙니다**(업비트·KIS 서버측 손절 API는 이 구현에서 사용하지 않음). 맥북·네트워크가 꺼지면 동작하지 않습니다.

### 8. AI 비용

- 기본 월 AI 예산 상한 **5,000원**(설정 변경 가능). 호출 전 `count_tokens`로 입력 토큰을 세고 `최대 출력 토큰 × 요율`까지 **예상 최대 비용을 예약**한 뒤, 호출 후 실제 사용량(모델별)으로 정산합니다. 타임아웃·결과 불명 호출은 예약액 전액을 사용한 것으로 보수 정산합니다.
- 요율은 공식 가격표(2026-09-29 확인)를 설정 표에 기록해 두었고, **표에 없는 모델은 유료 자동 호출을 하지 않습니다**. 환율이 불확실하면 대체 환율로 계산하고 '추정'으로 표시합니다.
- 기본 모델은 `claude-opus-5`(입력 $5 / 출력 $25 per 백만 토큰). 5,000원 예산이면 연구+검증 호출이 **하루 1세트 수준도 빠듯할 수 있습니다**. 호출 횟수를 늘리고 싶다면 설정에서 `claude-sonnet-5`($2/$10) 또는 `claude-haiku-4-5`($1/$5)로 바꾸는 것은 대표의 선택입니다.
- AI 운영비는 투자원금과 별도 항목으로 표시하고 실험 전체 손익(대시보드 '실험 전체 손익')에는 포함합니다.

### 9. 검증 결과 (이 개발 환경: Windows 11, Python 3.12.7)

실행해서 확인한 것:
- 자동 테스트 **69개 통과**(`python -m pytest`): 요구 검증 1~10 전 항목 + 업비트/KIS 계약 테스트 + Claude 요청 형식 + AI 예산·검증.
- 실행무결성 자체검증 7개 시나리오 통과(정상 체결, 응답 유실 후 재주문 0회, 부분체결·취소 중 체결, 중복·역전 이벤트, 동시 예약 한도, 재시작 복구, LIVE 미활성 시 실주문 어댑터 호출 0회).
- `internal_paper` 서비스를 실제 업비트 공개 시세로 실행: 대사 완료 후 주문 허용, 수동 사이클·모의 체결(불리한 슬리피지·수수료 반영), 대시보드 전 화면 HTTP 200, 로그인·CSRF·Host 검사, `aifund stop` 정상 종료.
- 실제 업비트 45일 캔들로 백테스트(개발/평가 구간 분리), 환율(Frankfurter) 수집, 시계 오차 측정(+0.7초).

**미검증(이 환경에서 확인 불가 — 실행 결과와 구분):**
- macOS 전용: `scripts/macos/*.sh` 실제 실행, `launchctl` 등록·재시작, `caffeinate`, `pmset` 읽기. (셸 문법 검사와 plist 렌더링 검증만 함) → [docs/operations-macos.md의 맥 확인 절차](docs/operations-macos.md#맥북에서-확인할-절차)
- 업비트 인증 API(잔고·주문·주문검증·취소): 키가 없어 **계약 테스트(가짜 HTTP)만** 통과. 실제 연결 확인 아님.
- KIS 국내·미국(시세·주문·모의투자): 키가 없어 계약 테스트만. 특히 모의 미국 매도 TR ID(`VTTT1001U`)는 공식 예제 코드와 주석이 서로 달라 **미확정**.
- Claude API 실제 호출(키 없음), Ollama, 텔레그램·웹훅 알림, DART.
- 브라우저로 직접 화면을 본 것은 아님(HTTP 요청·HTML 렌더링으로 확인).

### 10. 아직 연결되지 않은 기능과 사용자가 할 최소 작업

| 기능 | 상태 | 사용자가 할 일 |
|---|---|---|
| 코인 실거래(업비트) | 코드 완료, 미연결 | 서브포켓 + 권한 제한 키 발급·IP 등록 → `.env` → `live check`/`enable` |
| 국내·미국 주식(KIS) | 코드 완료, 미연결 | KIS Developers 앱키·계좌 → `.env`의 `KIS_*` → 설정에서 시장 `enabled`·`allocation_krw` 지정 → `broker_sandbox`로 먼저 확인 권장 |
| 연구·검증 AI | 코드 완료, 키 없음 | `ANTHROPIC_API_KEY` 입력(유료). 비용 상한은 설정 |
| 외부 알림 | 코드 완료, 비활성 | 본인 텔레그램 봇 토큰·chat id 또는 웹훅 URL을 `.env`에 넣고 설정에서 켜기 |
| 공시(DART) | 코드 완료, 비활성 | DART 키 + 설정 `news.dart_enabled` |
| 바이낸스·키움·선물 | 범위 외 | 기존 코드가 없어 구현하지 않음(선물·COIN-M은 초기 LIVE 대상 아님) |

문서: [아키텍처](docs/architecture.md) · [연결 범위·확인일](docs/integrations.md) · [맥북 운영](docs/operations-macos.md) · [LIVE 절차](docs/live-trading.md)
