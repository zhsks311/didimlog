# 변경 이력

이 문서는 Didimlog의 사용자에게 보이는 변경을 기록합니다. 형식은 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/)를 따르고, 버전은 [Semantic Versioning](https://semver.org/lang/ko/)을 따릅니다.

## [Unreleased]

### 추가

- `status`가 현재 사용 중인 Claude 프로필 이름을 표시합니다. 프로필을 여러 개 쓸 때 어디에 연결됐는지 바로 알 수 있습니다.
- `doctor`가 먼저 고칠 원인, 그 원인 때문에 나타난 증상, 별도로 고칠 문제를 구분해 보여 줍니다. 아직 설정하지 않은 Git 프로젝트는 실패시키지 않고 선택 가능한 설정 방법을 안내합니다.

### 수정

- Claude 설정 파일이 다른 프로필을 가리키는 링크일 때, `status`와 `doctor`가 모든 문제를 하나로 뭉개 "설정을 읽을 수 없다"고만 알리고 실행해도 실패하는 `didim setup`을 안내하던 문제를 고쳤습니다. 이제 어떤 파일이 왜 막혔는지 각각 알려 주고, 실제로 그 파일을 가진 프로필에 연결하는 `CLAUDE_CONFIG_DIR=~/<프로필> didim setup` 명령을 그대로 보여 줍니다.
- 링크된 파일 하나 때문에 나머지 진단이 통째로 중단되던 문제를 고쳐, SessionStart 확인과 지식 사용 지침의 상태를 함께 볼 수 있습니다.

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


[Unreleased]: https://github.com/zhsks311/didimlog/compare/v0.0.2...HEAD
[0.0.2]: https://github.com/zhsks311/didimlog/releases/tag/v0.0.2
[0.0.1]: https://github.com/zhsks311/didimlog/releases/tag/v0.0.1
