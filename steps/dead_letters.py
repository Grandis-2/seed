"""preorder 수신 DLQ(preorder_dead_letter_events) 5건 — 관리자 DLQ 화면용. 모두 OPEN(되돌리기 · 버리기 전)이다.

외부 등록 DLQ(작업 DEAD_LETTER, preorders 단계)와는 다른 테이블이다. 이쪽은 preorder 가 큐에서 받다가 5번 실패한 메시지다.
  PROCESSING_FAILED 3   만료 요청. 예약이 이미 취소됐으므로 되돌리면 "만료 대상 아님" 으로 끝나 SUCCEEDED 가 된다
  UNKNOWN_EVENT_TYPE 1  받지 않는 종류 — 되돌릴 수 없다
  UNREADABLE_BODY 1     JSON 이 깨진 원문 — 되돌릴 수 없다
봉투(body)는 EventEnvelope 모양이다(aggregateId 는 예약 id 의 UUID 문자열). 검색 컬럼은 DeadLetterBodyParser 가 봉투에서 읽는 값과 같게 채운다.
"""
import json
from datetime import datetime, timedelta

from .common import insert, uuid7, uuid_of
from .preorders import Plan

SOURCE_QUEUE = 'preorder-events'
RECEIVE_COUNT = 5  # SQS maxReceiveCount


def seed(cur, now: datetime, plans: list[Plan]) -> None:
    by_n = {p.n: p for p in plans}
    # (계획 번호, 이벤트 종류, 실패 까닭, 보낸 시각)
    messages = [(1801 + k, 'PREORDER_EXPIRY_REQUESTED', 'PROCESSING_FAILED',
                 by_n[1801 + k].payable_from + timedelta(hours=24)) for k in range(3)]
    messages.append((2, 'PREORDER_PAYMENT_COMPLETED', 'UNKNOWN_EVENT_TYPE',
                     by_n[2].payable_from + timedelta(minutes=30)))

    rows = []
    for n, event_type, reason, sent_at in messages:
        p = by_n[n]
        event_id = uuid_of('dlq-event', n)
        body = {'eventId': event_id, 'eventType': event_type, 'aggregateType': 'PREORDER', 'aggregateId': str(p.id),
                'occurredAt': sent_at.strftime('%Y-%m-%dT%H:%M:%S.%fZ'), 'payload': {'preorderId': p.token}}
        stored = sent_at + timedelta(minutes=10)
        rows.append((uuid7(stored, 'dead-letter', n).bytes, SOURCE_QUEUE, uuid_of('dlq-message', n), event_id,
                     event_type, 'PREORDER', p.id.bytes, p.id.bytes, p.customer.id.bytes, json.dumps(body), reason,
                     RECEIVE_COUNT, sent_at, 'OPEN', stored, stored))

    sent_at = now - timedelta(days=2)
    stored = sent_at + timedelta(minutes=10)
    rows.append((uuid7(stored, 'dead-letter', 'unreadable').bytes, SOURCE_QUEUE, uuid_of('dlq-message', 'unreadable'),
                 None, None, None, None, None, None, '{"eventId":"7f3c2a', 'UNREADABLE_BODY', RECEIVE_COUNT, sent_at,
                 'OPEN', stored, stored))

    insert(cur, 'preorder_dead_letter_events', ['id', 'source_queue', 'message_id', 'event_id', 'event_type',
                                                'aggregate_type', 'aggregate_id', 'preorder_id', 'customer_id', 'body',
                                                'failure_reason', 'receive_count', 'sent_at', 'status', 'created_at',
                                                'updated_at'], rows)
