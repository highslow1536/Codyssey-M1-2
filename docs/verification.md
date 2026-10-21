# 백엔드 1차 구현 검증

검증일: 2026년 10월 21일. Python 3.12.14, 프로젝트 `.venv`와 `uv.lock` 사용.

## 실제 수집과 저장

Yahoo Finance를 yfinance로 실제 호출했다. 합성 시세나 장애 대체 데이터는 사용하지 않았다. 최근 1년의 완료된 거래일을 수집해 **로컬 공식 Firestore 에뮬레이터**의 `market_bars` 및 사용자 `data`에 저장했다.

| 대상 | 실제 관측치 | 기간 |
| --- | ---: | --- |
| KOSPI | 245 | 2025-10-21 ~ 2026-10-21 |
| KOSDAQ | 245 | 2025-10-21 ~ 2026-10-21 |
| 삼성전자 | 245 | 2025-10-21 ~ 2026-10-21 |
| SK하이닉스 | 245 | 2025-10-21 ~ 2026-10-21 |
| S&P 500 | 251 | 2025-10-21 ~ 2026-10-20 |
| NASDAQ Composite | 251 | 2025-10-21 ~ 2026-10-20 |
| VIX | 251 | 2025-10-21 ~ 2026-10-20 |
| AAPL | 251 | 2025-10-21 ~ 2026-10-20 |
| MSFT | 251 | 2025-10-21 ~ 2026-10-20 |
| 합계 | **2,235** | 각 대상 100건 이상 |

`jobs.verify_data --check-reimport`로 저장된 원본을 다시 가져와 9개 대상 모두 추가 생성 0건, 원본 변경 0건을 확인했다. 동일 필터의 전체 요약·출처·고유 날짜 수도 확인했다. 현재 작업 디렉터리의 `artifacts/firestore-verification.json`에 기계 판독용 결과가 있다. 이 폴더는 로컬 점검 산출물이며 버전 관리에서 제외한다.

테스트용 DB는 `demo-stock-assistant`, Firestore 에뮬레이터 v1.19.8, Java 17을 사용했다. 검증 후 임시 서버는 종료했으며 실제 원본 2,235건을 `artifacts/yahoo-all-real-bars.json`에 보관했다. 자신의 개발 환경에서는 수집 CLI를 다시 실행한다. 클라우드 저장 완료를 의미하지 않는다.

## 자동 검사

```bash
FIRESTORE_EMULATOR_HOST=127.0.0.1:8080 .venv/bin/python -m pytest -q
.venv/bin/ruff check app jobs tests
.venv/bin/ruff format --check app jobs tests
.venv/bin/python -m compileall -q app jobs
```

- 단위·통합 검사 **46개 통과**.
- 필수 API 10개 메서드·경로가 OpenAPI에 존재함을 확인했다. `/docs`, `/healthz` 접근도 확인했다.
- 실제 Firestore 트랜잭션으로 CRUD·revision 충돌·전체 필터 요약·페이지 조회·재수집 중복·사용자 수정과 삭제 보존을 검사했다.
- VIX는 일반 주식과 별도로 Cboe 정규장 종료 시각을 적용하고 조기 폐장도 검사했다. 시각 기준은 [Cboe 안내](https://www.cboe.com/about/hours/us-options)를 확인했다.
- 대화 생성·메시지 페이지 조회·소유권·하위 문서 삭제·202 작업 상태·삭제 재개를 검사했다.
- 채팅 저장·완료 응답 재사용·실패 후 단일 사용자 메시지 유지·동시 요청 거부·만료 시도의 덮어쓰기 방지·삭제한 대화의 재생성 거부를 검사했다.
- 모델 도구의 엄격한 인수 스키마·허용 목록·날짜 형식·사용량 상한·모델 실패·실제 데이터 기준 시각 표시를 검사했다. 모델 응답은 테스트 스텁으로 검증했으며 실제 OpenAI 응답 검증으로 계산하지 않았다.
- Firebase 인증 함수를 대체하지 않는 검사에서 Admin SDK가 로컬 에뮬레이터 토큰의 프로젝트·시간·UID를 확인하고 API에 연결하는지 검사했다. 클라우드 Firebase 서명 검증·실제 로그인 제공자와의 연결 검증은 별도다.
- `.env.example`을 설정 모델로 읽었고 빈 비밀값은 자격으로 취급하지 않는지 확인했다. `APP_ENV=production`에서 에뮬레이터 연결을 거부한다.

현재 Starlette TestClient에서 httpx 사용에 관한 지원 중단 예정 경고 1개가 있다. 테스트 실패는 없으며 경고를 숨기는 설정은 추가하지 않았다.

## 아직 실행하지 않은 검증

- 사용자 Firebase 프로젝트의 서비스 계정·권한·클라우드 복합 인덱스 배포 후 조회. 에뮬레이터는 복합 인덱스 존재 여부를 강제하지 않는다.
- `OPENAI_API_KEY`, `OPENAI_MODEL`을 이용한 실제 모델 응답.
- 바닐라 웹 UI, 뉴스·CNN·기술 지표, Discord·MCP, Render·Vercel 배포. 이 기능은 이번 구현 범위 이후다.

실행 방법과 필요한 키는 [README](../README.md)와 [환경변수 예시](../.env.example)에 있다. 비밀 키·웹훅·서버 서비스 계정은 생성하거나 저장하지 않았다.
