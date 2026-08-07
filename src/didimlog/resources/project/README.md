# Knowledge Harness (v1) — `improver` 프로젝트 지식 저장소

이 디렉터리는 `improver` 프로젝트가 학습한 내용을 **결정론적 파일**로 남기고 다시
찾아 쓰기 위한 최소 저장소다. 처음 보는 사람도 이 문서 하나로 무엇을 어떻게
기록하고 조회하는지 알 수 있도록 작성했다. 사람이 읽는 본문은 한국어로 쓰고,
코드·명령·식별자·schema key·enum 값(예: `observation`, `draft`)은 영어 원형을
그대로 둔다.

> **가장 중요한 원칙:** `knowledge/records/` 안의 기록(record)만이 유일한 진실
> 출처(source of truth)다. 앞으로 어떤 wiki·OKF·OpenKB 출력이 생기더라도 그것은
> 기록에서 다시 생성할 수 있는 **비규범적(advisory) 파생물**이며, 항상
> record-tree digest로 도장이 찍힌다. 어떤 사실도 그 파생 계층에만 존재할 수 없다.

---

## 0. 처음이라면 이 순서로 읽으세요 (first-reader path)
명령어와 용어가 낯설다면 먼저 프로젝트 루트의
`knowledge-harness-tutorial.html`을 브라우저로 여세요. Codex·Claude Code에 보낼
작업 전·후 문장부터 설명하는 실전 사용 안내서이며, 이 README는 세부 계약을 확인할 때
사용합니다.


1. `knowledge-harness-tutorial.html` — 에이전트 채팅부터 시작하는 한국어 사용 설명서.
2. `knowledge/POINTER.md` — 조회 순서와 금지사항만 담은 짧은 안내(규칙 아님).
3. 이 `README.md` — 용어·구조·명령·수명주기 전체 설명.
4. `knowledge/schema/record.schema.json` — 기록 frontmatter의 정확한 형식.
5. `knowledge/records/` 아래 실제 기록. 현재 비어 있다면 이후 실제 작업에서 채운다.

조회(retrieve)할 때의 순서는 항상: **POINTER.md → active/harness.md →
index/INDEX.md → 선택한 기록(최대 5건)**. 이 순서는 POINTER.md에도 요약돼 있다.

---

## 1. 목적 (purpose)

- 관찰한 사실, 실험 결과, 근거 자료를 **프롬프트에 부담을 주지 않으면서** 파일로
  영구 보존한다.
- 실패·반증 같은 부정 결과도 지우지 않고 보존해 같은 실수를 반복하지 않게 한다.
- 사람이 diff로 검토할 수 있는 투명하고 결정론적인 구조를 유지한다.
- v1은 **기억과 회고**만 돕는다. 에이전트 규칙을 스스로 바꾸는 기능은 없다.

---

## 2. 핵심 어휘: OBS / EXP / EVD

v1은 세 가지 record type만 지원한다. `type` 값과 ID prefix는 반드시 일치한다.

| type | ID prefix | 뜻 | 언제 쓰나 |
|---|---|---|---|
| `observation` | `OBS` | 관찰(Observation) | 확인한 사실이나 현상을 그대로 적을 때 |
| `experiment` | `EXP` | 실험(Experiment) | 가설을 세우고 방법·결과·해석을 남길 때 |
| `evidence` | `EVD` | 근거(Evidence) | 로그·산출물 같은 원자료를 참조·고정할 때 |

- ID 형식은 `PREFIX-YYYYMMDD-NN` (예: `OBS-20260714-01`). 날짜와 2자리 순번을
  사람이 직접 지정하며 자동 추측하지 않는다.
- `finding`, `decision`, `playbook`, `failure_pattern`, `improvement_proposal`
  (FIN/DEC/PLB/FPT/IMP) 등 다른 type은 **v1에서 구현하지 않는다**(8절 참고).

---

## 3. 진실 출처 계층 (source-of-truth layers)

```text
knowledge/
  raw/                     # 원자료(artifact). 파생 도구가 절대 수정하지 않는다.
  records/                 # 유일한 규범적 진실 출처: OBS/EXP/EVD 기록
    observation/
    experiment/
    evidence/
  index/INDEX.md           # records/ 로부터 만든 결정론적 조회용 색인(파생물)
  schema/record.schema.json# frontmatter 형식(검토용 사본)
  active/harness.md        # v1에서는 heading만 있는 빈 파일(9절 참고)
  README.md  POINTER.md
```

계층의 신뢰 순서:

1. `raw/` — 소스 산출물. 파생 도구가 건드리지 않는다.
2. `records/` — **유일한 규범적 진실 출처.** 엄격한 형식, 수명주기, supersession,
   digest를 가진다.
3. `index/INDEX.md` — `records/` 에서 결정론적으로 생성한 조회용 파생물.
4. (미래) `knowledge/wiki/` 또는 OKF export — 기록에서 재생성되는 **비규범적
   자문용** 계층으로, canonical record-tree digest 도장이 찍힌다. 여기에만 존재하는
   사실은 허용되지 않는다. **v1에서는 만들지 않는다.**
5. `active/harness.md` — v1에서는 비어 있다. 생성된 skill·wiki가 여기에 쓸 수 없다.

---

## 4. 전체 흐름: capture → index → retrieve

1. **capture(기록):** `didim add observation|experiment|evidence`로 OBS/EXP/EVD 기록 파일 하나를 만든다.
2. **index(색인):** `didim index`로 `records/` 전체를 다시 읽어
   `index/INDEX.md`를 원자적으로 다시 쓴다.
3. **retrieve(조회):** `index/INDEX.md`를 열어 관련 행을 찾고, 정렬 순서대로 관련
   기록을 **최대 5건** 직접 연다.

> v1의 조회는 색인과 기록을 **직접 읽는** 방식이다. 토큰 정규화·negative-first
> 순위·STALE 표시를 자동으로 해 주는 결정론적 `trusted-kh query` 도구는 **아직
> 없으며 Stage 3로 보류**돼 있다(8절).

`INDEX.md`의 각 행은 UTF-8/LF TSV이며 형식은 다음과 같다(기록 1건당 1행):

```text
scope<TAB>negative-rank<TAB>status-rank<TAB>status<TAB>updated<TAB>id<TAB>type<TAB>title<TAB>comma-separated-tags
```

- `didim index`는 `draft`, `running`, `validated`, `refuted`, `superseded` 상태의 기록을
  **모두** 색인에 넣고 `status` 열에 상태를 명시한다.
- v1 색인은 `stale` 열을 저장하지 않는다. `didim index`는 wall clock을 읽지 않고
  `review_by`의 날짜 유효성만 검사한다. 조회자는 기록을 연 뒤 query/runner 기준일과
  `review_by`를 비교해 8.4절 규칙을 적용한다.
- `draft`·`running` 기록은 조회 가능하지만 `status` 열을 보고 `UNVERIFIED`로
  취급하며, 활성 지침·평가 근거·확정 결론·승격 입력으로 쓸 수 없다.
- `didim index`가 만드는 digest의 이름은 `canonical-record-tree-digest`다("validated
  tree digest"가 아니다). `didim index`를 두 번 돌리면 byte 단위로 동일한 `INDEX.md`가
  나온다.

---

## 5. Didimlog CLI

기록 유형별 필수 인자와 비대화형 사용법은 설치된 버전의 도움말을 기준으로 확인한다.
현재 날짜를 암묵적으로 추측하지 않으며, 자동화에서는 `--date YYYY-MM-DD`를 명시한다.

```sh
didim add observation --help
didim add experiment --help
didim add evidence --help
didim index --help
```

## 6. Evidence 산출물 바인딩: local vs git

모든 EVD 기록은 산출물을 **반드시** 선언한다(빈 provenance는 허용하지 않는다).
frontmatter에 다음 key가 붙는다.

- `artifact_path`: project 상대경로(필수). 절대경로, `..`, NUL, backslash,
  project 밖 symlink target은 금지한다.
- **local 모드:** `artifact_sha256`(소문자 64-hex)이 필수이고 `artifact_git`은
  없다. `didim add evidence`와 `didim index`가 실제 파일의 SHA-256을 다시 계산해 비교한다.
  불일치 시 exit `3`, `ARTIFACT_DIGEST_MISMATCH <evidence-id> <artifact-path>`.
- **git 모드:** `artifact_git`(현재 workspace 저장소의 완전한 40-hex 또는 64-hex
  commit object ID)이 필수이고 `artifact_sha256`은 없다. `didim add evidence`와 `didim index`가
  commit과 그 commit 안의 `artifact_path`를 모두 확인한다. git이 없거나 workspace가
  저장소가 아니어서 검증할 수 없으면 추측해 통과시키지 않고 exit `7`로 실패한다.
- 두 모드 중 **정확히 하나**만 legal하다. OBS·EXP 기록에는 어떤 `artifact_*` key도
  넣을 수 없다.

---

## 7. 기록 형식과 규칙

### 7.1 공통 frontmatter (schema)

`record.schema.json`이 정의하는 필수 key와 parser가 강제하는 순서:
`schema_version`, `id`, `type`, `title`, `status`, `scope`, `created`, `updated`,
`version`, `tags`, `sources`. 이어서 선택 key `review_by`, `supersedes`,
`superseded_by`가 올 수 있고, EVD에는 `artifact_*` key가 붙는다.
frontmatter는 `+++` 경계 안에서 key마다 정확히 `key = value` 한 줄을 쓰며 빈 줄,
comment, 임의 spacing을 허용하지 않는다. 배열·string escaping까지 정본 renderer의
byte 표현과 같아야 한다.

- `scope`는 `project` 또는 `task:<이름>` 형식이다.
- `sources`는 **EVD 또는 EXP ID만** 참조한다(7.2절). OBS는 source가 될 수 없다.
- schema가 표현할 수 없는 정책(날짜의 달력 유효성, `created<=updated`, UTF-8 byte
  예산, 참조 존재 여부 등)은 parser·`didim add`·`didim index`가 강제하며 schema의
  `$comment`에 근거로 적혀 있다.

### 7.2 source 참조 무결성 (source references)

`sources[]`의 각 ID는 다음을 만족해야 한다.

- 현재 workspace 기록 트리 안에 실제로 존재한다;
- 파일명·선언된 type prefix와 일치한다;
- schema가 허용하는 대로 EVD 또는 EXP다;
- 현재 기록 ID와 같지 않다(자기 참조 금지).

`refuted`·`superseded` 기록을 참조하는 것은 **합법**이다(부정 이력 보존). `didim add`는
생성 전에, `didim index`는 트리 전체를 다시 검사한다. 누락·불일치 참조는 exit `3`,
`DANGLING_SOURCE <record-id> -> <source-id>`.

### 7.3 Experiment 모순 신호: `Contradicts:`

`didim add experiment`는 `--contradicts`가 필수이며 기본값도 추론도 없다.
`## Interpretation` 아래 첫 번째 비어 있지 않은 줄은 정확히 다음 중 하나다.

- `Contradicts: none`
- `Contradicts: <ID>, <ID>, ...`

- ID는 OBS/EXP/EVD ID이며 UTF-8 byte 정렬·중복 없음·실제 존재·자기 참조 금지다.
- 구문 오류나 누락은 exit `2`; 없거나 불일치하는 ID는 exit `3`(`DANGLING_SOURCE`).
- 이 신호는 v1에서 **자문용**이며 조회 순위를 바꾸지 않는다. 미래
  Reflector/Failure Pattern 계획의 입력이 된다.

### 7.4 Korean 태그 (tags)

태그는 영어 또는 한국어를 쓸 수 있다. 정규 정책:

- Unicode NFKC로 정규화한다;
- ASCII 글자는 소문자다;
- 첫 글자는 Unicode general category `L*` 또는 `N*`이어야 한다;
- 나머지 글자는 `L*`, `N*`, `.`, `_`, `-`만 허용한다;
- 길이는 Unicode scalar 1–32이며 UTF-8 최대 96 byte다;
- 공백·제어문자·그 밖의 문장부호는 금지한다;
- 배열은 중복 없이 정규화된 UTF-8 byte 순으로 정렬한다.

schema는 이 중 이식 가능한 부분(ASCII 소문자/숫자/`._-`/비-ASCII 문자 허용, 첫
글자에 `._-` 금지, scalar 길이 상한)만 표현하고, 나머지(byte 상한, NFKC, 엄밀한
category, 정렬)는 parser가 강제한다.

### 7.5 본문 구조와 크기

본문의 H2 heading은 type별로 정확히 다음 목록만 허용하며, 각 section 값은
공백만 있는 값이 아니라 실제 내용을 가져야 한다.

- OBS: `## Observation`
- EXP: `## Hypothesis` → `## Method` → `## Result` → `## Interpretation`
- EVD: `## Artifact` → `## Origin` → `## Collection`

추가 H2 heading은 허용하지 않는다. EXP의 `## Interpretation`은 `Contradicts:` 줄
뒤에 실제 해석을 포함해야 하고, EVD의 `## Artifact` 값은 frontmatter의
`artifact_path`와 정확히 같아야 한다. record는 마지막 LF 하나로 끝나며 최대
12,000 UTF-8 bytes와 200 LF다. `didim add`와 `didim index`가 같은 계약을 강제한다.

---

## 8. 수명주기·상태·부정 결과·supersession·staleness

### 8.1 status 의미와 합법 전이 (lifecycle)

| status | 의미 |
|---|---|
| `draft` | 초안. 아직 검증되지 않음(`UNVERIFIED`). |
| `running` | 실험 진행 중(**EXP 전용**). |
| `validated` | 검증됨. |
| `refuted` | 반증됨(부정 결과, 보존 대상). |
| `superseded` | 후속 기록으로 대체됨. |

허용되는 전이 규칙은 다음과 같다.

- OBS/EVD: `draft→validated`, `draft→refuted`, `validated→superseded`,
  `refuted→superseded`.
- EXP: `draft→running`, `running→validated`, `running→refuted`,
  `validated→superseded`, `refuted→superseded`.

모든 전이는 `version = 이전 + 1`과 `updated >= 이전 updated`를 요구한다.
`validated`/`refuted` 이후에는 대부분의 필드가 **freeze**되고, 정정은 새 기록과
양방향 supersession link로만 한다. v1에는 상태 변경 명령이나 transition ledger가
없으므로 `didim index`는 현재 status·날짜·상호 link 무결성만 검사하며 이전 상태를
증명하지 않는다. 기존 기록을 편집하는 관리자는 위 전이 규칙을 지켜야 한다.

### 8.2 부정 결과 보존 (negative-result preservation)

- 부정(negative)은 **EXP의 `## Result`가 `failure`** 이거나 status가 `refuted`인
  기록이다.
- 부정 결과는 **절대 삭제하지 않는다.** 미래의 결정론적 조회는 같은 주제 안에서
  부정 결과를 먼저 보여주도록(negative-first) 설계돼 있다.

### 8.3 supersession

- 이미 freeze된 기록을 고쳐야 하면 새 기록을 만들고 `supersedes`(새 기록) /
  `superseded_by`(옛 기록)로 **양방향 link**를 건다.
- 옛 기록은 `superseded` 상태가 되지만 그대로 남아 이력이 보존된다.
- `didim index`는 link 대상의 존재·같은 type·상호 참조를 검사한다. `superseded_by`가
  있는 이전 기록과 `supersedes`가 가리키는 predecessor는 모두 `superseded`
  상태여야 하며, dangling 또는 한쪽짜리 link는 exit `3`으로 거부한다.

### 8.4 staleness

- 조회자는 색인에서 선택한 기록을 직접 연 뒤 선택 key `review_by`를 query/runner
  기준일과 비교한다. `review_by`가 기준일보다 이르면 그 기록은 **stale**이며,
  `review_by`가 없으면 stale이 아니다.
- stale 기록은 `STALE <ID>`로 취급·표시하며, 기본 지침·평가 근거·승격 입력으로
  쓸 수 없다. 참고만 하고 적용하지 않는다.

---

## 9. `active/harness.md` 와 승격 게이트

`active/harness.md`는 에이전트가 항상 따르는 활성 규칙이 놓일 자리다. **v1에서는
heading만 있는 빈 파일**이다. 활성 규칙은 미래 Stage 3의 격리된 trusted
평가·승인·ledger 게이트를 통과해 승인된 IMP(improvement_proposal)가 병합될 때에만
반영된다. 그 게이트가 별도의 승인된 계획으로 구현되기 전까지 사람·생성된 skill·
파생 wiki 중 누구도 이 파일에 직접 규칙을 쓸 수 없다.

---

## 10. 5-record 파일럿 게이트와 보류된 기능

### 10.1 5-record 파일럿 게이트 (pilot gate)

Stage 2에서는 **실제 기록 5건 이상**을 모으고, 그중 **`result=failure`인 EXP 1건**과
**`refuted` 기록 1건**을 반드시 포함한다. 이 게이트를 통과한 뒤에야, 관찰된 기록과
실패 패턴을 근거로 **새 합의 계획**을 세워 Stage 3(격리된 trusted 평가/승격)를
설계한다. v1은 Stage 3를 구현하지 않는다.

미래 Stage 3 계획의 **필수 입력**: Lilian Weng의 Self-Harness/AHE 증거와 held-out
non-regression 요건. 즉 v1에서 이 개념들을 실행하지 않지만, 다음 계획은 반드시
이것들을 근거로 설계해야 한다.

### 10.2 v1에서 명시적으로 보류/제외된 기능 (deferred)

아래는 **v1에 존재하지 않으며**, 각각 새 승인 계획을 거쳐야 한다. 문서·명령·예시
어디에서도 이들이 v1에 있는 것처럼 다루지 않는다.

- 추가 record type: `finding`, `decision`, `playbook`, `failure_pattern`,
  `improvement_proposal`(FIN/DEC/PLB/FPT/IMP).
- 격리된 외부 trusted 평가기(evaluator)와 `trusted-kh` 명령 전체
  (`validate`, `query`, `evaluate`, `promote`, `verify-evidence` 등).
- 서명/승인 receipt, held-out 평가, ledger, 활성 규칙 자동 승격.
- 결정론적 `trusted-kh query`(토큰 정규화·negative-first·STALE 자동 표시).
- OpenKB wrapper, 결정론적 `kh-wiki` concept-page 생성기, `kh-export-okf`,
  두 단계 ingest, wiki lint, `LOG.md`.
- embeddings·vector store·graph clustering·DB, 네트워크 ingest.
- 다른 프로젝트 소비 또는 global/cross-project 승격, 전역 지침 수정.

이 모든 것을 요약하면: **v1은 어떤 외부 신뢰 검증(validation)·질의(query)·승격
(promotion)도 제공하지 않는다.** 그런 능력이 있는 것처럼 주장하지 않는다.

---

## 11. 한 줄 요약

`records/`가 유일한 진실이다. `didim add`로 기록하고 `didim index`로 색인하며,
색인과 기록을 직접 읽어 조회한다. 부정 결과는 보존하고, 정정은 supersession으로 하며,
승격·외부 신뢰 게이트·파생 wiki는 모두 뒤 단계로 보류돼 있다.
