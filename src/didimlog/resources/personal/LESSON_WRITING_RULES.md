# 검증된 교훈을 남기는 방법

작업 중 얻은 내용은 바로 저장하지 않고 후보로 보류한다. 실제 작업과 검증을 마친 뒤 다음 조건을 모두 만족할 때만 lesson으로 남긴다.

1. 다음 유사 작업에서 구체적인 행동을 바꾼다.
2. 사용자 교정, 테스트, 실행, 공식 문서 또는 저장소 근거가 있다.
3. 기존 lesson이나 프로젝트 문서와 중복되지 않는다.
4. 적용 프로젝트가 명확하다. 여러 프로젝트에 그대로 적용될 때만 `_global`을 명시한다.
5. 키·토큰·비밀번호·이메일·원문 절대경로·내부 URL을 포함하지 않는다.

lesson은 `lessons/<project>/<slug>.md`에 한 파일당 한 교훈으로 저장한다. 신규 lesson frontmatter는 다음을 사용한다.

```yaml
---
topic: portable-slug
title: 한 줄 제목
summary: 한 줄 요약
tags: [canonical, sorted]
date: YYYY-MM-DD
review_by: YYYY-MM-DD
---
```
`title`은 제어문자 없는 1~120자 한 줄입니다. `topic`과 각 `tags` 항목은 1~32자이면서 UTF-8 96 bytes 이하여야 하고, `tags`는 최대 20개를 UTF-8 byte 순으로 중복 없이 정렬합니다.


본문은 `## 상황`, `## 교훈`, `## 근거`를 포함한다. 실패·반증 결과도 지우지 않는다.

저장 명령:

```sh
didim add lesson <slug> --project <project> < lesson.md
```

`--project`를 생략하면 현재 Git 최상위 디렉터리 이름을 사용한다. `_global`은 반드시 명시한다. 저장 성공 뒤 지식 목록 생성이 실패하면 lesson은 보존되고 `KNOWLEDGE_INDEX_STALE`이 출력된다. 이때 다음 명령으로 복구한다.

```sh
didim index
```
