# Didimlog에 기여하기

## 시작하기

Didimlog은 Python 3.11~3.14와 `uv`를 사용합니다. 저장소를 복제한 뒤 의존성을 고정된 버전으로 준비합니다.

```sh
uv sync --locked
uv run --project . didim --help
```

## 변경 원칙

- 사용자 원문은 create-only로 다루고 기존 lesson·record를 덮어쓰지 않습니다.
- 개인 지식과 프로젝트 지식의 parser·schema·index 계약을 하나씩만 유지합니다.
- 오류 첫 줄의 영문 token과 exit code는 자동화 계약입니다.
- 기능이나 버그 수정은 관찰 가능한 계약을 먼저 실패하는 테스트로 고정합니다.
- 계획, 개인 자료, 실행 artifact, 절대 홈 경로를 공개 변경에 넣지 않습니다.

## 검증

변경 범위의 focused test를 먼저 실행한 뒤 전체 suite와 package 검사를 실행합니다.

```sh
uv run --project . python -m unittest discover -s tests -v
uv build
uv run --project . python -m unittest tests.didimlog_tests.test_release -v
```

## Pull request

일반 변경의 대상 브랜치는 `develop`입니다. 한 PR에는 한 목적만 담고, 사용자-visible 변경은 `CHANGELOG.md`에 기록합니다. `develop`에서 검증된 release 후보만 promotion PR로 `main`에 병합합니다.
