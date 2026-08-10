---
topic: redis
title: 캐시 TTL 없으면 메모리 누수
summary: TTL 없는 캐시 키는 영구 잔존해 used_memory를 밀어올린다
tags: [cache, redis]
date: 2026-07-20
review_by: 2026-01-01
---
## 상황
캐시 매니저 리뷰.
## 교훈
캐시는 반드시 TTL. 영구 보관은 근거를 남긴다.
## 근거
eviction 급증 관찰.
