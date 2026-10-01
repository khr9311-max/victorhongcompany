# 맥북(Apple Silicon) 상시 운영

> 이 문서의 macOS 전용 절차(launchd·caffeinate·pmset)는 개발 환경(Windows)에서 실행해 보지 못했습니다. 아래 **맥북에서 확인할 절차**를 처음 한 번 그대로 따라 하며 결과를 확인하세요.

## 설치

```bash
brew install python@3.12            # Python 3.12가 없을 때(사용자가 직접 설치)
git clone https://github.com/khr9311-max/victorhongcompany.git
cd victorhongcompany
bash scripts/macos/setup.sh         # .venv(네이티브 arm64) + requirements.lock(해시 고정) + aifund setup
bash scripts/macos/doctor.sh
```

`setup.sh`는 Docker 없이 Apple Silicon 네이티브로 설치하며, 시스템 설정(잠자기·보안·방화벽)을 바꾸지 않습니다.

## 상시 실행(launchd)

```bash
bash scripts/macos/install_launchd.sh --mode internal_paper      # 기본
bash scripts/macos/install_launchd.sh --mode live                 # 실거래 모드(LIVE는 별도로 켜야 실주문)
bash scripts/macos/install_launchd.sh --mode internal_paper --caffeinate   # 선택: 실행 중 유휴 잠자기 방지
launchctl kickstart -k gui/$(id -u)/com.victorhong.aifund       # 재시작
bash scripts/macos/uninstall_launchd.sh                          # 제거(데이터는 유지)
```

- LaunchAgent(`~/Library/LaunchAgents/com.victorhong.aifund.plist`): 로그인 시 시작(`RunAtLoad`), **비정상 종료 시에만 30초 간격 재실행**(`KeepAlive.SuccessfulExit=false`, `ThrottleInterval=30`). `aifund stop`으로 정상 종료하면 다시 뜨지 않습니다.
- 종료 신호(SIGTERM) → 신규 주문 중지 → 진행 중 사이클 대기(최대 60초) → 상태 기록. `ExitTimeOut=90`.
- 재실행 시: DB 무결성 검사 → 미전송/전송중단 주문 정리 → 거래소 대사 → 대사 완료 후에만 신규 주문. 놓친 봉 신호는 실행하지 않음.

## 로그

- 앱 로그: `var/<모드>/logs/aifund.log` (10MB × 7개 자동 순환, 비밀값 마스킹).
- launchd 표준출력/오류: `var/launchd.out.log`, `var/launchd.err.log` (시작 시 20MB 초과면 `.1`로 순환).
- `doctor`가 로그 크기(200MB 초과 경고)·백업 유무를 확인합니다.

## 백업·복구

```bash
bash scripts/macos/backup.sh --mode internal_paper            # 온라인 백업(실행 중 안전), 14개 보관
bash scripts/macos/stop.sh --mode internal_paper
bash scripts/macos/aifund.sh --mode internal_paper restore backups/internal_paper/aifund-YYYYmmdd-HHMMSS.sqlite3 --yes
bash scripts/macos/run.sh --mode internal_paper               # 시작 시 대사 완료 전 신규 주문 차단
```

복구 시 현재 DB는 `var/<모드>/aifund.pre-restore-*.sqlite3`로 보존됩니다. 매일 백업을 원하면 사용자가 `crontab -e`에 `0 5 * * * /bin/bash <경로>/scripts/macos/backup.sh --mode live`를 직접 추가하세요(자동 등록하지 않음).

## 전원·잠자기·뚜껑·재부팅·네트워크

| 상황 | 영향 | 권장 |
|---|---|---|
| 배터리 사용 | 잠자기·네트워크 절전이 더 공격적 | 전원 어댑터 연결 |
| 디스플레이 꺼짐 | 영향 없음(프로세스 계속 실행) | 괜찮음 |
| 시스템 잠자기 | 프로세스·네트워크 정지 → 시세·주문 감시·로컬 손절 중단. 깨어나면 재대사 후 계속 | 사용자가 에너지 설정에서 잠자기를 조정하거나 `--caffeinate` 선택(프로그램이 `pmset`을 바꾸지 않음) |
| 뚜껑 닫힘 | 외부 디스플레이·전원·입력장치가 없으면 잠자기 진입(`caffeinate`로 막히지 않을 수 있음) | 뚜껑을 연 채 운용하거나 클램셸 구성 |
| 재부팅 | LaunchAgent는 **로그인 후에만** 실행. FileVault 켜짐 → 재부팅 후 암호 입력·로그인 전에는 서비스 없음 | 정전·업데이트 후 직접 로그인 필요 |
| 네트워크 끊김 | 조회 실패 기록·경보, 위험 증가 주문 차단. 전송 직후 끊긴 주문은 상태불명으로 두고 조회로 확인 | 복구 후 자동 재대사 |
| 공인 IP 변경 | IP 제한 API 키 인증 실패 → `auth_error` 플래그 → 신규 주문 중지 | 거래소에 새 IP 등록 후 `aifund.sh reconcile` |
| 맥북 전원 꺼짐 | 맥북은 스스로 장애 알림을 보낼 수 없음 | 필요 시 외부 감시(다른 기기에서 원격 확인) 별도 준비 |

24시간 가동을 보장하지 않습니다. 로컬 손절은 거래소 보호 주문이 아니므로 맥북이 멈춘 동안에는 실행되지 않습니다.

## 원격 접속(선택, 기본 비활성)

기본은 `127.0.0.1` 바인딩이며 공유기 포트포워딩을 자동 설정하지 않습니다. 가장 안전한 방법은 포트를 열지 않고 SSH 터널을 쓰는 것입니다.

```bash
# 맥북: 시스템 설정 > 일반 > 공유 > 원격 로그인(사용자가 직접 켬)
# 다른 기기에서:
ssh -N -L 8765:127.0.0.1:8765 <맥북사용자>@<맥북주소>
# 그 기기의 브라우저에서 http://127.0.0.1:8765
```

대시보드를 직접 네트워크에 열어야 한다면: `.env`에 `AIFUND_ALLOW_REMOTE=1`, 32자 이상 `AIFUND_ADMIN_TOKEN`, 설정 `web.host`/`web.allowed_hosts` 지정. 이 경우 조회에도 로그인이 필요하고 상태 변경은 CSRF 토큰이 필요합니다. HTTPS가 없으므로 인터넷에 직접 노출하지 마세요(VPN/터널 권장).

## 맥북에서 확인할 절차

처음 설치 후 한 번 실행하고 결과를 확인하세요(이 절차들은 개발 환경에서 검증되지 않았습니다).

```bash
uname -m                                            # arm64 확인
bash scripts/macos/setup.sh --dev                   # 테스트 도구 포함 설치
PYTHONPATH=src .venv/bin/python -m pytest -q        # 전체 테스트(113개 통과 기대)
# .env는 Git에 없습니다. Windows PC의 .env 값(Gemini·네이버·키움·텔레그램 등)을 안전한 방법으로 옮겨 넣으세요.
bash scripts/macos/doctor.sh                        # ✘ 항목이 없어야 함(키 미설정은 '·' 정보)
bash scripts/macos/aifund.sh kiwoom-check           # 키움 시세 조회 확인(주문 없음, 키움 키를 넣은 경우)
bash scripts/macos/aifund.sh demo --hours 24        # 데모 시뮬레이션
bash scripts/macos/install_launchd.sh --mode internal_paper
launchctl print gui/$(id -u)/com.victorhong.aifund | grep -E "state|pid"   # state = running
curl -s http://127.0.0.1:8765/healthz               # {"status":"running",...}
kill -9 $(launchctl print gui/$(id -u)/com.victorhong.aifund | awk '/pid =/{print $3}')  # 강제 종료 시험
sleep 40; launchctl print gui/$(id -u)/com.victorhong.aifund | grep -E "state|pid"         # 새 pid로 재실행 확인
bash scripts/macos/aifund.sh status                 # startup_reconciled true, 사고(crash_recovery) 기록 확인
bash scripts/macos/stop.sh                          # 정상 종료 → launchd가 다시 띄우지 않는지 확인
pmset -g | grep -E "sleep|displaysleep"             # 현재 잠자기 설정 확인(읽기만)
```
