# 배포 런북 (Vercel + Render + Firebase)

Vercel 프런트·Render API·선택 Discord 워커의 배포 절차다. 실제 클라우드 배포·유료 서비스 생성·Discord 전송은 아직 수행하지 않았다. 로컬 검증 결과는 [검증 기록](verification.md)의 배포 준비 절에 있다.

계정·키를 처음 준비할 때는 [첫 배포 준비 안내](first-deploy.md)를 먼저 따른다.

## 1. 구성 개요

- 프론트엔드: Vercel 정적 호스팅. 저장소 루트에서 빌드하고 frontend/dist를 제공한다.
- API: Render 웹 서비스(Free). 루트 render.yaml은 API 전용으로 둔다. FastAPI + /healthz 헬스 체크.
- 워커(선택, 유료): Discord 예약 전송이 필요할 때만 별도 Blueprint(deploy/render-worker.yaml, Starter 이상,
  백그라운드 워커 타입, numInstances 1)로 둔다. API용 render.yaml과 같은 Blueprint에 합치지 않는다.
- 두 Blueprint 모두 환경변수 그룹 참조를 넣지 않는다. 같은 값을 sync:false로 각 서비스에 직접 입력한다.
  운영자가 Render 대시보드에서 환경변수 그룹을 공유하도록 설정할 수는 있지만, 자동으로 공유되지 않는다.
- Free 플랜 Render 웹 서비스는 트래픽이 없으면 슬립 상태가 되므로 연속 가격 알림 용도로 쓰지 않는다.
  연속 알림은 유료 워커 또는 항상 켜진 로컬 머신의 워커로 처리한다. [Render 무료 서비스 제한](https://render.com/docs/free) 참조.

## 2. 배포 순서 (순환 참조 해소)

API 주소와 프론트 주소가 서로를 참조하므로 아래 순서대로 진행한다.

1. 먼저 Firebase 프로젝트·DB·웹 앱·테스트 사용자·서버 자격을 준비한다(9장).
2. Vercel 프로젝트를 만들어 고정 Production 도메인(예: `your-app.vercel.app`)을 확인한다. 이 단계는 URL 확보용이며,
   API 주소와 Firebase 설정이 비어 있어 첫 빌드는 실패가 정상이다. 발행이 아니다.
3. 6장의 사전 검사를 통과한 값을 Render API에 입력한다. `ALLOWED_ORIGINS`에는 `https://your-app.vercel.app`을 넣는다.
4. Render API 배포가 준비되면 Vercel 환경변수 두 개에 API 주소와 Firebase 웹 JSON을 넣고 다시 빌드한다. 환경변수 변경은 새 빌드부터 적용된다.
5. Firebase 승인 도메인에 Vercel 호스트명을 추가한다(9장).
6. 11장의 수동 점검 후 필요하면 별도 워커를 생성한다. 로컬 워커를 쓰면 유료 워커는 생성하지 않는다.

Render 리전은 Firebase 데이터베이스와 가깝게 둔다. Vercel 정적 호스팅에는 컴퓨트 리전 선택이 없으므로
Vercel 쪽 리전 설정은 신경 쓰지 않는다.

## 3. Vercel 설정

- Root Directory: ./ (저장소 루트).
- Framework Preset: Other (`vercel.json`의 `framework: null`).
- Build Command: node frontend/scripts/build.mjs.
- Output Directory: frontend/dist.
- Install Command: 빈 값. 프론트엔드에 의존성이 없어 설치 단계가 필요 없다.
- config.js에만 no-store 헤더를 둔다(vercel.json headers, source /config.js).
- Node.js: 공식 지원 버전 중 24.x 또는 22.x를 사용한다. 24.x가 기본값이며 22.x도 정식 지원된다.
- Environment Variables의 **Enable access to System Environment Variables**를 켜서 빌드에 `VERCEL_ENV`가 전달되게 한다. 운영·프리뷰 검사는 이 값을 사용한다. [Vercel 시스템 변수](https://vercel.com/docs/environment-variables/system-environment-variables) 참조.

빌드 스크립트는 공개 키 3개(API_BASE_URL, FIREBASE_WEB_CONFIG_JSON, FRONTEND_AUTH_EMULATOR_URL)만 읽어
frontend/dist/config.js를 생성한다. 프로덕션/프리뷰 빌드는 API 주소가 https의 localhost가 아닌 값이고
Firebase 웹 설정이 들어 있으며 에뮬레이터 URL이 비어 있어야 한다. 공개되는 설정은 허용된 3개 키 그대로다.
Firebase 웹 설정이 비어 있는 빌드는 로컬에서만 설정 화면 상태로 동작한다. 프로덕션/프리뷰 빌드에서는 Firebase 웹 설정이 필수이며 없으면 실패한다.

## 4. Render API 설정 (루트 render.yaml 기준)

Render 대시보드의 **New → Blueprint**에서 저장소와 배포할 브랜치를 선택하고 Blueprint Path를 `render.yaml`로 지정한다. Root Directory는 비워 저장소 루트를 사용한다. 기존 Blueprint에 새 `sync: false` 항목을 추가한 경우에는 서비스 Environment에 직접 입력해야 한다.

- 타입: web, 런타임: python, 플랜: free.
- 빌드: pip install -r requirements.lock.
- 시작: python -m jobs.check_config --component api && exec uvicorn app.main:app --host 0.0.0.0 --port $PORT.
  시작 전 check_config가 구성을 검사하고, 실패하면 API를 띄우지 않는다. exec 형태 그대로 둔다. exec가 있어야 Render의 SIGTERM이 실제 프로세스에 전달된다. 워커를 uvicorn 시작 과정에 넣지 않는다.
- 헬스 체크 경로: /healthz.
- 고정값: PYTHON_VERSION=3.12.14, APP_ENV=production, 캐시 3종 기본값 30.
- 나머지 값(Firebase 자격, 허용 UID/오리진, OpenAI, 뉴스 키, Discord, JOB_OWNER_UID 등)은 대시보드에서 직접 입력한다.
- Free 플랜 제약: 비활성 시 슬립, ephemeral 파일시스템, 영속 디스크 사용 불가. 상태 저장은 Firestore에만 둔다.
- `/healthz`는 프로세스 생존 검사다. DB·인증·외부 공급원의 연결 상태를 검사하지 않는다.

## 5. Render 워커 설정 (선택, 유료)

같은 저장소로 **별도 Blueprint**를 생성하고 Blueprint Path를 `deploy/render-worker.yaml`로 지정한다. Root Directory는 `deploy/`로 바꾸지 않는다. 이 설정은 유료 Starter 인스턴스를 생성하므로 비용 없이 시연하려면 10장의 로컬 실행을 사용한다.

- 별도 Blueprint 파일(deploy/render-worker.yaml)로 관리한다. 타입은 백그라운드 워커이며 Starter 이상 플랜을 사용한다.
- numInstances 1, maxShutdownDelaySeconds 300. 같은 소유자로 워커를 중복 실행하지 않는다.
- 백그라운드 워커는 외부 HTTP 요청을 받지 않으므로 헬스 체크용 HTTP 엔드포인트를 두지 않는다.
- 시작: python -m jobs.check_config --component worker && exec python -m jobs.tick --loop --interval 60.
- 종료 유예 300초: SIGTERM을 받으면 유휴 상태에서는 바로, 실행 중에는 현재 서비스 틱을 마치고 종료하며,
  시작/종료 로그를 JSON으로 남긴다. 300초를 넘기면 Render가 강제 종료할 수 있다. 전송 도중 강제 종료되거나 응답이 유실되면 성공 여부가 불확실해지고 재시도 시 중복될 수 있다.

## 6. 오프라인 사전 점검 (jobs.check_config)

배포 전에 루트 .env에 프로덕션 값을 채운 뒤 아래 명령으로 검사한다. 외부 요청을 보내지 않으며, PEM 검사는 키 파일 자체의 유효성만 본다. 실제 권한 증명이 아니다.
검사 결과는 비밀값을 걷어낸 JSON(errors/warnings 필드)으로만 출력한다.

```bash
APP_ENV=production .venv/bin/python -m jobs.check_config --component api
APP_ENV=production .venv/bin/python -m jobs.check_config --component worker
```

공통 필수: APP_ENV=production, FIREBASE_PROJECT_ID, ALLOWED_USER_UIDS, 서비스 계정 JSON 또는 파일 경로.
두 자격 방식 모두 PEM을 파싱하며, JSON의 `project_id`도 같은 프로젝트여야 한다. JSON과 파일 경로는 동시에 설정하지 않는다.
API 추가 조건: ALLOWED_ORIGINS가 전부 정확한 https 오리진이며 localhost가 아니다.
워커 추가 조건: JOB_OWNER_UID가 허용 목록에 있고 DISCORD_WEBHOOK_URL이 유효한 웹후크 주소다. 둘 중 하나라도 어기면 오류다.
OpenAI/Naver/Finnhub 키가 비어 있으면 경고만 남긴다. OpenAI 채팅은 사용할 수 없고 뉴스 요약은 발췌 방식이며, 뉴스 키가 없는 공급원은 `news_not_configured` 상태가 된다. API 검사는 Discord 설정 두 항목 중 하나만 입력되면 경고하고, 워커 검사는 허용 UID·웹훅 주소를 필수로 검사한다.

## 7. 환경변수 매트릭스

에뮬레이터 3종(FIRESTORE_EMULATOR_HOST, FIREBASE_AUTH_EMULATOR_HOST, FRONTEND_AUTH_EMULATOR_URL)은
프로덕션의 모든 위치에서 비운다. 워커 값은 API와 같은 값을 워커 서비스에 직접 입력한다.

| 변수 | Vercel(공개) | Render API | Render 워커(선택) |
| --- | --- | --- | --- |
| APP_ENV | - | production | production |
| PYTHON_VERSION | - | 3.12.14 (YAML 고정) | 3.12.14 (YAML 고정) |
| API_BASE_URL | 보유 (Render API https 주소) | - | - |
| FIREBASE_WEB_CONFIG_JSON | 보유 (웹 클라이언트용 공개 config) | - | - |
| FRONTEND_AUTH_EMULATOR_URL | 비움 | - | - |
| FIREBASE_PROJECT_ID | - | 보유 | 보유 (같은 값) |
| FIREBASE_SERVICE_ACCOUNT_JSON | 절대 금지 | 보유 (한 줄 JSON) | 보유 (같은 값) |
| GOOGLE_APPLICATION_CREDENTIALS | - | JSON 대신 쓸 때만 파일 경로 | JSON 대신 쓸 때만 파일 경로 |
| ALLOWED_USER_UIDS | - | 보유 | 보유 (같은 값) |
| JOB_OWNER_UID | - | 등록 (큐 처리 시 필요) | 보유 (필수, 허용 목록 중 하나) |
| ALLOWED_ORIGINS | - | 보유 (정확한 https 오리진) | - |
| OPENAI_API_KEY / OPENAI_MODEL | 절대 금지 | 보유 (없으면 경고) | 보유 (같은 값) |
| NAVER_API_MODE | - | api_hub (기존 개발자센터 키는 legacy) | API와 같은 값 |
| NAVER_CLIENT_ID / NAVER_CLIENT_SECRET / FINNHUB_API_KEY | - | 보유 (없으면 경고) | 보유 (같은 값) |
| NEWS_CACHE_MINUTES / SENTIMENT_CACHE_MINUTES / ANALYSIS_CACHE_MINUTES | - | 기본 30 | 같은 값 |
| DATA_POLL_MINUTES | - | 보유 | 보유 (같은 값) |
| DISCORD_WEBHOOK_URL | 절대 금지 | 등록 (큐 처리 시 필요) | 보유 (필수) |
| MCP_OWNER_UID | - | - (로컬 MCP 전용) | - |
| FIRESTORE_EMULATOR_HOST / FIREBASE_AUTH_EMULATOR_HOST | - | 비움 | 비움 |

Firebase 웹 projectId는 서버의 FIREBASE_PROJECT_ID와 같은 프로젝트여야 한다.
Naver 신규 신청은 NAVER Cloud의 NAVER API HUB 검색 API에서 한다. `NAVER_API_MODE=api_hub`는 API Gateway 주소와 `X-NCP-APIGW-API-KEY-ID`/`X-NCP-APIGW-API-KEY` 헤더를 사용한다. 2026-07-31 이전 개발자센터 키는 `legacy`를 명시한다. 기존 키는 2027-06-30 종료 예정이며 두 종류의 자격은 호환되지 않는다. [공식 이전 안내](https://guide.ncloud-docs.com/docs/apihub-migration), [뉴스 API 규격](https://api.ncloud-docs.com/docs/naver-api-hub-search-news) 참조.
서비스 계정 JSON, OpenAI 키, Discord 웹후크를 Vercel/FIREBASE_WEB_CONFIG_JSON에 넣지 않는다.
빌드 스크립트는 서비스 계정 키 형태가 들어오면 실패한다.
Render 환경변수에는 `.env`의 따옴표로 감싸기 전 JSON 원문을 한 줄로 입력하고, PEM 안 줄바꿈은 JSON의 `\n` 형태로 보존한다. Render Secret File을 쓸 경우 각 서비스에 별도 마운트하고 `GOOGLE_APPLICATION_CREDENTIALS`를 해당 절대 경로로 설정한 뒤 JSON 변수는 제거한다. 파일은 Git에 넣지 않는다.
토큰·도구 제한 등 추가 설정은 `.env.example`의 기본값을 사용하며, 변경할 경우 필요한 API·워커에 같은 값을 입력한다.

## 8. CORS와 오리진 규칙

- ALLOWED_ORIGINS에는 정확한 오리진만 쉼표로 나열한다. 와일드카드와 정규식을 사용하지 않는다.
- 프리뷰 배포 도메인은 기본적으로 등록하지 않는다. 포크된 미리보기 같은 신뢰할 수 없는 도메인이
  자동으로 허용되지 않도록 한다. 검증이 필요한 프리뷰만 일시 등록하고 검증 후 제거한다.

## 9. Firebase 설정

### 9.1 제공자

- 같은 Firebase 프로젝트에 Firestore Native mode `(default)` DB와 웹 앱을 만든다. 웹 앱의 공개 구성을 `FIREBASE_WEB_CONFIG_JSON`에 복사하고 서버 `FIREBASE_PROJECT_ID`와 일치시킨다.
- Authentication에서 Email/Password와 Google 로그인을 켠다. 먼저 Console에서 이메일 테스트 계정을 만들고 UID를 `ALLOWED_USER_UIDS`에 입력하면 최초 API 배포를 준비할 수 있다. Google로 처음 로그인한 계정의 UID도 확인해 허용 목록에 추가한다. 로그인 성공 후 API 403이면 이 목록부터 확인한다.
- 프로젝트 설정 → 서비스 계정에서 서버 키를 생성해 Render의 비공개 환경변수에 넣는다. 직접 만든 서비스 계정은 DB 읽기·쓰기를 허용하는 `roles/datastore.user` 등의 IAM 권한이 필요하다. 키 형식 검사가 이 권한을 확인하지는 않는다. [Admin SDK 설정](https://firebase.google.com/docs/admin/setup), [Firestore IAM](https://firebase.google.com/docs/firestore/security/iam) 참조.

### 9.2 승인 도메인

- Firebase 콘솔의 승인된 도메인 목록에 Vercel 안정 도메인의 호스트명만 추가한다.
  스킴과 포트, 경로는 적지 않는다.
- 기본 도메인은 유지한다. 신뢰할 수 없는 프리뷰 도메인은 추가하지 않는다.

### 9.3 authDomain

- 프론트 Firebase 설정의 authDomain은 기본값(<프로젝트>.firebaseapp.com)을 그대로 둔다.
- 커스텀 authDomain이 코드에서 금지된 것은 아니다. 다만 Firebase 호스팅 프록시 구성이 제대로 갖춰진 경우에만 쓰고,
  이 프로젝트는 그 구성을 제공하지 않으므로 기본값을 쓴다.
- signInWithPopup은 기본 authDomain 그대로 사용한다.

### 9.4 Google OAuth 리다이렉트

- 기본 팝업 로그인 흐름에서는 Vercel용 OAuth 리다이렉트 URI를 별도로 등록하지 않는다.
- https://<authDomain>/__/auth/handler 형식의 리다이렉트 URI는 Google Cloud OAuth 클라이언트를
  직접 손질할 때만 사용한다. 커스텀 authDomain을 쓰지 않는 한 Vercel 주소 등록이 필요하지 않다.

### 9.5 Firestore 규칙과 인덱스

- 브라우저 직접 접근은 전면 거부한다(allow read, write: if false). 모든 검사는 서버 Admin SDK에서 UID 기준으로 한다.
- 규칙과 인덱스 배포는 Firebase CLI로 수동 실행하며, 이 문서 작성 시점에는 실행하지 않는다.
- 기존 프로젝트에 이미 규칙이 있으면 현재 규칙을 먼저 백업한 뒤 배포한다.
- 인덱스 정의는 `firestore.indexes.json`을 기준으로 한다. 클라우드에서 인덱스 빌드 완료를 기다리고 실제 조회를 확인한다. 에뮬레이터는 복합 인덱스 누락을 강제하지 않는다.

```bash
firebase deploy --only firestore:rules,firestore:indexes --project YOUR_PROJECT_ID
```

## 10. Discord 워커 운영

추가 호스팅 비용 없이 시연하려면 동일한 프로덕션 `.env`와 Firebase 자격을 둔 로컬 머신에서 아래를 실행한다. 가상환경 의존성을 먼저 설치하고, 실행 중 머신의 절전·종료를 피한다. Render 워커와 동시에 켜지 않는다.

```bash
APP_ENV=production .venv/bin/python -m jobs.check_config --component worker
.venv/bin/python -m jobs.tick --loop --interval 60
# 1회만 처리: 이 명령도 실제 전송을 수행한다.
.venv/bin/python -m jobs.tick
```

- 워커는 JOB_OWNER_UID 한 소유자의 브리핑/알림을 순서대로 처리한다. 같은 소유자로 프로세스를 중복 실행하지 않는다.
- jobs.tick에는 전송 없는 미리보기(dry-run)가 없다. 1회 실행도 대기 중인 항목을 실제로 전송한다.
  먼저 6장의 check_config 오프라인 점검을 통과하고 Firestore 대기 항목을 확인한 뒤,
  승인된 채널에서 대기열 규모를 파악한 상태로만 사용자가 직접 tick을 실행한다.
- 틱 로그는 브리핑/알림 처리 결과를 JSON 한 줄로 남긴다. 로그에 웹후크 URL과 키를 출력하지 않는다.
- `--interval 60`은 틱 완료 뒤 다음 확인까지의 대기 시간이다. `DATA_POLL_MINUTES=5`는 종목별 시세 수집 간격이며 서로 다르다. 예약의 IANA 시간대(`Asia/Seoul`, `America/New_York`)로 실행 시각을 계산하므로 서버 시스템 시간대를 바꿀 필요가 없다.
- 진단은 알림 모니터 상태, outbox 적체, HTTP 429, 리스 만료를 순서대로 본다.
  리스 만료 후 수동 재전송은 중복을 감수한다.
- 따라잡기(catchup)는 놓친 브리핑을 전부 보충하지 않는다. 워커가 멈춰 있던 구간의 알림은 누락될 수 있다.
- 가격 알림은 정규장 완성 5분 봉 기준이며, 한국 종목의 Yahoo 시세에는 약 20분 지연이 있다.
- 외부 시세가 클라우드에서 실패하면 상태값이 unavailable로 표시된다. 원인은 그때의 로그와 응답으로 확인하고,
  특정 외부 호스트 차단 같은 단정은 하지 않는다.
- 상태와 대기열은 Firestore에만 둔다. Render 디스크에 상태를 두지 않는다.

## 11. 배포 후 수동 점검

아래를 순서대로 사람이 직접 확인한다.

1. `GET /healthz` 200과 Render 시작 로그의 사전 검사 결과.
2. 토큰 없이 보호 API 호출 → 401.
3. 허용 오리진에서 호출 → 성공, 미등록 오리진에서 호출 → CORS 차단.
4. Firebase 이메일 로그인.
5. Firebase Google 로그인.
6. 실제 Yahoo 일봉 수집 후 데이터 생성·수정·삭제·전체 요약과 Firestore 저장 확인.
7. Naver/Finnhub 뉴스·CNN 심리·변동성 조회. 누락된 키·공급원 실패 상태도 확인.
8. AI 답변과 대화 저장·새로고침 후 기록 불러오기·삭제 확인.
9. 예약 브리핑 1건과 가격 알림 규칙의 저장·편집·일시정지·이력 조회 확인. 실가격 조건은 실제 관측 때만 발생한다.
10. 10장의 절차에 따라 테스트 채널에 큐 1건을 전송하고 웹 이력·Discord 수신을 대조한다. worker 재시작 후 예약·진행 상태가 보존되는지 확인한다.

## 12. 롤백과 일시정지

- 프런트 롤백: Vercel의 이전 정상 배포를 복원한다. API 주소만 바뀐 문제라면 `API_BASE_URL`을 고치고 다시 빌드한다.
- API/워커 롤백: 같은 정상 커밋을 다시 배포한다. 코드 롤백은 Firestore 문서와 환경변수를 되돌리지 않으므로 별도 확인한다.
- CORS 정리: 미등록 프리뷰 도메인을 ALLOWED_ORIGINS에서 제거한다.
- Firebase 정리: 신뢰할 수 없는 프리뷰 도메인을 승인 도메인에서 제거한다.
- 워커 일시정지: 워커 서비스를 중지한다. 이미 Firestore 대기열에 쌓인 항목은 지워지지 않으므로,
  재시작하면 대기 항목이 전송될 수 있다.

## 13. MCP 참고

- MCP 서버(python -m mcp_adapter.server)는 stdio 전용 로컬 실행이다. Render API URL을 가리키는 형태로 두지 않는다.
- 로컬 MCP 소유자(MCP_OWNER_UID)는 ALLOWED_USER_UIDS에 포함된 UID여야 한다.

## 출처

- [Vercel Node.js 버전](https://vercel.com/docs/functions/runtimes/node-js/node-js-versions)
- [Vercel 정적 배포 설정](https://vercel.com/docs/project-configuration/vercel-json)
- [Render Free](https://render.com/docs/free), [백그라운드 워커](https://render.com/docs/background-workers), [Blueprint](https://render.com/docs/blueprint-spec)
- [Firebase Google 로그인](https://firebase.google.com/docs/auth/web/google-signin), [외부 호스팅과 authDomain](https://firebase.google.com/docs/auth/web/redirect-best-practices)
- Firebase 승인 도메인 위치: Console → Authentication → Settings → Authorized domains
- 저장소 기준 파일: render.yaml, deploy/render-worker.yaml, vercel.json, .env.example,
  frontend/scripts/build.mjs, jobs/check_config.py, jobs/tick.py, firebase.json,
  firestore.rules, firestore.indexes.json, app/config.py, app/main.py
