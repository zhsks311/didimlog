# 변경 이력

이 문서는 Didimlog의 사용자에게 보이는 변경을 기록합니다. 형식은 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/)를 따르고, 버전은 [Semantic Versioning](https://semver.org/lang/ko/)을 따릅니다.

## [Unreleased]

### 추가

- `status`가 현재 사용 중인 Claude 프로필 이름을 표시합니다. 프로필을 여러 개 쓸 때 어디에 연결됐는지 바로 알 수 있습니다.
- `doctor`가 먼저 고칠 원인, 그 원인 때문에 나타난 증상, 별도로 고칠 문제를 구분해 보여 줍니다. 아직 설정하지 않은 Git 프로젝트는 실패시키지 않고 선택 가능한 설정 방법을 안내합니다.

### 수정

- Claude 설정 파일이 다른 프로필을 가리키는 링크일 때, `status`와 `doctor`가 모든 문제를 하나로 뭉개 "설정을 읽을 수 없다"고만 알리고 실행해도 실패하는 `didim setup`을 안내하던 문제를 고쳤습니다. 이제 어떤 파일이 왜 막혔는지 각각 알려 주고, 실제로 그 파일을 가진 프로필에 연결하는 `CLAUDE_CONFIG_DIR=~/<프로필> didim setup` 명령을 그대로 보여 줍니다.
- 링크된 파일 하나 때문에 나머지 진단이 통째로 중단되던 문제를 고쳐, SessionStart 확인과 지식 사용 지침의 상태를 함께 볼 수 있습니다.
- 파일마다 다른 프로필을 가리켜 한 번에 고칠 수 없을 때, `doctor`가 몇 가지를 모두 고쳐야 하는지 먼저 알리고 세션 시작 안내는 `didim doctor`로 넘깁니다. 이전에는 그중 한 명령만 보여 주어 그것만 실행하면 끝나는 것처럼 보였습니다.
- 다른 Didimlog 실행이 개인 지식을 사용 중이라 상태를 확인하지 못한 경우를 원본 오류와 구분합니다. 고칠 파일이 없으므로 `didim index` 대신 잠시 뒤 다시 확인하도록 안내합니다.

## [0.0.5] - 2026-08-19

### 변경

- 개인 지식을 현재 Git 프로젝트뿐 아니라 사용자가 지정한 이름의 공간으로 묶어 저장·조회할 수 있음을 명확히 했습니다. 공간을 명시하면 현재 프로젝트보다 우선하며, 명시한 공간이 없더라도 현재 프로젝트로 바꾸지 않습니다.

## [0.0.4] - 2026-08-18

### 수정

- 배포 산출물 폴더에 `uv`가 만드는 `.gitignore`를 패키지 파일로 잘못 세어 배포가 중단되던 문제를 고쳤습니다. 실패한 배포는 유효한 `main` merge commit SHA를 지정해 같은 버전으로 안전하게 다시 실행할 수 있습니다.

## [0.0.3] - 2026-08-14

### 추가

- 릴리스 PR별로 준비·취소 기록을 Git 이력에 남기고, 취소가 먼저 오든 병합이 먼저 오든 같은 배포 여부를 결정하도록 바꿨습니다. `main`이 전진하면 열려 있는 여러 릴리스 PR을 최신 기준으로 다시 계산합니다. 준비 뒤 릴리스 파일을 수정해도 취소 과정은 후속 변경을 보존하며, 여러 hotfix 동기화는 한 번에 하나씩 처리합니다. `hotfix/*`의 patch 배포 뒤에는 `main`을 `develop`에 직접 합치고, 보호 규칙이나 충돌로 막히면 `main` → `develop` 동기화 PR을 만들거나 갱신합니다.

### 수정

- 개인 지식 폴더의 `.DS_Store`와 이미지 같은 관련 없는 파일 때문에 전체 index 생성이 실패하던 문제를 고쳤습니다. 프로젝트별 lesson·docs·book 디렉터리를 외부 저장 위치에 연결해도 읽기와 쓰기를 지원하며, 잘못된 Markdown은 논리 경로와 원인을 표시합니다.
- 개인 검색 목록과 책 HTML은 완성된 생성 결과를 한 방향으로 교체합니다. 이전 버전이 남긴 복구 파일은 생성기가 만든 파일임을 확인한 경우에만 자동 정리하며, 사용자가 만든 파일은 보존하고 오류로 알립니다.

## [0.0.2] - 2026-08-12

### 변경

- 프로젝트 evidence 경로를 `knowledge/raw/` 아래로 제한하고 scaffold 문서를 현재 CLI 기준으로 정리했습니다.
- 프로젝트 record와 개인 lesson·index를 같은 source snapshot 잠금 안에서 처리합니다.
- `setup`은 프로젝트 `knowledge/`를 기본적으로 로컬 Git 저장소의 `info/exclude`로 추적에서 제외하고, 팀 공유가 필요하면 `didim setup --project-knowledge shared`로 선택하게 바꿨습니다.
- 설치되는 프로젝트 안내를 실제 `didim add` 동작에 맞춰 ID 자동 할당과 experiment의 `contradicts` 입력 방식을 명확히 했습니다.

### 수정

- 경로를 검사한 뒤 파일이 교체되는 경쟁과 부분 record가 최종 파일명으로 노출될 수 있는 문제를 막았습니다.
- 조회 A/B의 각 case가 독립된 `HOME`과 Claude 설정을 사용하고, Claude 자식 프로세스에는 검증된 OAuth access token과 실행에 필요한 환경만 전달하도록 격리했습니다.
- 비밀값이 포함된 lesson은 값을 출력하지 않고 `LESSON_SECRET`과 exit `5`로 거부합니다.
- `disconnect`가 관리 문서를 복원하지 못해도 최상위 Claude 설정 rollback을 계속 수행하도록 수정했습니다.
- 배포 전에 `SHA256SUMS`가 wheel과 sdist의 정확한 두 항목만 엄격한 형식으로 포함하는지 확인하도록 강화했습니다.
- v0.0.1의 기존 프로젝트 안내 파일만 현재 버전으로 안전하게 갱신해 `didim setup`과 `didim add`를 다시 실행할 수 있게 했습니다.
- 패키지 관리자가 만든 심볼릭 링크를 따라 실제 `didim` 실행 파일을 확인해 `setup`과 Claude 연결이 정상 동작하도록 수정했습니다.

## [0.0.1] - 2026-08-05

### 추가

- 프로젝트별 개인 지식과 `_global` 지식을 분리해 저장하고 색인하는 `didim` CLI.
- 프로젝트 Knowledge Harness의 observation, experiment, evidence 기록과 결정론적 9-field 색인.
- Claude Code에 조회 지침만 연결하고 관련 상세 자료를 최대 5건 읽게 하는 설정.
- create-only 원문 저장, symlink/path escape 차단, 조건부 rollback과 상태 진단.
- macOS·Linux 및 Python 3.11~3.14 지원 계약.


[Unreleased]: https://github.com/zhsks311/didimlog/compare/v0.0.5...HEAD
[0.0.5]: https://github.com/zhsks311/didimlog/releases/tag/v0.0.5
[0.0.4]: https://github.com/zhsks311/didimlog/releases/tag/v0.0.4
[0.0.3]: https://github.com/zhsks311/didimlog/releases/tag/v0.0.3
[0.0.2]: https://github.com/zhsks311/didimlog/releases/tag/v0.0.2
[0.0.1]: https://github.com/zhsks311/didimlog/releases/tag/v0.0.1
