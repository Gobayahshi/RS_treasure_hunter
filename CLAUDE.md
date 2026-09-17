# RS-Treasure 작업 안내 (모든 대화창 공통)

한 저장소에서 **4개 프로젝트**를 대화창을 나눠 진행한다. 프로젝트 이름은 바뀔 수 있다.

| # | 프로젝트 | 상태 | 진입 URL |
|---|---|---|---|
| 1 | 보물찾기 (영업사원 방문 인증) | 운영 중 | `/`, `/admin` |
| 2 | 대리점 재고 (지도 + 챗봇) | 운영 중 | `/inventory` |
| 3 | 판매점별 시장동향 입력 | 미착수 | - |
| 4 | 정책 문의 챗봇 | 미착수 | - |

**대화창 규칙**
- 작업을 시작하면 공통 규칙과 자기 프로젝트 섹션을 먼저 읽는다.
- 작업을 마치면 자기 섹션의 "현재 상태 / 결정 사항 / 다음 할 일"을 갱신한다. 날짜는 절대 날짜(예: 2026-09-17)로 쓴다.
- 여러 프로젝트가 같은 파일(`webapp/app.py`, `webapp/db.py`, `webapp/static/css/style.css`)을 고친다. 동시에 작업할 때는 대화창마다 워크트리를 쓰거나, 한 번에 한 대화창만 편집한다.
- 공통 규칙(아래)을 바꿔야 하면 사용자에게 먼저 확인한다.

---

## 공통

### 기술 스택과 실행
- Python 3.9+ / Flask / SQLite(롤백 저널) / 프레임워크 없는 HTML·JS / Leaflet 지도. Node나 별도 DB 서버는 없다.
  - **WAL 쓰지 않는다:** 2026-09-17 Render 영구 디스크(`/data`, 네트워크 블록 스토리지)에서 WAL의 `-shm` mmap이 `disk I/O error`를 일으켜 로그인을 포함한 모든 쓰기가 실패했다. `db.get_conn()`은 `journal_mode = DELETE`를 쓴다. 되돌리지 않는다.
- 로컬 실행: `python app.py` (루트). 실제 앱은 `webapp/` 아래에 있다.
- 테스트: `cd webapp && pip install -r requirements-dev.txt && python -m pytest`
  - 임시 DB로 돌아서 `webapp/rs_treasure.db`를 건드리지 않는다.
  - **코드를 고치면 테스트를 추가하고 전부 통과시킨다.**
- 배포: **Render + gunicorn (`Procfile`)만 대상으로 한다.**
  - `master`에 push하면 운영에 반영될 수 있다. push 전에 사용자에게 확인한다.
- **사내 Playground는 절대 고려하지 않는다.**
  - 설계, 구현, 테스트, 검증, 배포 안내 어디에서도 Playground 동작이나 호환성을 따지지 않는다.
  - 관련 흔적(`Diyfile.yaml`, `CONTEXT_PATH`, `_PrefixMiddleware`, `window.APP_BASE`)은 기존 코드에 남아 있다. 이를 이유로 설계를 바꾸거나 Playground용 코드를 새로 넣지 않는다.
  - 이 흔적을 지우는 것도 사용자가 요청할 때만 한다.

### 코드 구조
```
app.py, wsgi.py, Procfile   실행/배포 진입점 (Diyfile.yaml은 Playground용이라 무시)
webapp/
  app.py            Flask 라우트 전부 (인증, 보물찾기, 재고, 관리자)
  db.py             SQLite 스키마, 부팅 시 마이그레이션(migrate_schema), 시드 동기화
  confidence.py     방문 인증 부정행위 규칙 R1~R9
  excel_import.py   대리점/사원/판매점 마스터 엑셀, normalize_store_code
  geocode.py        주소 → 좌표 (카카오 → Nominatim)
  inventory.py      재고 엑셀 파싱, 지도/집계 쿼리
  inventory_chat.py 재고 질문 규칙 기반 파서 + 답변 조립
  inventory_llm.py  Gemini/OpenAI REST 호출 (실패 시 규칙 기반으로 폴백)
  seed/rs_treasure.db  배포용 시드 (판매점 마스터 12,545곳, 대리점, 사원)
  static/           index.html(보물찾기) admin.html(관리) inventory.html(재고) + js/ css/
  tests/            pytest
```

### 사용자 계층 (모든 프로젝트 공통, 역할을 새로 만들지 않는다)
자세한 원문: `.cursor/rules/user-hierarchy.mdc`

| 역할 | 로그인 ID | 범위 | 권한 |
|---|---|---|---|
| SKT 총괄 | `admins.role='super'` (`admin`) | 전국 | 수정, 계정 발급 |
| SKT 직원 | `admins.role='staff'` (총괄이 `/admin`에서 발급) | 전국 | 조회만. 예외: 대리점 관리자/직원 구분 변경 가능 |
| 대리점 관리자/직원 | 사원 고유ID `reps.employee_code` | 소속 대리점만 | 조회 + 재고 업로드. `reps.dealer_role`(manager/staff)은 구분만 하고 권한은 같다 |
| 판매점 | P코드(판매점코드), 매장당 1개 | 자기 매장만 | 시장동향 입력 예정 (프로젝트 3) |

- 보물찾기 영업사원 = 대리점 직원. 같은 ID로 보물찾기와 재고 화면을 모두 쓴다.
- 서버 데코레이터: `require_admin`(총괄) · `require_skt`(총괄+직원, 조회) · `require_rep`(영업사원 본인) · `require_inventory_user` · `require_inventory_uploader`
- 세션: SKT는 `admin_sessions` + 헤더 `X-Admin-Token`. 사원은 `rep_sessions` + `X-Rep-Token` (재고 화면은 `X-Admin-Token` 헤더로 보내도 사원 토큰을 인정한다).
- **요청 본문의 `rep_id` 같은 신원 값은 믿지 않는다.** 항상 토큰의 주인을 쓴다.
- 소속 대리점이 없는 사원은 재고 화면을 쓸 수 없다 (볼 범위가 없으므로).

### 반드시 지킬 구현 규칙
- **시간:** DB에는 UTC로 저장하고, 날짜 경계·근무시간 같은 판단은 한국 시간으로 한다 (`confidence.to_kst`, `app.kst_now`, `app.kst_day_start_utc_iso`).
- **판매점코드:** 저장할 때 `normalize_store_code()`로 공백 없는 대문자로 맞춘다. 조인은 `s.store_code = i.store_code`처럼 단순 비교로 쓴다. `UPPER()/TRIM()`을 씌우면 인덱스를 못 타 수백 초가 걸린다 (2026-09-16 실측 413초 → 0.39초).
- **단일 워커:** SQLite 파일 하나 + 메모리 상태(`_UPLOAD_JOBS`, `_OVERVIEW_CACHE`) 때문에 gunicorn은 `--workers 1 --threads 8`이다.
- **Render 30초 제한:** 오래 걸리는 작업(대용량 업로드, 지오코딩)은 백그라운드 스레드 + 클라이언트 폴링으로 한다.
- **LLM:** 숫자(대수, 금액)는 LLM이 만들지 않는다. DB 집계나 문서 원문만 근거로 한다. 키가 없거나 호출이 실패해도 규칙 기반으로 동작해야 한다.
- **마이그레이션:** 도구 없이 `db.migrate_schema()`가 매 부팅마다 방어적으로 `ALTER TABLE ... ADD COLUMN` 한다. 새 컬럼은 `SCHEMA`와 `migrate_schema` 양쪽에 넣는다.
- **정적 파일 캐시:** `/`, `/admin`, `/inventory`는 `_page_response()`가 JS/CSS에 `?v=` 버전을 붙인다. API와 JS가 짝이 맞아야 하므로 새 페이지도 이 함수를 쓴다.

### git에 올리지 않는 것
`영업정책/` (사내 정책 원본, 대외비 가능), `2607_판매점 리스트_양식.xlsx`, `.venv-test/`, `webapp/rs_treasure.db`, `webapp/static/inventory-preview*` (시연용). 외부 서비스에 업로드하거나 커밋하기 전에 사용자에게 확인한다.

---

## 프로젝트 1: 보물찾기

**파일:** `confidence.py`, `static/index.html` + `js/app.js`, `static/admin.html` + `js/admin.js`, `app.py`의 `/api/treasures/*`, `/api/visit-sessions/*`, `/api/points*`, `/api/rewards*`, `/api/admin/visit-sessions*`

**현재 상태 (2026-09-17)**
- 방문 인증: 세션 시작 → 1초 간격 위치 샘플 → 완료 시 R1~R9 점수 → `auto_approved` / `pending_review` / `rejected`.
- `pending_review`는 `/admin`의 "검토 대기 방문"에서 총괄이 승인/반려한다 (`manual_approved` / `manual_rejected`, `reviewed_by/at` 기록). 승인은 자동 승인과 같은 `_grant_visit_points()`를 쓰고, R7(당일 중복) 건은 포인트를 주지 않는다.
- 포인트 잔액 = 적립 합계 − 리워드(`pending`/`issued`). 취소(`cancelled`)는 잔액으로 돌아온다. 잔액은 저장하지 않고 항상 계산한다 (`_rep_point_balance`).
- 리워드 신청 API는 있지만 영업사원 화면에 신청 UI는 아직 없다.

**결정 사항**
- 2026-09-16: 근무시간(R9)과 당일 중복(R7)은 한국 시간 기준이다.
- 2026-09-16: 영업사원 API는 모두 토큰 인증이다. 앱 인증 버전 `v3-token` (바꾸면 전원 재로그인).

**다음 할 일**
- 반경 30m / 정확도 100m 기준을 실제 반려 사유(`visit_sessions.flag_reasons`) 데이터로 조정.
- 서버에서 최소 샘플 수와 샘플 간격 검증 (지금은 샘플 1개로도 통과 가능).
- R1(가짜 위치)은 웹에서 판별할 수 없다. 필요하면 네이티브 앱/PWA 검토.
- 아이디어: 체화 재고(30일+)가 있는 매장에 rare 보물을 자동 스폰 (프로젝트 2와 연결).

---

## 프로젝트 2: 대리점 재고

**파일:** `inventory.py`, `inventory_chat.py`, `inventory_llm.py`, `static/inventory.html` + `js/inventory-chat.js`, `app.py`의 `/api/inventory/*`

**현재 상태 (2026-09-17)**
- 로그인: 대리점 직원은 사원 고유ID, SKT는 관리자 아이디 (같은 로그인 칸). SKT 직원은 업로드 버튼이 숨겨진다.
- 업로드: 비동기 잡(`_UPLOAD_JOBS`) + 클라이언트가 만든 32자리 `job_id`로 재시도해도 한 번만 처리. 해당 대리점의 이전 재고는 교체된다.
- 본사 "전체 대리점" 패널은 **직원이 한 명 이상 등록된 대리점** + 재고를 올린 대리점을 보여준다.

**결정 사항**
- 2026-09-17: 임시 대리점 계정(`yuwon`, `frisbee`, `jieun`)을 삭제한다. 부팅 시 `role='dealer'` 계정을 지운다.
- 2026-09-17: 재고 업로드는 대리점 관리자/직원 누구나 가능하다. SKT 직원은 불가.

**버그 이력**
- 2026-08-28 커밋 `8af313e`에서 `_partner_upload_filter`의 `def` 줄이 지워져, 전체 대리점 패널·대표상품명 드롭다운·영역 기종별 표·실구매가 합계·챗봇 분석이 500 오류였다. 2026-09-17 복구하고 테스트를 추가했다.
- 2026-09-17: 대리점을 고르거나(전체 대리점 목록 클릭), 기종/영역으로 필터링해도 지도가 실제 매장 위치로 이동하지 않고 항상 고정된 기본 화면(`fitLandscapeFocus`, 수도권 중심)으로 리셋되는 버그. 대리점 매장이 기본 화면 밖에 있으면 마커가 하나도 안 보였다 (예: 프리스비 서울/인천/경기 매장 276곳 확인 중 발견). `js/inventory-chat.js`의 `fitChatMap`에서 `data.dealer_id`가 있을 때는 `fitBounds`로 실제 좌표에 맞추도록 수정. 영역 선택(bbox 드래그) 결과도 지도를 옮기지 않도록(`keepView=true`) 고쳤다. 프론트엔드 전용 변경이라 pytest 대상 아님.

**다음 할 일**
- 재고 화면에서도 초기 비밀번호(=고유ID) 사용 시 변경을 요구 (보물찾기 앱은 이미 요구함).
- 배포 전: 임시 계정으로 올리던 대리점에 "사원 고유ID로 로그인" 공지.

---

## 프로젝트 3: 판매점별 시장동향 입력 (미착수)

**정해진 것 (사용자 계층 규칙)**
- 판매점 로그인은 P코드(판매점코드), 매장당 1개. 자기 매장만 입력·조회한다.
- SKT는 전국, 소속 대리점은 자기 매장들을 취합 조회한다.

**있는 자산**
- `stores` 테이블: 판매점 마스터 12,545곳 (P코드, 이름, 기본/상세주소, 좌표, 소속 대리점).
- `2607_판매점 리스트_양식.xlsx` (git 미추적): 판매점코드, 판매점명, 관할마케팅팀, 권역상권명, 우편번호, 기본주소, 상세주소, 시도명, 시군구명, 읍면동명, 법정동코드, 소집계구코드, 지정상권명. **`stores` 테이블에는 마케팅팀·상권 컬럼이 아직 없다.**

**사용자에게 정해야 할 것**
- 입력 항목(시장동향이 무엇인지), 입력 주기, 판매점 계정 발급·초기 비밀번호 방식, 입력 화면을 어느 URL에 둘지.

---

## 프로젝트 4: 정책 문의 챗봇 (미착수)

**있는 자산**
- `영업정책/` (git 미추적, 대외비 가능): 42개 파일. xlsx 26, docx 10, doc 1, pptx 2, png 2, md 1.
  - `1.이동전화`, `2.유선`, `3.보안,SK매직`, `4.구독, 부가`, `5.카드,안심보상,영업상품,보험`
  - 월별 문서(26년 8월/9월)라 매달 교체된다.
- `영업정책/1.이동전화/모델명_호칭_정규화_규칙.md` + `모델 조회.xlsx`(대표모델/모델명/펫네임): 사용자가 줄여 말한 모델명(예: `S948`, `F741 512기가`)을 공식 모델명에 연결하는 규칙.
  - **모델이 확정되지 않으면 금액이나 정책 적용 여부를 추정하지 않는다.**
- 재사용할 코드: `inventory_llm.py`(LLM 호출과 폴백 구조), `inventory_chat.py`와 `inventory-chat.js`의 모델번호 검색(예: `971` → `SM-F971`).

**사용자에게 정해야 할 것**
- 사용자(대리점 직원? SKT?)와 권한, 월별 문서 교체 방법(업로드 화면 vs 폴더), 답변에 문서 출처 표시 방식, 대외비 문서를 외부 LLM API로 보내도 되는지.

---

## 전체 이력

- 2026-09-16: 0단계 정리. KST 시간 버그, 검토 대기 처리, 영업사원 토큰 인증, 리워드 차감, 재고 조인 속도, 테스트 도입.
- 2026-09-17: 1단계 계정 통합. SKT 직원 역할, 사원 고유ID로 재고 로그인, 임시 계정 삭제, `_partner_upload_filter` 복구.
- 로드맵 후보: 보물찾기 운영 준비(규칙 튜닝) → 두 기능 연결(체화 재고 → rare 보물) → 판매점 입력(프로젝트 3).
