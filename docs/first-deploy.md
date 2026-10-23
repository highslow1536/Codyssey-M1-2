# 첫 배포: 계정과 로컬 설정 준비

실제 계정 연결을 위한 순서다. 배포별 환경변수·CORS·운영 절차는 [배포 런북](deployment.md)을 따른다. 키와 비밀번호는 채팅에 보내지 않고 Git에서 제외된 로컬 `.env`에 입력한다. 서버 서비스 계정 JSON은 저장소 밖에 보관한다.

## 1. Firebase — 이 항목부터 준비

1. [Firebase Console](https://console.firebase.google.com/)에서 과제용 프로젝트를 만들거나 사용할 기존 프로젝트를 선택한다. **프로젝트 ID**를 `.env`의 `FIREBASE_PROJECT_ID`에 입력한다. 표시 이름과 프로젝트 ID는 다르다.
2. Firestore Database에서 Native mode의 `(default)` 데이터베이스를 만든다. 처음부터 프로덕션 모드로 만들고, 뒤이어 저장소의 규칙·인덱스를 배포한다. 리전은 이후 Render와 가까운 곳을 선택한다.
3. Authentication → Sign-in method에서 이메일/비밀번호와 Google을 켠다. Google의 지원 이메일도 설정한다.
4. Authentication → Users에서 이메일 테스트 사용자를 만든다. 비밀번호는 본인이 보관하고 로그인 화면에 직접 입력한다. 사용자의 UID를 `ALLOWED_USER_UIDS`, `JOB_OWNER_UID`에 동일하게 입력한다. MCP도 쓸 경우 `MCP_OWNER_UID`에 같은 UID를 넣는다.
5. 프로젝트 설정 → 일반 → 내 앱에서 **웹 앱**을 등록한다. Firebase Hosting은 이 구성에 필요하지 않다. 제공되는 웹 config 객체를 아래처럼 **유효한 JSON 한 줄**로 바꿔 `FIREBASE_WEB_CONFIG_JSON`에 입력한다. `authDomain`은 기본값을 유지한다.

```dotenv
FIREBASE_PROJECT_ID=your-project-id
ALLOWED_USER_UIDS=your-authentication-user-uid
JOB_OWNER_UID=your-authentication-user-uid
FIREBASE_WEB_CONFIG_JSON='{"apiKey":"your-web-api-key","authDomain":"your-project-id.firebaseapp.com","projectId":"your-project-id","appId":"your-web-app-id"}'
```

6. 프로젝트 설정 → 서비스 계정 → 새 비공개 키 생성으로 JSON을 내려받는다. 저장소 밖에 보관하고 `GOOGLE_APPLICATION_CREDENTIALS`에 **파일의 절대 경로**를 넣는다. 로컬의 `FIREBASE_SERVICE_ACCOUNT_JSON`은 비워 둔다. 파일 경로가 공백을 포함하면 전체 값을 따옴표로 감싼다.

```dotenv
GOOGLE_APPLICATION_CREDENTIALS="/absolute/path/outside/repository/service-account.json"
FIREBASE_SERVICE_ACCOUNT_JSON=
FIRESTORE_EMULATOR_HOST=
FIREBASE_AUTH_EMULATOR_HOST=
FRONTEND_AUTH_EMULATOR_URL=
```

서버 키 JSON과 웹 config는 서로 다른 자료다. 서버 JSON을 웹 config에 넣지 않는다. 서버 자격 형식과 프로젝트 일치 여부를 로컬에서 먼저 검사하고, 실제 DB 권한은 이후 클라우드 왕복으로 확인한다. [서버 자격 공식 안내](https://firebase.google.com/docs/admin/setup)

소규모 검증은 Spark 무료 할당량부터 사용한다. 기능이나 사용량 때문에 유료 플랜이 필요하다는 안내가 나오면 전환 전에 요금을 확인한다. [Firebase 요금제](https://firebase.google.com/docs/projects/billing/firebase-pricing-plans)

## 2. GitHub·Vercel·Render — 기본 화면과 API 배포

- GitHub에서 기존 저장소를 사용할지, 새 비공개 저장소를 만들지 정한다. 기존 저장소라면 URL만 공유한다. 푸시 전에 실제 키·`.env`·서비스 계정 파일이 추적되지 않는지 확인한다.
- [Vercel](https://vercel.com/signup)·[Render](https://dashboard.render.com/register)에 가입하거나 로그인한다. 개인 무료 플랜으로 시작한다. CLI가 연 인증 페이지에서는 본인이 계정을 확인하고 연결을 승인한다. 계정 토큰을 채팅에 복사할 필요가 없다.
- GitHub 앱 연결이 필요하면 이번 저장소만 선택한다. 이 연결은 계정 소유자가 브라우저에서 처리한다.
- Vercel Production 주소를 확보한 뒤 Render API의 `ALLOWED_ORIGINS`를 정한다. Render API가 준비되면 Vercel에 `API_BASE_URL`과 `FIREBASE_WEB_CONFIG_JSON`을 입력해 배포한다. Vercel에는 서버 키를 업로드하지 않는다.
- Firebase Authentication → Settings → Authorized domains에 **실제 Vercel Production 호스트명**을 추가한다. `https://`나 경로는 넣지 않는다. 로컬 브라우저 검증도 할 경우 `localhost`를 명시적으로 추가한다.

Render에는 로컬 파일 경로 대신 서비스 계정 JSON 내용을 비공개 `FIREBASE_SERVICE_ACCOUNT_JSON`으로 전달하거나 Render Secret File을 사용한다. 두 자격 방식을 동시에 설정하지 않는다. 서버 키의 클라우드 전달은 배포에 필요한 단계이며 웹 공개 설정과 분리한다.

## 3. 뉴스 — 무료 키로 추가

### Finnhub

[Finnhub](https://finnhub.io/) 무료 계정을 만들고 대시보드의 API 키를 `FINNHUB_API_KEY`에 입력한다. 이 앱은 미국 시장의 general 뉴스와 개별 종목 company-news를 사용한다. 키 발급 후 두 요청을 각각 실제 호출해 계정에서의 이용 가능 여부를 확인한다. 반환 기사 범위와 호출 제한은 해당 플랜을 따른다.

### Naver

새 키는 NAVER Cloud Console → Application Services → **NAVER API HUB**에서 이용 신청하고, Application 등록 시 검색 API를 선택해 발급한다. `NAVER_API_MODE=api_hub`, `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`을 입력한다. 현재 무료 플랜과 신청 조건은 콘솔에서 확인한다. 별도의 유료 서비스를 선택할 필요는 없다.

2026-07-31부터 개발자센터 검색 API의 신규 신청이 중단됐다. 이전에 발급한 개발자센터 키를 사용할 때만 `NAVER_API_MODE=legacy`로 변경한다. 두 종류의 키는 주소·인증 헤더가 달라 혼용할 수 없고, 기존 서비스는 2027-06-30 종료 예정이다. [공식 이전 안내](https://guide.ncloud-docs.com/docs/apihub-migration), [뉴스 요청 규격](https://api.ncloud-docs.com/docs/naver-api-hub-search-news)

Yahoo 시세와 CNN 공포탐욕지수에는 별도 키가 없다. CNN은 공개 계약이 없는 웹사이트 피드이므로 실제 접근 성공·관측 시각을 매번 확인한다.

## 4. AI — 키와 모델이 준비되면 추가

[OpenAI API quickstart](https://developers.openai.com/api/docs/quickstart)에 따라 프로젝트 API 키를 만들고 `OPENAI_API_KEY`, 사용할 모델 ID를 `OPENAI_MODEL`에 입력한다. API 사용에는 비용이 발생할 수 있다. 키는 서버에서만 사용한다. 키와 모델을 준비하기 전에도 데이터·지표 조회와 뉴스 발췌 요약은 검증할 수 있다.

## 5. Discord — 테스트 채널로 수신 확인

과제용 서버에 테스트 텍스트 채널을 만든다. 서버 설정 → 연동 → 웹후크에서 해당 채널의 웹후크를 만들고 URL을 `DISCORD_WEBHOOK_URL`에 입력한다. **봇 토큰은 필요하지 않다.** URL 자체가 비밀값이다. [Discord 공식 안내](https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks)

처음에는 로컬 워커 한 개로 검증할 수 있다. Render Background Worker는 유료 서비스이므로 무료 API Blueprint와 분리했다. 유료 워커를 선택하기 전에는 생성하지 않는다. 로컬 워커는 컴퓨터가 켜져 있어야 예약을 처리한다. [Render 무료 서비스 범위](https://render.com/docs/free)

전송 전에 해당 소유자의 기존 예약과 대기열을 확인한다. 승인된 테스트 브리핑 한 건의 큐·Discord 실제 메시지·웹 전송 이력을 대조한다. 가격 알림의 조건 충족은 실제 관측으로만 확인하며, 원하는 시세를 만들어 알림을 발생시키지 않는다.

## 준비된 항목부터 확인하는 순서

1. 키 없이 Yahoo 한국·미국 일봉, VIX, CNN 피드 연결을 확인한다.
2. Firebase가 준비되면 규칙·인덱스 배포, 실제 로그인, 실제 시세 저장·조회·수정·삭제·요약과 클라우드 복합 인덱스를 확인한다.
3. GitHub·Vercel·Render가 준비되면 무료 API·프런트를 배포하고 실제 주소의 `/healthz`·인증·CORS·이메일/Google 로그인을 확인한다.
4. 각 뉴스 키가 준비되면 실제 기사·출처·수집 시각과 웹 화면을 확인한다.
5. OpenAI가 준비되면 실제 답변·도구 호출·대화 저장·새로고침 후 기록 복원을 확인한다.
6. Discord가 준비되면 로컬 워커에서 예약 전송·전송 이력과 가격 규칙 저장을 확인한다. 실제 가격 조건 발생 여부는 관측 결과와 구분해 기록한다.

준비 상태를 알려줄 때는 “Firebase 준비 완료”, “Finnhub 준비 완료”처럼 항목명만 보내면 된다. 비밀값이나 사용자 비밀번호를 보내지 않는다.
