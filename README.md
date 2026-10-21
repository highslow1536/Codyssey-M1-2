# 한국·미국 주식시장 AI 비서

Yahoo의 실제 일봉을 Firestore에 저장하고, 저장한 데이터 요약으로 답하는 AI 비서를 개발합니다. 전체 설계는 [프로젝트 설계서](docs/project-design.md)에 있습니다.

현재 구현은 **백엔드·과제 필수 API와 바닐라 웹 UI**입니다. Naver/Finnhub 뉴스, CNN 심리 지표, Discord 예약·알림, MCP 서버는 다음 단계입니다. 해당 기능의 키 이름만 미리 준비했으며, 아직 동작하는 기능으로 표시하지 않습니다.

## 구현 범위

- Python 가상환경, 버전 잠금, FastAPI·Swagger·CORS·요청 ID·오류 응답.
- Firebase ID token 검증과 허용 UID 제한. 미인증 요청은 401, 타인 데이터는 404.
- 한국·미국 9개 종목·지수의 실제 완료 일봉 수집. 거래일·조기 폐장·UTC 기준 시각, 원래 종가와 조정 종가 보존.
- `instruments`·`market_bars` 원본과 수정 가능한 사용자 `data` 분리, 중복 방지·revision 충돌 검사.
- 데이터 CRUD·전체 필터 요약, 대화 저장·목록·메시지 페이지 조회·하위 문서 삭제.
- 서버 요약을 주입하는 OpenAI Responses 채팅, 자동 저장·재시도·동시 요청 제한, `summarize_dataset` 도구.
- 프레임워크 없는 HTML/CSS/JS 화면: 이메일·Google Firebase 로그인, 실제 Yahoo 수집, CRUD·기간 전체 요약·목록 차트, AI 채팅과 대화 기록 복원, 모바일 화면.

장중 수집·기술 지표·가격 알림은 아직 구현 전입니다. 일봉의 `event_time`은 거래소 세션 종료 시각이고 `fetched_at`은 공급원 수집 시각입니다. 데이터가 없거나 외부 호출이 실패하면 가짜 값으로 대체하지 않습니다.

## 개발 환경

Python **3.12**를 사용합니다. 설계의 Python 3.11 기준을 충족하는 상위 버전이며, 현재 작업 환경에서 제공되는 3.12.14로 검증했습니다. 지원 범위는 3.11~3.13입니다. `uv.lock`은 개발 환경, `requirements.lock`은 Render 실행 의존성을 고정합니다.

```bash
uv sync --locked --extra dev
cp .env.example .env
```

`uv` 없이 실행할 때:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
```

개발 검사에는 `uv sync --locked --extra dev`로 pytest·ruff도 설치하세요. 이후 명령은 프로젝트 루트에서 실행합니다.

## Firebase 설정

1. Firebase Console에서 프로젝트와 **Firestore Native mode `(default)` DB**를 생성합니다.
2. Authentication에서 사용할 로그인 제공자를 활성화하고 테스트 사용자를 준비합니다.
3. 서버용 서비스 계정 JSON을 받아 저장소 밖에 보관하고, `.env`의 `GOOGLE_APPLICATION_CREDENTIALS`에 파일 경로를 입력합니다. Render에서는 대신 `FIREBASE_SERVICE_ACCOUNT_JSON`에 한 줄 JSON을 설정할 수 있습니다. 둘 중 하나만 사용합니다.
4. `.env`에 `FIREBASE_PROJECT_ID`, `ALLOWED_USER_UIDS`, `JOB_OWNER_UID`를 입력합니다. 두 UID에는 Authentication의 동일 사용자 UID를 넣습니다. 허용 목록을 비워 두면 개인 API는 접근을 허용하지 않습니다.
5. Firebase CLI를 사용한다면 인덱스와 규칙을 배포합니다. 아래 명령은 지정한 클라우드 프로젝트 설정을 변경하므로 프로젝트 ID를 확인하고 직접 실행하세요.

```bash
firebase deploy --only firestore:rules,firestore:indexes --project YOUR_PROJECT_ID
```

브라우저의 Firestore 직접 접근은 `firestore.rules`로 차단합니다. 서버의 Admin SDK는 규칙을 우회하므로 모든 개인 API에서 UID·소유권을 확인합니다. 인덱스 빌드가 끝난 뒤 조회하세요. 에뮬레이터는 복합 인덱스 존재를 강제하지 않으므로 클라우드에서도 확인해야 합니다.

## 실제 데이터 수집

Yahoo에는 키가 필요하지 않습니다. 기본은 9개 대상 각각 최근 1년, 최소 100개 실제 일봉이며, 최소 수에 미달하면 해당 대상은 저장하지 않고 실패를 보고합니다. 실행 중 다른 종목이 실패해도 성공한 종목은 저장되며 종료 코드는 1입니다. 재실행으로 이어서 수집할 수 있습니다.

```bash
.venv/bin/python -m jobs.bootstrap_data
# 두 시장의 대표 종목만 먼저 저장
.venv/bin/python -m jobs.bootstrap_data --instruments KRX_005930 NASDAQ_AAPL
# 명시적 기간: 양 끝 날짜 포함
.venv/bin/python -m jobs.bootstrap_data --instruments INDEX_SP500 --start 2025-10-01 --end 2026-10-01
# Firebase 설정 전에도 가능한 실제 공급원 수집 점검; DB에는 쓰지 않음
.venv/bin/python -m jobs.bootstrap_data --fetch-only --instruments KRX_005930 NASDAQ_AAPL --output artifacts/yahoo-real.json
# 저장 후 전체 건수·출처 확인과 저장된 원본 재가져오기 중복 검사
.venv/bin/python -m jobs.verify_data --check-reimport --output artifacts/verification.json
```

지원 ID: `INDEX_KOSPI`, `INDEX_KOSDAQ`, `INDEX_SP500`, `INDEX_NASDAQ`, `INDEX_VIX`, `KRX_005930`, `KRX_000660`, `NASDAQ_AAPL`, `NASDAQ_MSFT`. 기간은 최대 730일입니다. 당일 아직 끝나지 않은 세션은 저장하지 않습니다.

`data.value`는 수집 당시 조정 종가를 복사합니다. 사용자 수정은 재수집에서 보존합니다. 삭제는 원본과 분리된 사용자 문서에 삭제 표식을 남겨 재수집으로 되살아나지 않도록 합니다. 원본 정정 시 수정하지 않은 복사본만 갱신합니다. 개별 종목 저장은 최대 100개 관측치씩 트랜잭션으로 처리하므로 중간 실패 뒤 재실행해도 중복되지 않습니다.

## API 실행과 인증

```bash
.venv/bin/uvicorn app.main:app --reload --port 8000
```

- 상태 확인: `http://localhost:8000/healthz` (프로세스 생존 확인).
- API 문서: `http://localhost:8000/docs`.
- 나머지 API: `Authorization: Bearer <Firebase ID token>` 필요. API 키·서비스 계정 JSON·Firebase custom token을 이 자리에 넣지 마세요.

웹 화면은 Firebase SDK의 `user.getIdToken()`으로 받은 ID token을 API에 전달합니다. Swagger에서는 같은 토큰을 **Authorize**에 입력할 수 있습니다. `ALLOWED_ORIGINS`에는 사용할 프런트 출처를 쉼표로 구분해 설정합니다.

| 메서드 | 경로 | 동작 |
| --- | --- | --- |
| POST / GET | `/api/data` | 분석 데이터 추가 / 종목별 목록 |
| GET | `/api/data/summary` | 필터 전체의 기간·건수·평균·최대·최소·추세·출처 |
| PUT / DELETE | `/api/data/{id}` | 분석 데이터 수정 / 삭제 |
| POST | `/api/data/import` | 한 종목 실제 일봉 수집·저장 |
| POST / GET | `/api/conversations` | 대화 저장 / 목록 |
| GET / DELETE | `/api/conversations/{id}` | 메시지 조회 / 하위 문서 포함 삭제 |
| POST | `/api/chat` | 요약 기반 답변·자동 대화 저장 |
| GET | `/api/jobs/{id}` | 대화 삭제 작업 상태 조회 |
| GET | `/api/instruments` | 지원 종목·지수 검색 |
| GET | `/api/market/{instrument_id}/history` | 저장된 공급원 원본 일봉 조회 |

데이터 목록·요약 기본 종목은 `INDEX_SP500`입니다. `instrument_id`, `start_date`, `end_date`로 한 종목·최대 730일을 조회합니다. 기본 기간은 최근 365일입니다. 통화를 섞어 평균내지 않습니다. 목록의 `limit`은 기본 50, 최대 200이고 `next_cursor`를 다음 요청의 `cursor`로 전달합니다. 요약은 첫 페이지에 한정하지 않으며 5,000개 초과 시 기간 축소를 요청합니다. 평균 등 빈 데이터의 수치는 `null`입니다. 추세는 직전 20개 관측치 대비 변화가 ±1%를 초과하는지로 구분하며, 거래일 결측 통계는 다음 분석 단계에 구현합니다.

데이터 추가 예:

```json
{"instrument_id":"NASDAQ_AAPL","date":"2025-10-21","value":261.8028869628906,"memo":"관측값 확인","idempotency_key":"aapl-note-20251021"}
```

수정에는 `date`, `value`, `memo`, `expected_revision`을 모두 전달합니다. 최신 `revision`과 다르면 409입니다. 수동 입력은 `source=manual`로 표시해 공급원에서 직접 수집한 관측치와 구분합니다.

채팅 예 (`client_request_id`는 질문마다 새 UUID):

```json
{"message":"최근 저장된 삼성전자 데이터의 추세를 설명해줘","instrument_id":"KRX_005930","client_request_id":"3039876e-4a35-4a57-8860-75c982cceaba"}
```

후속 질문에는 반환된 `conversation_id`를 추가합니다. 같은 질문을 재전송할 때는 같은 요청 ID와 본문을 사용합니다. 완료된 응답은 다시 반환하며 모델을 재호출하지 않습니다. 모델 실패 시 사용자 메시지와 실패 상태만 남깁니다. 외부 모델 호출과 DB 저장을 하나의 원자적 작업으로 묶을 수 없으므로 네트워크 장애 중 모델 과금 중복까지 보장하지는 않습니다.

`OPENAI_API_KEY`, `OPENAI_MODEL`이 비어 있으면 채팅만 503입니다. 사용할 계정의 모델 ID를 지정하세요. `summarize_dataset` 도구는 웹과 같은 `DataService`를 사용하고 소유 UID를 서버에서 주입합니다. 임의 코드 실행·외부 전송 도구는 노출하지 않습니다.

출력 토큰 기본 상한 1,200은 요청 안의 모든 모델 왕복 합계입니다. 도구 결과를 처리할 토큰이 부족하면 오류로 알리고 가짜 답변을 저장하지 않습니다. 대화 삭제 뒤에는 같은 생성/채팅 요청 ID를 재사용하지 말고 새 UUID를 사용하세요.

대화 삭제는 보통 204입니다. 매우 큰 대화는 삭제 표식과 영속 작업을 저장한 뒤 202와 `job_id`, `status_url`을 반환합니다. 같은 DELETE를 다시 호출하거나 `.venv/bin/python -m jobs.cleanup_conversations`로 정리를 이어갈 수 있습니다. 예약 워커가 아직 없으므로 202 작업은 이 방법으로 재개합니다. 삭제 완료 후 부모와 메시지·turn 문서가 없어지고, 삭제 이력만 `job_runs`에 남습니다.

## 검사

```bash
.venv/bin/python -m pytest tests/unit -q
.venv/bin/ruff check app jobs tests
```

Firestore 통합 검사는 **로컬 에뮬레이터만** 사용합니다. 클라우드 DB에서 테스트 데이터를 생성하지 않습니다. Firebase CLI와 지원되는 Java를 설치한 환경에서:

```bash
firebase emulators:start --only firestore,auth --project demo-stock-assistant
# 다른 터미널에서
FIRESTORE_EMULATOR_HOST=127.0.0.1:8080 .venv/bin/python -m pytest -q
```

에뮬레이터에서 실제 수집 CLI를 실행할 때는 다음을 `.env`에 설정합니다. `APP_ENV=production`에서는 에뮬레이터 연결을 거부합니다.

```dotenv
APP_ENV=development
FIREBASE_PROJECT_ID=demo-stock-assistant
FIRESTORE_EMULATOR_HOST=127.0.0.1:8080
FIREBASE_AUTH_EMULATOR_HOST=127.0.0.1:9099
ALLOWED_USER_UIDS=YOUR_EMULATOR_AUTH_UID
JOB_OWNER_UID=YOUR_EMULATOR_AUTH_UID
```

경계값 테스트의 작은 입력과 모델 스텁은 테스트 전용입니다. 서비스 실행 시 합성 데이터·가짜 답변 대체 경로는 없습니다. `tests/fixtures/yahoo_aapl_daily.json`은 실제 Yahoo 응답의 일부이며 수집 시각·URL을 기록했습니다. 실제 OpenAI 호출은 키를 설정한 뒤 별도로 점검해야 합니다.

## 웹 화면 실행

Node **22 이상**으로 정적 파일을 빌드합니다. 실행 화면은 HTML/CSS/ES modules만 사용하며 Firebase SDK는 고정된 버전의 공식 CDN에서 가져옵니다. Playwright는 개발 검사 전용입니다.

1. Firebase Console의 Authentication에서 **이메일/비밀번호**를 활성화하고 사용자를 등록합니다. Google 로그인도 사용하려면 해당 제공자를 활성화합니다. 웹에 회원가입 기능은 없으며 허용 UID는 서버에서 관리합니다.
2. 프로젝트 설정 → 일반에서 웹 앱을 추가하고 공개 `firebaseConfig` 객체를 **JSON 한 줄**로 `.env`의 `FIREBASE_WEB_CONFIG_JSON`에 입력합니다. 서비스 계정 JSON과 다른 설정입니다.
3. Authentication → 설정 → 승인된 도메인에 `localhost`와 실제 Vercel 도메인을 등록합니다. 서버의 `ALLOWED_ORIGINS`에도 프런트의 정확한 출처를 추가합니다.
4. `.env`에 로컬 `API_BASE_URL=http://localhost:8000`과 서버 설정을 입력하고 API를 실행합니다.

```bash
# 터미널 1: 프로젝트 루트
.venv/bin/uvicorn app.main:app --reload --port 8000
# 터미널 2: 프로젝트 루트. 정적 빌드는 패키지 설치 없이 가능
node frontend/scripts/build.mjs
python3 -m http.server 5173 --directory frontend/dist
```

`http://localhost:5173`을 엽니다. 이메일/비밀번호 또는 Google로 로그인합니다. 로그인 상태는 탭 세션에 보관하며 새로고침 때 복원됩니다. 로그아웃 시 개인 화면을 지우고 진행 중인 요청을 취소합니다. 설정을 바꾼 뒤에는 다시 빌드하세요. 공개 설정이 없으면 로그인을 비활성화하고 오류를 표시합니다.

- **데이터 워크스페이스**: 종목·날짜 조회, Yahoo 실제 수집, 직접 추가·수정·삭제, 더 불러오기. 수정 시 revision 충돌을 서버에서 검사합니다. 추가 요청을 그대로 재시도하면 같은 idempotency key를 사용합니다.
- **전체 요약과 차트**: 요약은 선택 기간의 모든 기록으로 계산하고, 차트는 현재 불러온 목록만 표시합니다. 수집·직접 입력·수정 기록의 출처를 구분합니다. 빈 데이터는 `—`로 표시합니다.
- **AI 리서치 대화**: 선택 종목의 **최근 1년** 분석 데이터를 사용합니다. 데이터 화면의 날짜 필터는 채팅에 적용되지 않습니다. 질문을 보내면 대화가 서버에 자동 저장됩니다. 실패 시 같은 질문 재시도 버튼은 원래 요청 ID와 본문을 유지합니다.
- **대화 기록**: 목록에서 선택하면 메시지를 첫 페이지부터 끝까지 순서대로 불러옵니다. 새 대화·삭제를 지원합니다. 큰 대화의 202 삭제 작업은 간격을 두고 최대 8회 재개하고, 남은 작업은 목록의 ↻ 버튼으로 이어서 처리합니다. 새로고침·연결 종료 뒤에는 서버 CLI로 정리를 재개할 수 있습니다.

모든 개인 데이터는 FastAPI를 통해서만 읽고 씁니다. 브라우저는 Firestore에 직접 접근하지 않습니다. 외부 메모·대화는 HTML로 실행하지 않고 텍스트로 표시합니다. OpenAI 키가 없으면 채팅 오류가 표시되며 가짜 답변은 생성하지 않습니다.

### 웹 검사 재현

```bash
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend test
pnpm --dir frontend exec playwright install chromium
```

브라우저 통합 검사는 공식 Auth·Firestore 에뮬레이터와 실제 API를 사용합니다. **모델 출력만** 별도 테스트 서버 안에서 대체합니다. 운영 API에 이 코드가 포함되거나 인증을 우회하는 설정은 없습니다. 먼저 `firebase emulators:start --only firestore,auth --project demo-stock-assistant`를 실행하고 다음을 별도 터미널에서 실행합니다.

```bash
# 테스트 API: 알려진 임시 계정과 실제 Yahoo 응답 3건을 로컬에만 저장
APP_ENV=test FIREBASE_PROJECT_ID=demo-stock-assistant \
FIRESTORE_EMULATOR_HOST=127.0.0.1:8080 FIREBASE_AUTH_EMULATOR_HOST=127.0.0.1:9099 \
ALLOWED_USER_UIDS=browser-test-owner,browser-test-other \
OPENAI_API_KEY=browser-test-only OPENAI_MODEL=browser-test-stub \
PYTHONPATH=. .venv/bin/python frontend/tests/serve_api.py
```

```bash
# 테스트 전용 공개 설정으로 빌드하고 정적 서버 실행
API_BASE_URL=http://localhost:8000 \
FIREBASE_WEB_CONFIG_JSON='{"apiKey":"demo-key","authDomain":"demo-stock-assistant.firebaseapp.com","projectId":"demo-stock-assistant","appId":"1:demo:web:test"}' \
FRONTEND_AUTH_EMULATOR_URL=http://127.0.0.1:9099 node frontend/scripts/build.mjs
python3 -m http.server 5173 --directory frontend/dist
# 다른 터미널
pnpm --dir frontend browser:test
```

브라우저 검사는 실제 Yahoo 재수집도 호출하므로 인터넷 연결이 필요합니다. `artifacts/web-*.png`에 화면을 저장합니다. 테스트 후 에뮬레이터·서버를 종료하고 개인 설정으로 다시 빌드하세요. 클라우드 연결에서는 세 에뮬레이터 변수를 모두 비웁니다. 프런트 에뮬레이터 연결은 localhost에서만 허용합니다.

## 배포와 다음 단계

`render.yaml`은 백엔드 배포 설정입니다. 아직 배포하지 않았습니다. Render의 환경변수에는 `.env.example` 중 서버 필수 항목을 입력하고, 에뮬레이터 변수는 설정하지 않습니다. `/healthz` 정상만으로 Firebase·OpenAI 연동 완료를 의미하지 않습니다. 실제 API 호출도 확인하세요.

`vercel.json`은 프로젝트 루트에서 `node frontend/scripts/build.mjs`를 실행하고 `frontend/dist`를 배포하도록 구성했습니다. Vercel에는 `API_BASE_URL`과 `FIREBASE_WEB_CONFIG_JSON`만 넣습니다. 서버 키·서비스 계정·Discord 웹훅은 Render에만 저장합니다. 빌드는 공개 설정 세 항목과 Firebase 웹 구성의 허용된 필드만 출력하며 서비스 계정 입력은 거부합니다. Google 로그인에는 Vercel 도메인 승인도 필요합니다. 배포는 아직 수행하지 않았습니다.

이후 구현 순서는 기술 지표·뉴스·심리 → Discord 브리핑 → 조건부 알림 → MCP → Vercel·Render 통합 배포입니다.

| 제출 항목 | 상태 |
| --- | --- |
| 백엔드 필수 API | 구현, 로컬 검증 결과는 `docs/verification.md` 참조 |
| 실제 Yahoo 수집·Firestore 저장 | 수집기 구현, 로컬 에뮬레이터 검증; 클라우드는 자격 설정 후 확인 |
| 실제 OpenAI 답변 | 연동 구현, API 키·모델 지정 후 실호출 필요 |
| 바닐라 웹 UI·화면 증빙 | 구현, 로컬 데스크톱·모바일 검증 및 `artifacts/web-*.png` |
| Discord·MCP·배포 URL | 다음 단계 |
