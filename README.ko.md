# Didimlog

[English](README.md) | 한국어

Didimlog는 Claude Code와 함께 일하며 확인한 **교훈, 관찰, 실험, 근거 자료**를 로컬 파일로 남기고, 다음 작업에서 필요한 내용만 다시 찾게 하는 CLI입니다.

다음과 같은 경우에 적합합니다.

- 같은 문제를 다시 풀지 않도록 검증한 교훈을 프로젝트별로 보존하고 싶다.
- 실험 결과와 원자료를 Git 프로젝트에 연결해 추적하고 싶다.
- 전체 지식 본문을 AI context에 항상 넣지 않고 관련 자료만 불러오고 싶다.
- 원문을 덮어쓰지 않는 로컬 우선 저장 방식을 원한다.

현재 개발 단계는 **Pre-Alpha**입니다. macOS·Linux와 Python 3.11~3.14를 지원하며 Windows는 아직 검증하지 않았습니다.

## 요구사항

- macOS 또는 Linux
- Python 3.11~3.14
- 권장 설치 도구인 [`uv`](https://docs.astral.sh/uv/)
- 프로젝트 기록을 남길 경우 Git 저장소
- Claude Code를 연결할 경우 한 번 이상 실행해 만든 Claude 설정 디렉터리(기본값 `~/.claude`)

Claude Code를 사용하지 않거나 아직 설정하지 않았다면 처음 설정에서 `--skip-claude`를 사용할 수 있습니다.

## 빠른 시작

이 경로는 Didimlog를 설치하고, Git 프로젝트에 저장 공간을 준비한 뒤, 첫 교훈을 저장하고 index 상태까지 확인합니다.

### 1. 설치

```sh
uv tool install didimlog
didim --version
```

현재 버전에서는 다음과 같이 출력됩니다.

```text
Didimlog 0.0.2
```

`pipx`를 사용한다면 `pipx install didimlog`로 설치할 수 있습니다.

### 2. 변경 계획을 확인하고 설정

대상 Git 프로젝트의 최상위 디렉터리에서 실행합니다.

```sh
cd /path/to/your-project
didim setup --dry-run
didim setup --yes
```

`--dry-run`은 파일을 바꾸지 않고 개인 지식, 프로젝트 근거 자료, Claude 연결에 생길 변경을 보여 줍니다. 실제 설정이 끝나면 마지막 줄에 다음 문구가 출력됩니다.

```text
Didimlog 준비를 마쳤습니다.
```

같은 명령을 다시 실행해도 이미 준비된 항목은 `변경 없음`으로 표시됩니다.

### 3. 준비 상태 확인

```sh
didim status
```

정상적으로 준비됐다면 다음 상태를 확인할 수 있습니다. 프로젝트 이름은 현재 Git 최상위 디렉터리 이름으로 표시됩니다.

```text
Didimlog 0.0.2
개인 지식: 최신
현재 프로젝트: <프로젝트 이름>
프로젝트 근거: 최신
Claude 연결: 정상
```

### 4. 첫 교훈 저장

교훈은 Markdown 원문을 표준 입력으로 받습니다. 아래 예시는 실행 시각을 slug에 넣으므로 반복해도 기존 교훈을 덮어쓰지 않습니다.

```sh
today="$(date +%F)"
slug="didimlog-quick-start-$(date +%Y%m%d-%H%M%S)"
cat > /tmp/didimlog-quick-start.md <<EOF
---
topic: didimlog-quick-start
title: 같은 문제를 다시 풀지 않는다
summary: 검증한 해결 방법을 저장하고 다음 작업에서 다시 찾는다
tags: [didimlog, quick-start]
date: $today
---
## 상황
반복되는 작업에서 이미 검증한 해결 방법이 필요했다.

## 교훈
작업이 끝난 뒤 재사용할 조건과 절차를 교훈으로 저장한다.

## 근거
Didimlog로 교훈을 저장하고 index 상태를 확인했다.
EOF

didim add lesson "$slug" --date "$today" < /tmp/didimlog-quick-start.md
didim index --check
```

성공하면 교훈 경로와 두 index의 현재 상태가 출력됩니다.

```text
lessons/<프로젝트 이름>/didimlog-quick-start-<실행 시각>.md
개인 지식: PERSONAL_INDEX_CURRENT
프로젝트 근거: PROJECT_INDEX_CURRENT
```

이제 교훈 원문은 `~/knowledge/lessons/<프로젝트 이름>/`에 남고, Claude Code는 다음 작업에서 index를 먼저 검색한 뒤 관련 원문만 읽습니다.

## 다음에 할 일

- 팀이 같은 프로젝트 기록을 Git으로 공유해야 한다면 [프로젝트 지식을 팀과 공유하기](#프로젝트-지식을-팀과-공유하기)
- 실제로 확인한 사실을 남기려면 [프로젝트 관찰 기록하기](#프로젝트-관찰-기록하기)
- 가설과 결과를 함께 남기려면 [실험 결과 기록하기](#실험-결과-기록하기)
- 파일이나 Git 원본을 검증 가능한 형태로 묶으려면 [근거 자료 등록하기](#근거-자료-등록하기)
- 상태가 예상과 다르면 [문제 진단하기](#문제-진단하기)

## 자주 하는 작업

### 프로젝트 지식을 팀과 공유하기

기본 설정은 프로젝트의 `knowledge/`를 이 컴퓨터에서만 사용합니다. Git에 포함하려면 다음 명령으로 설정을 다시 적용합니다.

```sh
didim setup --yes --project-knowledge shared
```

`shared`는 로컬 Git 제외 파일에서 Didimlog가 관리하는 블록만 제거합니다. `.gitignore`, 전역 제외 설정, 사용자가 작성한 다른 제외 규칙은 바꾸지 않습니다. 명령이 제외 규칙이 남아 있다고 안내하면 해당 규칙을 직접 확인해야 합니다.

다시 로컬 전용으로 바꾸려면 다음 명령을 사용합니다.

```sh
didim setup --yes --project-knowledge local
```

### 프로젝트 관찰 기록하기

관찰은 실제로 확인한 재사용 가능한 사실입니다. JSON 본문에는 `body`만 넣습니다.

```sh
today="$(date +%F)"
printf '%s' '{"body":"setup 뒤 status의 네 항목이 모두 정상 또는 최신으로 표시됐다."}' |
  didim add observation \
    --date "$today" \
    --title "초기 설정 상태 확인" \
    --tags "setup,status"
```

성공하면 Didimlog가 ID를 할당하고 다음 형태의 경로를 출력합니다.

```text
<git-root>/knowledge/records/observation/OBS-YYYYMMDD-NN.md
```

### 실험 결과 기록하기

실험은 가설, 방법, 결과, 모순 신호, 해석을 함께 저장합니다. `result`는 `success`, `failure`, `inconclusive` 중 하나이며 `contradicts`는 모순이 없으면 `none`입니다.

```sh
today="$(date +%F)"
printf '%s' '{"hypothesis":"index를 다시 만들면 저장 직후 상태를 유지한다.","method":"didim index를 실행한 뒤 didim index --check를 실행했다.","result":"success","contradicts":"none","interpretation":"두 index가 최신이므로 현재 기록 트리와 일치한다."}' |
  didim add experiment \
    --date "$today" \
    --title "index 재생성 확인" \
    --tags "index"
```

성공하면 다음 형태의 경로가 출력됩니다.

```text
<git-root>/knowledge/records/experiment/EXP-YYYYMMDD-NN.md
```

### 근거 자료 등록하기

로컬 파일을 근거 자료로 등록하려면 먼저 `knowledge/raw/` 아래에 파일을 만들고 SHA-256을 함께 제출합니다. 다음 예시는 고유한 파일명을 사용합니다.

```sh
today="$(date +%F)"
artifact="knowledge/raw/setup-status-$(date +%Y%m%d-%H%M%S).txt"
printf 'setup status: current\n' > "$artifact"
digest="$(python3 -c 'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' "$artifact")"
printf '{"artifact":"%s","origin":"didim status output","collection":"captured after setup","artifact_sha256":"%s"}' "$artifact" "$digest" |
  didim add evidence \
    --date "$today" \
    --title "설정 상태 원본" \
    --tags "setup,status"
```

성공하면 다음 형태의 경로가 출력됩니다.

```text
<git-root>/knowledge/records/evidence/EVD-YYYYMMDD-NN.md
```

Git commit에 포함된 원본은 `artifact_sha256` 대신 `artifact_git`에 완전한 commit object ID를 넣습니다. 경로 제약, Git 검증 방식, record 수명주기는 설정 후 생성되는 [`knowledge/README.md`](src/didimlog/resources/project/README.md)의 해당 설치본에서 확인할 수 있습니다.

### index 다시 만들기

개인 지식 전체와 현재 Git 프로젝트의 index를 다시 만듭니다.

```sh
didim index
```

개인 index는 다음 Markdown만 원본으로 처리합니다.

```text
lessons/<project>/*.md
docs/<project>/**/*.md
book/<project>/*.md
```

`.DS_Store`, 이미지, 편집기 임시 파일처럼 이 패턴에 들지 않는 항목은 무시합니다. `lessons/<project>`, `docs/<project>`, `book/<project>` 위치의 프로젝트 디렉터리는 한 단계 symlink로 외부 실제 디렉터리를 가리킬 수 있습니다.

```text
lessons/my-project -> /path/to/external-lessons
```

개별 Markdown 파일과 프로젝트 내부 디렉터리의 symlink는 거부합니다. 외부 프로젝트에서 읽거나 쓴 파일도 index와 CLI에는 `lessons/my-project/...` 같은 논리 경로로 표시됩니다. 원본 형식이 잘못되면 `didim --explain-errors index`가 `무엇:`에 논리 경로를, `이유:`에 실패 원인을 표시합니다.

파일을 바꾸지 않고 원문과 index가 일치하는지만 검사하려면 다음 명령을 사용합니다.

```sh
didim index --check
```

두 index가 최신이면 exit `0`과 다음 token을 반환합니다.

```text
개인 지식: PERSONAL_INDEX_CURRENT
프로젝트 근거: PROJECT_INDEX_CURRENT
```

### 문제 진단하기

```sh
didim status
didim doctor
```

`status`는 버전, 개인 지식, 현재 프로젝트, 프로젝트 근거 자료, Claude 연결을 요약합니다. `doctor`는 발견한 문제가 미치는 영향과 다음 실행 명령을 함께 보여 줍니다.

자동화 로그에서도 오류 설명이 필요하면 전역 옵션을 명령 앞에 붙입니다.

```sh
didim --explain-errors index --check
```

## 명령 요약

다음 표는 사용자가 직접 실행하는 명령의 요약입니다. 모든 옵션은 설치된 버전의 `didim <command> --help`에서 확인할 수 있습니다.

| 명령 | 결과 | 주요 옵션 또는 입력 |
| --- | --- | --- |
| `didim setup` | 개인·프로젝트 저장 공간과 Claude 연결 준비 | `--dry-run`, `--yes`, `--skip-claude`, `--project-knowledge local\|shared`, `--config-dir` |
| `didim connect claude` | Claude Code 연결 추가 | `--yes`, `--config-dir` |
| `didim disconnect claude` | Didimlog가 관리하는 Claude 연결 제거 | `--config-dir` |
| `didim add lesson <slug>` | 개인 교훈을 create-only로 저장 | Markdown stdin, `--date`, `--project`, `--global` |
| `didim add observation` | 프로젝트 관찰 기록 저장 | JSON stdin, 공통 record 옵션 |
| `didim add experiment` | 프로젝트 실험 기록 저장 | JSON stdin, 공통 record 옵션 |
| `didim add evidence` | 프로젝트 근거 자료와 원본 결합 | JSON stdin, 공통 record 옵션 |
| `didim index` | 개인·프로젝트 index 재생성 | `--check` |
| `didim status` | 현재 상태 요약 | `--config-dir` |
| `didim doctor` | 문제와 수정 방법 진단 | `--config-dir` |

전역 옵션은 `--version`과 `--explain-errors`입니다. `didim hook session-start`는 Claude Code 연결이 사용하는 내부 명령입니다.

### 공통 record 옵션

`observation`, `experiment`, `evidence`는 다음 옵션을 공유합니다.

| 옵션 | 의미 |
| --- | --- |
| `--date YYYY-MM-DD` | 생성 날짜. 표준 입력을 사용하는 비대화형 실행에서는 필수 |
| `--title` | record 제목. 필수 |
| `--scope` | `project` 또는 `task:<name>`. 기본값 `project` |
| `--tags` | 쉼표로 구분한 태그 |
| `--sources` | 쉼표로 구분한 EVD 또는 EXP ID |

JSON stdin의 정확한 필드는 다음과 같습니다. 문자열이 아닌 값과 알 수 없는 필드는 거부됩니다.

| 유형 | 필수 필드 |
| --- | --- |
| observation | `body` |
| experiment | `hypothesis`, `method`, `result`, `contradicts`, `interpretation` |
| evidence | `artifact`, `origin`, `collection`과 `artifact_sha256` 또는 `artifact_git` 중 정확히 하나 |

CLI 표준 입력의 상한은 64 KiB입니다. 생성된 프로젝트 record는 최대 12,000 UTF-8 bytes와 200 LF이며, 더 자세한 형식과 검증 규칙은 설치된 프로젝트의 `knowledge/README.md`와 `knowledge/schema/record.schema.json`이 기준입니다.

### 오류와 종료 코드

오류 첫 줄의 영문 token과 종료 코드는 자동화에서 사용할 수 있는 안정된 동작입니다. 실제 터미널이나 `--explain-errors` 사용 시 다음 줄에 한국어 `도움말:`이 추가됩니다.

| 종료 코드 | 의미 |
| --- | --- |
| `2` | 명령 사용법 또는 입력 형식 오류 |
| `3` | 경로, 원본, index 등 지식 무결성 정책 위반 |
| `5` | 교훈에서 비밀값으로 보이는 내용 발견 |
| `7` | Git 저장소나 object를 검증할 수 없음 |

`LESSON_SECRET`은 값을 출력하지 않고 교훈 저장을 거부합니다. `KNOWLEDGE_INDEX_STALE` 또는 `PROJECT_INDEX_STALE`은 원문 저장에는 성공했지만 index 갱신이 실패했다는 뜻이므로 `didim index`로 복구합니다.

## 저장 방식과 안전 규칙

개인 지식은 홈 디렉터리에 저장됩니다.

```text
~/knowledge/
├── lessons/<project>/
├── docs/<project>/
├── book/<project>/
├── lessons/_global/
├── docs/_global/
├── book/_global/
└── index/<project>.md
```

- `lessons`: 다음 작업에서 행동을 바꿀 검증된 교훈
- `docs`: 정확한 절차, 계획, 조사 결과
- `book`: 여러 자료의 배경과 사례를 엮은 해설
- `_global`: 여러 프로젝트에 그대로 적용되는 자료
- `index`: 제목, 찾을 때, 상세 파일 경로만 담은 재생성 가능한 목록

프로젝트 지식은 Git 최상위 디렉터리에 저장됩니다.

```text
<git-root>/knowledge/
├── POINTER.md
├── README.md
├── records/
├── raw/
├── schema/record.schema.json
├── index/INDEX.md
└── active/harness.md
```

`records/`가 프로젝트 지식의 원본이며 `INDEX.md`는 전체 record를 다시 검증해 재생성할 수 있습니다. 기본 `local` 설정은 `.gitignore`를 바꾸지 않고 현재 로컬 저장소의 `info/exclude`에 `/knowledge/`를 추가합니다. 연결된 작업 트리(linked worktree)는 이 파일을 공유하므로 한 작업 트리의 설정이 다른 연결된 작업 트리에도 적용됩니다.

Didimlog는 다음 안전 규칙을 적용합니다.

- 교훈과 record 원문은 create-only이며 기존 원문을 덮어쓰거나 삭제하지 않습니다.
- 설정 전에 전체 변경 계획을 검사하고, 승인 뒤 경로나 파일이 바뀌면 쓰기를 중단합니다.
- symlink와 상위 경로 탈출을 거부합니다.
- 설정 도중 실패하면 Didimlog가 이번 실행에서 만든 내용 중 그대로 남아 있는 것만 되돌립니다. 동시에 저장된 사용자 변경은 건드리지 않습니다.
- 기록 파일 생성 뒤 index 갱신만 실패하면 원문은 보존하고 복구 명령을 안내합니다.
- Git object 근거 자료는 명령 시작 시 확인한 저장소의 object database에서만 검증합니다. 현재 shell의 Git 환경 변수나 외부 alternates에서 다른 object를 찾지 않습니다.

## Claude Code 연결

`didim setup --yes`는 저장 공간과 Claude Code 연결을 함께 준비합니다. 저장 공간만 준비하려면 다음 명령을 사용합니다.

```sh
didim setup --yes --skip-claude
```

연결만 따로 관리할 수도 있습니다.

```sh
didim connect claude --yes
didim disconnect claude
```

연결된 새 Claude 세션은 `KNOWLEDGE_USAGE.md`의 짧은 조회 절차만 자동으로 읽습니다. 실제 작업에서는 현재 프로젝트와 `_global` index를 검색하고 관련 상세 자료를 최대 5건 읽습니다. 전체 index나 모든 교훈 본문을 세션 시작 context에 싣지 않습니다.

## 제거

먼저 Claude Code 연결을 해제한 뒤 도구를 제거합니다.

```sh
didim disconnect claude
uv tool uninstall didimlog
```

제거해도 `~/knowledge`와 각 프로젝트의 `knowledge/` 원문은 남습니다.

## 개발과 검증

고정된 의존성을 설치하고 전체 test suite를 실행합니다.

```sh
uv sync --locked
uv run --project . python -m unittest discover -s tests -v
```

배포 파일과 공개 allowlist를 확인합니다.

```sh
uv build
uv run --project . python -m unittest tests.didimlog_tests.test_release -v
```

### 릴리스

릴리스 자동화를 사용하기 전에 저장소 관리자가 다음 설정을 직접 적용해야 합니다. workflow 파일만 병합해도 이 설정은 생기지 않습니다.

- GitHub Actions의 `Workflow permissions`를 `Read and write permissions`로 설정합니다.
- `release:none`, `release:patch`, `release:minor`, `release:major`, `release:ready` 레이블을 만듭니다.
- `main`은 PR, CI, `release-state` 통과를 필수로 설정하고, PR 브랜치가 최신 `main`을 반영해야 병합할 수 있도록 설정합니다. 병합 방식은 merge commit만 허용하고 squash, rebase, direct push는 막습니다.
- `develop`에는 GitHub Actions가 준비 커밋과 취소 커밋을 push할 수 있게 둡니다.
- `pypi` environment(배포 환경)를 만들고, 완전 자동 배포를 원하면 필수 승인자를 두지 않습니다.
- PyPI Trusted Publisher에 소유자 `zhsks311`, 저장소 `didimlog`, workflow `release.yml`, environment `pypi`를 등록합니다.
- GitHub Release immutability(공개 후 수정 금지)를 켭니다.

`develop` → `main` PR에 선택 레이블이 없거나 `release:none`이면 배포하지 않습니다. 배포하려면 다음 버전 레이블 중 하나만 붙입니다.

- `release:patch`: 버그 수정 버전 준비
- `release:minor`: 하위 호환 기능 버전 준비
- `release:major`: 호환성이 깨지는 버전 준비

자동화는 `pyproject.toml`, `uv.lock`, `CHANGELOG.md`를 갱신하고 전체 테스트와 빌드를 실행합니다. 준비가 끝나면 `release:ready`를 붙이지만, 이 레이블은 현재 상태를 보여 주는 표시입니다. 실제 필수 병합 기준은 현재 PR 커밋의 Git 이력에 유효한 준비 기록이 남아 있고, 바로 그 커밋에 대한 `release-state` 검사가 통과하는 것입니다.

준비 뒤 PR에 커밋을 추가하면 자동화가 이전 준비를 취소하고 새 커밋 기준으로 다시 준비합니다. 그사이 `main`이 전진하면 이전 준비를 취소하고 PR 브랜치가 최신 `main`을 반영할 때까지 기다립니다. 반영 뒤 자동으로 다시 준비합니다.

`main` 병합은 두 부모를 가진 merge commit만 배포 대상으로 인식합니다. squash, rebase, direct push는 배포하지 않고 릴리스 workflow를 오류로 끝냅니다.

`hotfix/*` → `main` PR은 `release:patch`만 지원합니다. patch 배포에 성공하면 `main`을 `develop`에 직접 합칩니다. 보호 규칙이나 충돌로 직접 반영할 수 없으면 `main` → `develop` 동기화 PR을 만들거나 갱신합니다.

기여 방법은 [`CONTRIBUTING.md`](CONTRIBUTING.md), 보안 제보 방법은 [`SECURITY.md`](SECURITY.md), 사용자에게 보이는 변경은 [`CHANGELOG.md`](CHANGELOG.md)에서 확인할 수 있습니다.

## 라이선스

Didimlog는 [MIT License](LICENSE)로 배포됩니다. Python-Markdown과 vendored Mermaid의 라이선스는 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)에 있습니다.
