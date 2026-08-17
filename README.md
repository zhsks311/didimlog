# Didimlog

Didimlog는 AI와 함께 일하며 확인한 사실, 실험 결과, 재사용할 교훈을 로컬 파일로 남기고 필요한 순간에만 다시 찾게 하는 CLI입니다.

- 개인 지식은 `~/knowledge`에 프로젝트별로 나눠 보존합니다.
- 프로젝트 지식은 프로젝트 디렉터리의 `knowledge/records`에 observation, experiment, evidence로 보존하며 기본적으로 Git 추적에서 제외합니다.
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
2. 현재 위치가 Git 저장소라면 프로젝트 `knowledge/` 폴더와 index를 준비합니다.
3. Claude Code 설정에 Didimlog가 관리하는 조회 지침과 SessionStart hook을 연결합니다.
4. 실제 파일과 연결 상태를 다시 검사합니다.

사용자가 직접 작성한 `~/knowledge`, 프로젝트 `knowledge/`, Claude 설정의 사용자 소유 본문은 덮어쓰지 않습니다. 기존 파일과 충돌하거나 symlink·path escape가 발견되면 쓰기를 중단합니다. 계획을 확인한 뒤 다른 프로세스가 파일을 바꾼 경우에도 작업을 중단하고, Didimlog가 만든 내용 중 그대로 남아 있는 것만 되돌립니다. 동시에 저장된 사용자 변경은 되돌리거나 덮어쓰지 않습니다.

프로젝트 `knowledge/`의 기본값은 로컬 전용입니다. `.gitignore`를 바꾸지 않고 현재 로컬 Git 저장소가 사용하는 `info/exclude` 파일(일반 저장소의 `.git/info/exclude`)에 `/knowledge/` 규칙을 추가합니다. 같은 로컬 저장소에 연결된 작업 트리(linked worktree)는 이 파일을 공유하므로 한 작업 트리의 설정이 다른 연결된 작업 트리에도 적용됩니다.

프로젝트 지식을 팀과 공유하려면 다음 명령으로 `shared`를 선택합니다.

```sh
didim setup --yes --project-knowledge shared
```

`shared`는 로컬 제외 설정에서 Didimlog 관리 표시로 둘러싼 블록만 제거하고 사용자 규칙은 바꾸지 않습니다. 같은 파일의 다른 규칙, `.gitignore`, 사용자의 전역 제외 설정 등이 `knowledge/`를 계속 제외하면 안내를 표시하므로, Git에 포함하려면 해당 규칙을 직접 바꿔야 합니다.

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
비밀값으로 보이는 내용은 저장하지 않으며, 값을 출력하지 않고 `LESSON_SECRET`
오류와 exit `5`를 반환합니다.

### 프로젝트 관찰·실험·증거 저장

```sh
didim add observation --help
didim add experiment --help
didim add evidence --help
```

- observation: 실제로 관찰한 재사용 가능한 사실
- experiment: 가설, 방법, 결과, 해석을 함께 남기는 실험
- evidence: SHA-256 또는 검증된 Git object에 결합한 원자료. Git object 방식은 현재 shell의 Git 환경 변수나 외부 alternates를 사용하지 않고, 명령을 시작할 때 확인한 저장소의 object database에서만 검증합니다. linked worktree에서는 공통 object database를 사용합니다.

ID는 `OBS-YYYYMMDD-NN`, `EXP-YYYYMMDD-NN`, `EVD-YYYYMMDD-NN` 형식으로 Didimlog가 할당합니다. 기록을 저장하는 동안 프로젝트 경로나 `knowledge/records`가 다른 디렉터리로 교체되면 새 경로에 기록하지 않고 중단합니다. 기록 파일이 원래 디렉터리에 완전히 만들어진 뒤 project index 갱신에 실패하면 원문은 보존하고 `PROJECT_INDEX_STALE: run didim index`를 출력합니다.

Git metadata나 object database가 검증 도중 바뀌어 같은 저장소라고 확정할 수 없으면 `GIT_UNVERIFIABLE <record-id>`로 중단합니다. 검증 실패를 성공으로 간주하거나 다른 저장소에서 object를 찾지 않습니다.

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

파일을 바꾸지 않고 현재 상태만 검사할 수 있습니다.

```sh
didim index --check
```

### 상태와 문제 해결

```sh
didim status
didim doctor
```

`status`는 버전, 개인 index, 현재 프로젝트, Claude 연결을 한 화면에 요약합니다. `doctor`는 문제가 미치는 영향과 다음 실행 명령을 함께 보여 줍니다.

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

### 릴리스

한 번만 다음 저장소 설정을 마칩니다.

- GitHub Actions의 `Workflow permissions`를 `Read and write permissions`로 설정합니다.
- `pypi` environment(배포 환경)를 만들고, 완전 자동 배포를 원하면 필수 승인자를 두지 않습니다.
- PyPI Trusted Publisher에 소유자 `zhsks311`, 저장소 `didimlog`, workflow `release.yml`, environment `pypi`를 등록합니다.
- GitHub Release immutability(공개 후 수정 금지)를 켭니다.
- `main`은 PR과 CI 통과를 필수로 하고, `develop`에는 GitHub Actions가 준비 커밋과 취소 커밋을 push할 수 있게 둡니다.

자동화가 `main`에 처음 반영되면 릴리스 선택 label이 생성됩니다. 이후 `develop` → `main` PR에서 다음 label 중 하나만 선택합니다.

- `release:none`: 배포 없이 병합
- `release:patch`: 버그 수정 버전 준비
- `release:minor`: 하위 호환 기능 버전 준비
- `release:major`: 호환성이 깨지는 버전 준비

버전 label을 붙이면 `pyproject.toml`, `uv.lock`, `CHANGELOG.md`가 자동으로 갱신되고 전체 테스트와 빌드가 실행됩니다. `release:ready`와 CI 통과를 확인한 뒤 PR을 병합하면 tag, 검증된 wheel·sdist·`SHA256SUMS`, 변경 이력, PyPI 배포가 차례로 생성됩니다. 병합 전에 버전 label을 제거하거나 `release:none`을 붙이면 준비 커밋을 revert하고 배포를 취소합니다. 강제 push는 사용하지 않습니다.

기여 방법은 [`CONTRIBUTING.md`](CONTRIBUTING.md), 보안 제보 방법은 [`SECURITY.md`](SECURITY.md), 사용자-visible 변경은 [`CHANGELOG.md`](CHANGELOG.md)에서 확인할 수 있습니다.

## 라이선스

Didimlog는 MIT License로 배포됩니다. Python-Markdown과 vendored Mermaid의 라이선스는 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)에 있습니다.
