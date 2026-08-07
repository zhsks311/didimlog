---
project: demo-api
topic: kafka
title: Kafka 멱등성은 acks=all과 세트다
summary: enable.idempotence=true라도 acks≠all이면 client가 조용히 멱등성을 끈다
tags: [kafka, producer]
date: 2026-07-10
review_by: 2026-12-31
---
## 상황
프로듀서 설정 리뷰 중.
## 교훈
멱등성은 acks=all·retries>0와 함께여야 순서가 보장된다.
## 근거
kafka-clients 3.0+ silent fallback 확인.
