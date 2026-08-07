# 보안 정책

## 지원 버전

공개된 최신 patch 버전만 보안 수정을 받습니다. 아직 공개되지 않은 개발 snapshot은 지원 버전이 아닙니다.

## 취약점 제보

공개 issue에 취약점, 개인 자료, 홈 경로, token을 올리지 마세요. GitHub 저장소의 **Security → Report a vulnerability**에서 비공개 security advisory를 작성해 주세요.

제보에는 다음 정보만 포함합니다.

- 영향을 받는 Didimlog 버전과 운영체제
- 재현에 필요한 최소 단계
- 예상 영향
- 비밀정보를 제거한 로그나 예제 경로

유지관리자는 제보를 확인한 뒤 영향과 수정 계획을 security advisory에서 답합니다. 수정이 공개되기 전에는 취약점 세부 내용을 공개하지 않습니다.

## 범위

다음 경계의 우회는 보안 문제로 취급합니다.

- `~/knowledge` 또는 프로젝트 `knowledge/` 밖의 파일 쓰기
- symlink·path escape를 통한 읽기나 쓰기
- 기존 사용자 원문 덮어쓰기 또는 삭제
- Claude 설정의 사용자 소유 구간 변경
- artifact digest 또는 Git object 검증 우회
- 로그·상태 출력의 비밀정보 노출
