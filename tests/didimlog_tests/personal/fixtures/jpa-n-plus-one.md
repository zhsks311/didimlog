---
project: global
topic: jpa
title: ManyToOne 기본 EAGER는 항상 LAZY로 명시
summary: @ManyToOne/@OneToOne 기본 EAGER가 N+1과 예측불가 로딩을 부른다
tags: [jpa, performance]
date: 2026-07-15
---
## 상황
엔티티 매핑 리뷰.
## 교훈
연관관계는 LAZY 명시 + 필요시 fetch join.
## 근거
Hibernate 기본 EAGER 관찰.
