# Didimlog

Didimlog는 AI와 함께 일하며 확인한 사실, 실험 결과, 재사용할 교훈을 로컬 파일로 남기고 필요한 순간에만 다시 찾게 하는 CLI입니다.

- 개인 지식은 `~/knowledge`에 프로젝트별로 나눠 보존합니다.
- 프로젝트 지식은 Git 저장소의 `knowledge/records`에 observation, experiment, evidence로 보존합니다.
- Claude Code에는 전체 본문 대신 짧은 조회 지침만 연결합니다.
- 기존 원문은 덮어쓰거나 삭제하지 않습니다.

현재 지원 범위는 macOS·Linux와 Python 3.11~3.14입니다. Windows 지원은 아직 검증하지 않았습니다.

## 설치

권장 설치 방법:

```sh
uv tool install didimlog
didim --version
```

`pipx`를 사용해도 됩니다.

```sh
pipx install didimlog
```

## 처음 설정

먼저 변경될 파일을 확인합니다.

```sh
didim setup --dry-run
```

출력된 계획이 맞으면 승인합니다.

```sh
didim setup --yes
```

`setup`은 다음 작업을 순서대로 수행합니다.

1. `~/knowledge`에 개인 지식 디렉터리와 검색용 index를 준비합니다.
2. 현재 위치가 Git 저장소라면 프로젝트 `knowledge/` scaffold와 index를 준비합니다.
3. Claude Code 설정에 Didimlog가 관리하는 조회 지침과 SessionStart hook을 연결합니다.
4. 실제 파일과 연결 상태를 다시 검사합니다.

사용자가 직접 작성한 `~/knowledge`, 프로젝트 `knowledge/`, Claude 설정의 사용자 소유 본문은 덮어쓰지 않습니다. 기존 파일과 충돌하거나 symlink·path escape가 발견되면 쓰기 전에 중단합니다.

Claude Code를 연결하지 않고 저장 공간만 준비하려면 다음 명령을 사용합니다.

```sh
didim setup --yes --skip-claude
```

## 저장 구조

개인 지식:

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

프로젝트 지식:

```text
<git-root>/knowledge/
├── POINTER.md
├── README.md
├── records/
├── raw/
├── index/INDEX.md
└── active/harness.md
```

`records/`가 프로젝트 지식의 유일한 원본입니다. `INDEX.md`는 모든 record를 검증한 뒤 다시 만들 수 있는 9-field TSV 색인입니다. `active/harness.md`는 v0.0.1에서 header-only이며 Didimlog가 승격 규칙을 쓰지 않습니다.

## 일상 사용

### 개인 교훈 저장

입력 형식과 필수 옵션은 명령의 도움말에서 확인합니다.

```sh
didim add lesson --help
```

lesson은 create-only로 저장됩니다. 같은 이름의 파일이 이미 있으면 기존 파일을 바꾸지 않고 실패합니다. 저장에 성공하면 개인 index를 다시 만듭니다.

### 프로젝트 관찰·실험·증거 저장

```sh
didim add observation --help
didim add experiment --help
didim add evidence --help
```

- observation: 실제로 관찰한 재사용 가능한 사실
- experiment: 가설, 방법, 결과, 해석을 함께 남기는 실험
- evidence: SHA-256 또는 검증된 Git object에 결합한 원자료

ID는 `OBS-YYYYMMDD-NN`, `EXP-YYYYMMDD-NN`, `EVD-YYYYMMDD-NN` 형식으로 Didimlog가 할당합니다. 기록 파일을 만든 뒤 project index 갱신에 실패해도 새 원문은 지우지 않고 복구 명령을 출력합니다.

### index 다시 만들기

개인 지식 전체와 현재 Git 프로젝트의 index를 다시 만듭니다.

```sh
didim index
```

파일을 바꾸지 않고 현재 상태만 검사할 수 있습니다.

```sh
didim index --check
```

### 상태와 문제 해결

```sh
didim status
didim doctor
```

`status`는 버전, 개인 index, 현재 프로젝트, Claude 연결, legacy 설치 흔적을 한 화면에 요약합니다. `doctor`는 문제가 미치는 영향과 다음 실행 명령을 함께 보여 줍니다.

오류 첫 줄의 영문 token과 exit code는 자동화 계약입니다. 실제 TTY에서는 다음 줄에 한국어 `도움말:`이 표시됩니다. non-TTY 로그에서도 설명이 필요하면 전역 옵션을 사용합니다.

```sh
didim --explain-errors index --check
```

## Claude Code 연결

`didim setup --yes`가 연결까지 처리합니다. 연결만 따로 관리할 수도 있습니다.

```sh
didim connect claude
didim disconnect claude
```

연결된 새 Claude 세션은 `KNOWLEDGE_USAGE.md`의 짧은 절차만 자동으로 읽습니다. 실제 작업에서는 현재 프로젝트와 `_global` index를 검색하고, 관련 상세 자료만 최대 5건 읽습니다. 전체 index나 모든 lesson 본문을 시작 context에 싣지 않습니다.

## 제거

먼저 Claude Code 연결을 해제한 뒤 도구를 제거합니다.

```sh
didim disconnect claude
uv tool uninstall didimlog
```

제거해도 `~/knowledge`와 각 프로젝트의 `knowledge/` 원문은 남습니다.

## 개발과 검증

고정된 의존성을 설치하고 전체 suite를 실행합니다.

```sh
uv sync --locked
uv run --project . python -m unittest discover -s tests -v
```

배포 파일과 공개 allowlist를 확인합니다.

```sh
uv build
uv run --project . python -m unittest tests.didimlog_tests.test_release -v
```

기여 방법은 [`CONTRIBUTING.md`](CONTRIBUTING.md), 보안 제보 방법은 [`SECURITY.md`](SECURITY.md), 사용자-visible 변경은 [`CHANGELOG.md`](CHANGELOG.md)에서 확인할 수 있습니다.

## 라이선스

Didimlog는 MIT License로 배포됩니다. Python-Markdown과 vendored Mermaid의 라이선스는 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)에 있습니다.
