# 외부 AI 채팅에서 MCP 연결하기

공식 Python SDK 2.3을 사용하는 **로컬 stdio 서버**다. 웹·AI 채팅과 같은 시세 공급원, 계산 함수, Firestore 저장소를 사용한다. API 서버를 따로 실행할 필요는 없다. 원격 HTTP 연결과 OAuth는 이번 구현 범위에 포함하지 않는다.

## 설치와 서버 설정

프로젝트 루트에서 실행한다. Python 3.11~3.13을 지원하며 3.12로 검증했다.

```bash
uv sync --locked --extra dev
cp .env.example .env
# uv가 없다면 같은 가상환경에 python -m pip install -e .
```

| 설정 | 용도 |
| --- | --- |
| `FIREBASE_PROJECT_ID` | 기존 웹 API와 같은 Firestore 프로젝트 |
| `GOOGLE_APPLICATION_CREDENTIALS` | 서버 서비스 계정 JSON의 **절대 경로**. 대신 `FIREBASE_SERVICE_ACCOUNT_JSON` 또는 ADC 사용 가능 |
| `ALLOWED_USER_UIDS` | Firebase Authentication 사용자 UID 허용 목록 |
| `MCP_OWNER_UID` | 위 목록에 포함된 한 사용자. 이 사용자의 데이터·알림에 고정 |
| `MCP_CONFIRMATION_TIMEOUT_SECONDS` | 알림 변경 확인 대기, 기본 120초, 30~300초 |
| `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`, `FINNHUB_API_KEY` | 기존 실제 뉴스 수집 설정. 없으면 해당 공급원은 미설정 상태 |
| `NEWS_CACHE_MINUTES`, `SENTIMENT_CACHE_MINUTES`, `ANALYSIS_CACHE_MINUTES`, `DATA_POLL_MINUTES` | 기존 서비스와 공유하는 캐시·수집 간격 |
| `JOB_OWNER_UID`, `DISCORD_WEBHOOK_URL` | 저장한 알림을 Discord로 받을 때 필요한 별도 worker 설정 |

MCP는 추가 LLM 호출을 하지 않으므로 `OPENAI_API_KEY`가 필요하지 않다. 외부 채팅 앱이 자기 모델로 결과를 해석한다. API 키·Firebase 자격·Discord 웹훅은 서버의 `.env`에만 보관한다. 도구 인수로 UID, 비밀 키, 자격 파일 경로를 전달할 수 없다. 호스트 JSON에 `.env` 내용을 복사하지 않는다.

실제 클라우드에서는 에뮬레이터 변수를 비운다. 로컬 검증에는 `APP_ENV=test`, `FIREBASE_PROJECT_ID=demo-stock-assistant`, `FIRESTORE_EMULATOR_HOST=127.0.0.1:8080`과 임의의 **테스트 UID**를 쓴다. MCP 자체에는 Auth 에뮬레이터가 필요하지 않다.

## 실행과 호스트 등록

```bash
/ABSOLUTE/PATH/TO/project/.venv/bin/python -m mcp_adapter.server \
  --env-file /ABSOLUTE/PATH/TO/project/.env
```

stdio는 호스트가 위 프로세스를 실행해 stdin/stdout으로 통신한다. 터미널에서 실행했을 때 배너 없이 대기하는 것이 정상이다. stdout은 프로토콜 전용, 진단은 stderr다. `--env-file`은 존재하는 절대 경로이며 운영체제 환경변수가 파일 값보다 우선한다. 생략하면 소스 프로젝트의 `.env`를 사용한다.

[예시 설정](../examples/mcp-host.json)의 경로를 실제 절대 경로로 바꾸고 **stdio를 지원하는 로컬 MCP 호스트**에 추가한다.

```json
{
  "mcpServers": {
    "stock-market-assistant": {
      "command": "/ABSOLUTE/PATH/TO/project/.venv/bin/python",
      "args": ["-m", "mcp_adapter.server", "--env-file", "/ABSOLUTE/PATH/TO/project/.env"],
      "env": {}
    }
  }
}
```

Claude Desktop의 `claude_desktop_config.json` 또는 Cursor의 `.cursor/mcp.json`처럼 `mcpServers` 형식을 사용하는 설정에 병합한다. 기존 서버 항목을 보존하고 앱을 재시작한 뒤 도구 16개가 표시되는지 확인한다. 호스트 작업 디렉터리가 달라도 동작하도록 패키지를 설치하고 Python·환경 파일·자격 파일에 절대 경로를 사용한다. 원격 HTTP URL만 받는 서비스에는 이 stdio 설정을 직접 연결할 수 없다.

로컬 단일 사용자 서버이므로 이 프로세스를 사용하는 호스트는 지정한 사용자 데이터에 접근한다. 여러 사람에게 네트워크로 공개하는 인증 서버로 사용하지 않는다.

## 도구 계약

도구별 정확한 JSON 스키마는 MCP `tools/list`에서 확인할 수 있다. 알림 객체 내부까지 추가 필드를 거절한다.

| 도구 | 주요 인수·동작 |
| --- | --- |
| `list_instruments` | `market` 선택: KR/US. 생략하면 지원하는 9개 대상 |
| `get_quote` | `instrument_id`. 실제 정규장 완성 5분 봉 종가·관측/수집 시각·지연·장 상태 |
| `get_price_history` | 종목·선택 날짜 범위. `interval`은 `1d`만 허용, 최대 730일, 기본 최근 365일 |
| `calculate_indicators` | 같은 날짜 범위의 실제 원본 조정 종가로 기존 계산 함수 실행 |
| `summarize_dataset` | `instrument_id`, 선택 `start_date`·`end_date`. 해당 사용자의 편집 가능한 저장 데이터 요약 |
| `search_news` | `market` 필수, `instrument_id` 선택, `window=24h/7d`. 실제 기사 최대 5건·발췌 요약·링크 |
| `get_us_sentiment` | 인수 없음. CNN 미국 주식 공포탐욕·VIX, 긴 CNN 이력 제외 |
| `get_market_analysis` | `instrument_id`. 웹·Function Calling과 같은 최근 1년 원본 분석 |
| `get_market_overview` | `market` 필수, `instrument_id` 선택, `window=24h/7d`. 기본 KOSPI/S&P 500 |
| `list_alerts` | `active` 선택. 내 규칙·상태·revision |
| `preview_alert_rule` | `rule`: 기존 `AlertValues`. 저장 없는 구조 검증·조건 해석 |
| `create_alert` | `rule`: 기존 `AlertCreate`, `idempotency_key` 포함. 확인 후 저장 |
| `update_alert` | `alert_id`, 전체 `rule`: `expected_revision`·`reset_state` 포함. 확인 후 수정 |
| `reset_alert` | `alert_id`, `expected_revision`. 확인 후 진행 상태 초기화·감시 재개 |
| `list_alert_events` | `limit=1..50`, `cursor` 선택. 내 발생·전송 이력 페이지 |
| `get_alert_event` | `event_id`. 내 이벤트와 부분별 전송 결과 |

계산은 기존 조정 종가 수익률, 연율화 실현 변동성, Wilder RSI, 이동평균, 거래량 비율 등을 반환한다. 사용자 편집 데이터와 원본 시장 데이터를 혼합하지 않는다. 관측치가 부족한 수치는 `null`이다. 뉴스·시세·CNN이 미설정·실패·오래된 경우 해당 상태를 그대로 전달하며 합성 값으로 대체하지 않는다. 뉴스·메모의 문장은 분석 자료로만 취급한다.

일봉 조회는 원본 `market_bars`만 저장하며 사용자 `data`를 만들거나 덮어쓰지 않는다. 가격 조회는 worker와 `price_streams`/`price_quotes` 캐시·수집 임대를 공유한다. 주문, 임의 코드·URL 실행, 즉시 Discord 발송·재시도 도구는 제공하지 않는다.

## 가격 알림 예시와 확인

외부 채팅에서 “삼성전자가 35,000원을 상승 돌파한 뒤 더 나중에 30,000원 이하가 되면 한 번 알려줘”라고 요청하면 다음 **하나의 순차 규칙**으로 해석한다.

`preview_alert_rule` 인수:

```json
{
  "rule": {
    "instrument_id": "KRX_005930",
    "currency": "KRW",
    "steps": [
      {"op": "cross_above", "price": "35000"},
      {"op": "lte", "price": "30000"}
    ],
    "session_scope": "regular",
    "repeat_mode": "once",
    "expires_at": null,
    "step_timeout_hours": null,
    "active": true
  }
}
```

`create_alert`에는 같은 `rule` 안에 `idempotency_key`를 추가한다. 같은 요청 재시도에는 같은 키를 사용한다. 다른 규칙에 키를 재사용하면 충돌한다. 단순 목표가는 `steps` 한 개, 같은 관측에서 모두 충족할 조건은 `{"all":[...]}`로 표현한다. 전체 규칙·정밀도·경계값·공백 처리 규칙은 [README](../README.md)의 가격 알림 절을 따른다.

생성·수정·초기화 호출은 서버가 검증한 정확한 조건과 변경 전후 내용을 **MCP 폼 elicitation**으로 보여준다. 사용자가 해당 폼에서 승인하여 `action=accept`, `confirmed=true`를 모두 반환한 경우에만 저장한다. 취소·거절·false·시간 초과에는 저장하지 않는다. `confirmed`를 도구 인수로 넣어 확인을 우회할 수 없다. 확인 응답은 그 실행 중인 호출의 검증된 인수에만 적용된다. 수정·초기화는 저장 시점에도 revision을 검사하므로 확인 도중 다른 곳에서 바뀐 규칙을 덮어쓰지 않는다.

이 확인 경로는 `initialize`로 연결하는 MCP 프로토콜의 중첩 `elicitation/create`를 사용한다. Python SDK 2.x로 직접 클라이언트를 작성할 때는 `Client(..., mode="legacy", elicitation_callback=...)`로 연결한다. 새 `discover` 방식처럼 중첩 요청 채널이 없는 연결은 변경을 거절한다.

**호스트가 중첩 form elicitation을 지원하지 않으면** 조회·미리보기는 사용할 수 있고 변경은 `confirmation_unavailable`로 실패한다. 이 경우 기존 웹 가격 알림 화면에서 저장한다. 호스트의 일반 도구 승인 UI와 MCP 폼 지원은 별개다.

`active=false/true`를 전체 규칙 수정으로 보내 일시정지·재개할 수 있다. 조건을 바꾸려면 `reset_state=true`가 필요하다. 완료한 규칙은 `reset_alert`로 다시 감시한다. 알림 삭제와 전송 재시도는 기존 웹 화면에서 한다.

MCP는 worker를 시작하지 않는다. 저장한 규칙을 Discord로 받으려면 같은 사용자로 기존 worker를 별도 실행한다.

```bash
.venv/bin/python -m jobs.tick --loop
```

이 worker의 `JOB_OWNER_UID`가 `MCP_OWNER_UID`와 같아야 해당 규칙을 감시한다. 정규장 완성 5분 봉을 비교하므로 순간 호가 돌파를 모두 잡지는 못하고, Yahoo 지연과 수집 간격만큼 늦을 수 있다.

## 연결 검증과 오류 확인

공식 SDK 클라이언트가 서버를 **별도 subprocess**로 실행하여 initialize 연결·도구 목록·엄격한 사용자 인수 거절을 확인한다. 알림은 생성하지 않는다.

```bash
.venv/bin/python -m mcp_adapter.smoke --env-file /ABSOLUTE/PATH/TO/project/.env
# 실제 Yahoo 두 시장 시세·일봉·계산까지 조회. 원본 캐시는 갱신될 수 있다.
.venv/bin/python -m mcp_adapter.smoke --env-file /ABSOLUTE/PATH/TO/project/.env --live
```

정상 결과는 `tools: 16`이다. `--live`는 실제 공급원 데이터와 상태를 JSON으로 출력한다. 불가용 응답을 최신 시세로 해석하지 말고 `quality_status`, `event_time`, `fetched_at` 등을 함께 확인한다.

| 오류 코드 | 조치 |
| --- | --- |
| `mcp_owner_invalid` | MCP UID와 허용 목록 확인 |
| `configuration_missing/invalid` | 프로젝트 ID·절대 경로·Firebase 자격·설정 타입 확인 |
| `tool_args_invalid` | `tools/list` 스키마, 가격 문자열·날짜·불필요한 필드 확인 |
| `confirmation_unavailable` | form elicitation 지원 호스트 사용 또는 웹에서 저장 |
| `confirmation_declined/timeout` | 변경 없음. 필요하면 새 호출에서 다시 확인 |
| `revision_conflict/reset_required` | 최신 규칙을 조회하고 변경 조건·진행 상태 초기화 확인 |
| `idempotency_conflict` | 같은 요청은 같은 내용·키로 재시도, 새 규칙에는 새 키 사용 |
| `tool_unavailable` | 서버 stderr와 공급원·Firestore 연결 확인. 비밀값은 로그에 출력하지 않음 |

자동 검사와 실제 공급원 결과는 [검증 기록](verification.md)을 참고한다. SDK 클라이언트 검증은 실제 AI 호스트에서 사용자가 대화한 검증과 구분한다. 외부 앱에 설정을 등록한 뒤 위 예시를 실제 채팅에서도 확인한다.

참고: [공식 SDK](https://github.com/modelcontextprotocol/python-sdk), [호스트 연결](https://py.sdk.modelcontextprotocol.io/get-started/real-host/), [Elicitation 규격](https://modelcontextprotocol.io/specification/2025-11-25/client/elicitation).
