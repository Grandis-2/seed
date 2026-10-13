"""예약 묶음: 예약 · 이력 · 외부 등록 작업 · 시도 · 아웃박스, 그리고 결제한 예약의 주문.

한 예약이 "예약 · 작업 · 주문 · Mock 원장" 이 서로 맞는 한 묶음이다. 먼저 예약마다 계획(Plan)을 세우고 — 묶음 · 회원 ·
옵션 · 시각 — 모든 테이블이 그 계획을 읽는다. Mock 원장(external_mock 단계)도 같은 계획에서 만든다.
계획 번호(n)는 1~3,000 이 아이폰 18 Pro(순번 = n), 3,001~4,000 이 갤럭시 Z 폴드8(순번 = n − 3,000)이다. id(UUID v7)는 접수 시각으로 만든다.

  아이폰 18 Pro (마감, 3,000건)
    1~1,650     PAID             결제 완료(RESERVED) — 주문 AWAITING_CONFIRMATION
    1,651~1,800 UNPAID           결제 가능(REGISTERED, 기한 안) — 등록이 DLQ 에 갔다가 관리자 재처리(실행 회차 2)로 늦게 성공해
                                 1~21시간 전에 결제 가능
    1,801~2,250 EXPIRED          결제 기한 초과 취소(SYSTEM)
    2,251~2,450 CANCEL_REFUNDED  결제 후 사용자 취소 — 주문 CANCELED · 결제 REFUNDED
    2,451~2,850 CANCELED         결제 전 사용자 취소
    2,851~2,940 DEAD_LETTER      외부 등록이 세 세대(차례 12번)를 다 쓰고 실패 → 관리자 재처리 대기
    2,941~2,970 PENDING          등록 대기 — 아웃박스 미발행. 기동하면 릴레이가 발행한다
    2,971~3,000 LOST             등록 대기인데 Mock 원장에는 이미 있다(응답 유실 재현) — 보내면 Mock 이 201 로 재생한다
  갤럭시 Z 폴드8 (진행 중, 1,000건 · 다음 신청은 1,001번)
    1~450 PAID · 451~500 CANCELED · 501~800 UNPAID · 801~1,000 PENDING
    결제 가능이 24시간 결제 기한 안에 있어야 해서 늦은 순번(최근 접수)을 결제 가능 · 등록 대기로 두었다.

큐 안에 있어야 성립하는 상태(PROCESSING · RETRY_SCHEDULED · 진행 중 CANCELING)는 만들지 않는다.
DB 만 채우면 SQS 메시지가 없어 영원히 멈춘다.
이력은 상태가 바뀔 때만 남는다 — 결제 시작(주문 생성)은 payment_started_at 만 남기고 이력을 남기지 않는다(PreorderLedger).
"""
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import campaign
from .catalog import Catalog, Option
from .common import insert, seconds, uuid7, uuid_of
from .customers import Customer
from .orders import Event, Item, Order

PAID, UNPAID, EXPIRED = 'PAID', 'UNPAID', 'EXPIRED'
CANCEL_REFUNDED, CANCELED = 'CANCEL_REFUNDED', 'CANCELED'
DEAD_LETTER, PENDING, LOST = 'DEAD_LETTER', 'PENDING', 'LOST'

# 아이폰 18 Pro(1) · 갤럭시 Z 폴드8(2) 의 묶음: (마지막 순번, 묶음)
BUCKETS = {
    1: [(1650, PAID), (1800, UNPAID), (2250, EXPIRED), (2450, CANCEL_REFUNDED), (2850, CANCELED),
        (2940, DEAD_LETTER), (2970, PENDING), (3000, LOST)],
    2: [(450, PAID), (500, CANCELED), (800, UNPAID), (1000, PENDING)],
}

# 시연 계정은 묶음마다 하나씩 끼워 마이페이지에서 여러 상태를 보게 한다. {상품: {순번: 회원 번호}}
DEMO_SEATS = {
    1: {1: 1, 1651: 2, 2251: 3, 2851: 4},   # 결제 완료 · 결제 가능 · 결제 후 취소 · 등록 DLQ
    2: {1: 1, 451: 4, 501: 5, 801: 3},      # 결제 완료 · 결제 전 취소 · 결제 가능 · 등록 대기
}

USER_CANCEL_REASONS = ['단순 변심', None, '다른 색상으로 다시 신청할게요', '배송 일정이 맞지 않아요']

# Mock 이 등록을 확정한 뒤 be 가 성공을 받기까지. 확정 시각 = 결제 가능 시각 − 이 값
CONFIRM_LEAD = timedelta(milliseconds=400)
# worker 재시도(be worker nova.worker.retry 기본값): 한 세대에 차례 4번(차례 사이 10 · 20 · 40초), 세대 3번(세대 사이 15분).
# 차례 하나가 시도 기록 한 줄이다 — 차례 안에서 곧바로 한 번 더 보내는 것은 같은 줄에 들어간다. 세 세대를 다 쓰면 작업이 DEAD_LETTER 다
ROUND_BACKOFF = [seconds(10), seconds(20), seconds(40)]
GENERATIONS = 3
GENERATION_GAP = timedelta(minutes=15)
# 관리자 재처리를 누른 시각 — 재처리 실행의 첫 차례가 성공하기 1초 전
REPROCESS_LEAD = seconds(1)
# 결제하기를 눌러 주문이 생긴 시각 — 결제 확인 3분 전(주문 생성 = 결제 시작, payment_started_at)
ORDER_LEAD = timedelta(minutes=3)
# 발행 끝난 아웃박스 행은 7일 뒤 be 가 지운다(common:outbox 정리 작업, nova.outbox.retention 기본값)
OUTBOX_RETENTION = timedelta(days=7)
# 외부 등록 본문의 scope — be 설정 nova.external-mock.scope 의 기본값
EXTERNAL_SCOPE = 'preorder'
LOST_NOTE = '[시드] 응답 유실 재현 — Mock 원장에는 이미 등록돼 있고 be 는 아직 모른다'
INJECTED_500 = ('TRANSIENT_FAILURE', 500, 'UPSTREAM_UNAVAILABLE', '외부 시스템을 사용할 수 없습니다.')
NO_RESPONSE = ('UNKNOWN', None, None, '응답 없음(읽기 타임아웃 5초)')


@dataclass
class Plan:
    """예약 한 건의 계획. 시각이 비어 있으면 그 일이 일어나지 않았다는 뜻이다."""
    n: int
    product_no: int
    product_id: uuid.UUID
    position: int
    bucket: str
    customer: Customer
    option: Option
    batch_id: uuid.UUID
    token: str
    created_at: datetime
    payable_from: datetime | None = None
    paid_at: datetime | None = None
    cancel_actor: str | None = None
    cancel_reason: str | None = None
    cancel_start: datetime | None = None

    @property
    def id(self) -> uuid.UUID:
        return uuid7(self.created_at, 'preorder', self.n)

    @property
    def register_job_id(self) -> uuid.UUID:
        return uuid7(self.created_at, 'register-job', self.n)

    @property
    def cancel_job_id(self) -> uuid.UUID | None:
        return uuid7(self.cancel_job_created_at, 'cancel-job', self.n) if self.cancel_start else None

    @property
    def confirm_at(self) -> datetime | None:
        """Mock 이 등록을 확정한 시각. 외부 등록이 성공한 예약만 있다."""
        return self.payable_from - CONFIRM_LEAD if self.payable_from else None

    @property
    def external_number(self) -> str | None:
        """Mock 이 발급한 번호. Mock 형식(R-yyyyMMdd-10자리)이고 계획 번호라 겹치지 않는다."""
        return f'R-{self.confirm_at:%Y%m%d}-{self.n:010d}' if self.confirm_at else None

    @property
    def payment_started_at(self) -> datetime | None:
        return self.paid_at - ORDER_LEAD if self.paid_at else None

    @property
    def cancel_done(self) -> datetime | None:
        """취소 시작 → (주문 정리) → 외부 취소 작업 → 취소 완료까지 3초."""
        return self.cancel_start + seconds(3) if self.cancel_start else None

    @property
    def status(self) -> str:
        if self.cancel_start:
            return 'CANCELED'
        if self.paid_at:
            return 'RESERVED'
        return 'REGISTERED' if self.payable_from else 'PENDING_SYNC'

    @property
    def late_registered(self) -> bool:
        """아이폰 18 Pro 결제 가능 묶음 — 5회 실패로 DLQ 에 갔다가 관리자 재처리로 늦게 성공했다."""
        return self.product_no == 1 and self.bucket == UNPAID

    @property
    def exhausted_rounds(self) -> list[tuple]:
        """세 세대를 다 쓰고 실패한 첫 실행의 차례들: (세대, 차례, 결과, 시작, 끝). DLQ 묶음과 늦게 성공한 묶음만 있다.

        홀수 차례는 Mock 주입 500, 짝수 차례는 응답이 없어 결과를 모른다(worker 읽기 타임아웃 5초).
        """
        if not (self.bucket == DEAD_LETTER or self.late_registered):
            return []
        rounds, started = [], self.created_at + seconds(1)
        for generation in range(1, GENERATIONS + 1):
            for turn in range(1, len(ROUND_BACKOFF) + 2):
                finished = started + seconds(1 if turn % 2 else 5)
                rounds.append((generation, turn, INJECTED_500 if turn % 2 else NO_RESPONSE, started, finished))
                started = finished + (ROUND_BACKOFF[turn - 1] if turn <= len(ROUND_BACKOFF) else GENERATION_GAP)
        return rounds

    @property
    def dead_lettered_at(self) -> datetime | None:
        """지금 DEAD_LETTER 인 작업만 있다 — 관리자 재처리는 이 시각을 지운다(PreorderSyncJobRepository.reopen)."""
        return self.exhausted_rounds[-1][4] + seconds(1) if self.bucket == DEAD_LETTER else None

    @property
    def reprocessed_at(self) -> datetime | None:
        return self.confirm_at - REPROCESS_LEAD if self.late_registered else None

    @property
    def register_run(self) -> int:
        """등록 작업의 실행 회차. 관리자 재처리마다 1 씩 오른다"""
        return 2 if self.late_registered else 1

    @property
    def cancel_job_created_at(self) -> datetime:
        """외부 취소 작업은 주문 정리가 끝난 뒤 생긴다. 결제한 예약은 환불이 끝난 뒤다."""
        return self.cancel_start + seconds(2 if self.bucket == CANCEL_REFUNDED else 1)


def plan(now: datetime, catalog: Catalog, campaigns: dict[int, campaign.Campaign],
         customers: dict[int, Customer]) -> list[Plan]:
    plans = []
    for product_no, buckets in BUCKETS.items():
        opens = campaigns[product_no].opens_at
        start = 0 if product_no == 1 else 3000
        for position in range(1, buckets[-1][0] + 1):
            index = (position - 1) % 8  # 옵션은 8개(4색 × 2용량)를 돌아가며 고른다
            p = Plan(
                n=start + position,
                product_no=product_no,
                product_id=catalog.product_ids[product_no],
                position=position,
                bucket=next(b for last, b in buckets if position <= last),
                customer=customers[_customer(product_no, position)],
                option=catalog.picks[(product_no, index // 2, index % 2)],
                batch_id=campaigns[product_no].batch_id(position),
                token=uuid_of('preorder', start + position),
                created_at=_accepted_at(now, opens, product_no, position),
            )
            _schedule(p, now)
            plans.append(p)
    return plans


def _customer(product_no: int, position: int) -> int:
    """아이폰 18 Pro 는 서로 다른 3,000명(6~3,005). 갤럭시 Z 폴드8 은 앞 600명이 아이폰 18 Pro 에도 예약한 사람이고 나머지 400명은 3,006~3,405. 1인 1매는 모델마다다."""
    if position in DEMO_SEATS[product_no]:
        return DEMO_SEATS[product_no][position]
    if product_no == 1 or position <= 600:
        return position + 5
    return position + 2405


def _accepted_at(now: datetime, opens: datetime, product_no: int, position: int) -> datetime:
    """접수 시각. 아이폰 18 Pro 는 오픈 직후에 몰린다(순번^1.5 초, 마지막이 약 46시간 뒤).
    갤럭시 Z 폴드8 은 앞 500명이 오픈 후 8시간 안에, 나머지는 최근 14시간에 들어왔다 — 결제 가능이 24시간 기한 안에 있게."""
    if product_no == 1:
        return opens + seconds(round(position ** 1.5))
    if position <= 500:
        return opens + seconds((position - 1) * 60)
    return now - seconds((1001 - position) * 100)


def _schedule(p: Plan, now: datetime) -> None:
    if p.bucket in (DEAD_LETTER, PENDING, LOST):
        return
    if p.late_registered:
        p.payable_from = now - timedelta(hours=1 + p.position % 20, minutes=p.position % 60)
    else:
        # 보통 접수 2~4초 뒤. 20건에 1건은 첫 차례가 일시 실패해 10초 뒤 둘째 차례로 확인됐다
        p.payable_from = p.created_at + seconds(2 + p.position % 3 + (10 if p.n % 20 == 0 else 0))

    if p.bucket in (PAID, CANCEL_REFUNDED):
        p.paid_at = p.payable_from + timedelta(minutes=5 + p.position % 300)
    if p.bucket == EXPIRED:
        # 만료 취소는 SYSTEM 이 사유 없이 시작한다(ExpiryCancelService)
        p.cancel_actor = 'SYSTEM'
        p.cancel_start = p.payable_from + timedelta(hours=24, minutes=1 + p.position % 30)
    elif p.bucket in (CANCEL_REFUNDED, CANCELED):
        p.cancel_actor = 'USER'
        p.cancel_reason = USER_CANCEL_REASONS[p.position % 4]
        p.cancel_start = (p.paid_at + timedelta(hours=1 + p.position % 48) if p.bucket == CANCEL_REFUNDED
                          else p.payable_from + timedelta(hours=1 + p.position % 20))


@dataclass(frozen=True)
class Seeded:
    plans: list[Plan]
    orders: list[Order]


def seed(cur, now: datetime, catalog: Catalog, campaigns: dict[int, campaign.Campaign],
         customers: dict[int, Customer]) -> Seeded:
    plans = plan(now, catalog, campaigns, customers)
    _insert_preorders(cur, plans, catalog)
    _insert_events(cur, plans)
    _insert_jobs(cur, plans)
    _insert_attempts(cur, plans)
    _insert_outbox(cur, plans, now)
    _advance_campaigns(cur, plans)
    return Seeded(plans, _orders(plans, catalog))


def _events(p: Plan) -> list[tuple]:
    """상태가 바뀐 사건만: →PENDING_SYNC(USER) → REGISTERED(SYSTEM) → RESERVED(SYSTEM) → CANCELING → CANCELED(SYSTEM)."""
    rows = [(None, 'PENDING_SYNC', 'USER', None, p.created_at)]
    if p.payable_from:
        rows.append(('PENDING_SYNC', 'REGISTERED', 'SYSTEM', None, p.payable_from))
    if p.paid_at:
        rows.append(('REGISTERED', 'RESERVED', 'SYSTEM', None, p.paid_at))
    if p.cancel_start:
        rows.append((rows[-1][1], 'CANCELING', p.cancel_actor, p.cancel_reason, p.cancel_start))
        rows.append(('CANCELING', 'CANCELED', 'SYSTEM', None, p.cancel_done))
    return rows


def _insert_preorders(cur, plans: list[Plan], catalog: Catalog) -> None:
    rows = []
    for p in plans:
        events = _events(p)
        rows.append((
            p.id.bytes, p.token, p.customer.id.bytes, p.product_id.bytes, p.option.id.bytes, p.batch_id.bytes,
            p.position,
            # 입장권 id 는 가짜 값이지만 비우지 않는다. NULL 은 "관리자 대신 접수" 라는 뜻이다
            f'seed-ticket-{p.n}', f'seed-accept-{p.n}',
            catalog.product_titles[p.product_no], p.option.title, p.option.price,
            # 결제 가능했던 예약은 취소된 뒤에도 결제 가능 시각 · 결제 시각 · 외부 번호를 남긴다(be 가 지우지 않는다)
            p.status, p.payable_from, p.payment_started_at, p.paid_at, p.external_number,
            LOST_NOTE if p.bucket == LOST else None,
            len(events), p.created_at, events[-1][4],
        ))
    insert(cur, 'preorders', ['id', 'preorder_token', 'customer_id', 'product_id', 'option_id', 'shipment_batch_id',
                              'queue_position', 'admission_ticket_id', 'idempotency_key', 'product_title_snapshot',
                              'option_title_snapshot', 'unit_price_snapshot', 'status', 'payable_from',
                              'payment_started_at', 'reserved_at', 'external_reference', 'internal_note',
                              'event_sequence', 'created_at', 'updated_at'], rows)


def _insert_events(cur, plans: list[Plan]) -> None:
    insert(cur, 'preorder_events',
           ['preorder_id', 'event_sequence', 'from_status', 'to_status', 'actor', 'reason', 'created_at'],
           [(p.id.bytes, sequence, *event) for p in plans for sequence, event in enumerate(_events(p), start=1)])


def register_payload(p: Plan) -> dict:
    """외부 등록 본문 — be RegisterRequestPayload 그대로. 회원 · 상품은 UUID 문자열, 키는 Idempotency-Key 헤더(= preorder_token)로도 간다."""
    return {'ourReservationId': p.token, 'customerRef': str(p.customer.id), 'itemCode': str(p.product_id),
            'optionCode': p.option.sku, 'qty': 1, 'scope': EXTERNAL_SCOPE}


def _insert_jobs(cur, plans: list[Plan]) -> None:
    """외부 등록 작업은 예약마다 하나, 외부 취소 작업은 취소된 예약마다 하나."""
    rows = []
    for p in plans:
        status = {DEAD_LETTER: 'DEAD_LETTER', PENDING: 'PENDING', LOST: 'PENDING'}.get(p.bucket, 'SUCCEEDED')
        rows.append((p.register_job_id.bytes, p.id.bytes, 'REGISTER', json.dumps(register_payload(p)), status,
                     p.register_run, None, None, p.dead_lettered_at, p.created_at,
                     p.confirm_at or p.dead_lettered_at or p.created_at))
        if p.cancel_start:
            # be CancelRequestPayload 그대로 — 번호 칸 이름은 reservationNo 다
            payload = {'externalKey': p.token, 'reservationNo': p.external_number,
                       'reason': 'USER_CANCEL' if p.cancel_actor == 'USER' else 'DEADLINE_EXCEEDED'}
            rows.append((p.cancel_job_id.bytes, p.id.bytes, 'CANCEL', json.dumps(payload), 'SUCCEEDED', 1,
                         None, None, None, p.cancel_job_created_at, p.cancel_done - seconds(0.5)))
    insert(cur, 'preorder_sync_jobs', ['id', 'preorder_id', 'job_type', 'request_payload', 'status', 'run',
                                       'lease_token', 'lease_expires_at', 'dead_lettered_at', 'created_at',
                                       'updated_at'], rows)


def _insert_attempts(cur, plans: list[Plan]) -> None:
    """시도 기록은 worker 가 보낸 결과 이벤트 하나가 한 줄이다(preorder SyncAttemptWriter).

    번호(attempt_number)는 작업 안의 일련번호, 세대 · 차례(generation · generation_attempt)는 worker 가 센 값이다 —
    관리자 재처리 뒤에는 worker 가 1세대 1차례부터 다시 센다. 결과 이벤트 id(event_id)로 같은 이벤트를 두 번 남기지 않는다.
    """
    rows = []

    def attempt(p, job_type, job_id, number, generation, turn, actor, outcome, started, finished):
        result, status, code, message = outcome
        rows.append((job_id.bytes, number, generation, turn, actor, result, status, code, message, started, finished,
                     uuid_of('attempt-event', job_type, p.n, number)))

    success = ('SUCCESS', 201, None, None)
    for p in plans:
        rounds = p.exhausted_rounds
        for number, (generation, turn, outcome, started, finished) in enumerate(rounds, start=1):
            attempt(p, 'REGISTER', p.register_job_id, number, generation, turn, 'SYSTEM', outcome, started, finished)
        if p.late_registered:
            # 재처리한 실행 — worker 가 다시 1세대 1차례부터 센다. 행위자는 재처리 메시지의 ADMIN
            attempt(p, 'REGISTER', p.register_job_id, len(rounds) + 1, 1, 1, 'ADMIN', success,
                    p.confirm_at - seconds(0.5), p.confirm_at)
        elif p.confirm_at:
            if p.n % 20 == 0:
                attempt(p, 'REGISTER', p.register_job_id, 1, 1, 1, 'SYSTEM', INJECTED_500,
                        p.created_at + seconds(0.5), p.created_at + seconds(1))
            first = 2 if p.n % 20 == 0 else 1
            attempt(p, 'REGISTER', p.register_job_id, first, 1, first, 'SYSTEM', success,
                    p.confirm_at - seconds(0.5), p.confirm_at)
        if p.cancel_start:
            # 외부 취소는 한 번에 성공(Mock 취소는 200)
            attempt(p, 'CANCEL', p.cancel_job_id, 1, 1, 1, 'SYSTEM', ('SUCCESS', 200, None, None),
                    p.cancel_job_created_at + seconds(0.2), p.cancel_done - seconds(0.5))
    insert(cur, 'preorder_sync_attempts', ['sync_job_id', 'attempt_number', 'generation', 'generation_attempt',
                                           'actor', 'result', 'http_status', 'error_code', 'error_message',
                                           'started_at', 'finished_at', 'event_id'], rows)


def _insert_outbox(cur, plans: list[Plan], now: datetime) -> None:
    """등록 작업의 REGISTER_JOB_READY. payload 는 be RegisterJobReady 를 Jackson 이 쓴 모양 그대로다 —
    worker 는 DB 를 읽지 않아 등록 본문(request)을 통째로 싣고, 실행 회차(run)를 결과에 돌려줘 지난 실행의 결과를 가린다.
    접수 때 한 번(SYSTEM, 회차 1), 관리자 재처리 때마다 한 번(ADMIN, 오른 회차) 나간다.

    "지금 처리될" 예약(PENDING · LOST)만 미발행이다 — 끝난 예약을 미발행으로 두면 기동할 때 다시 발행된다.
    발행 끝난 행은 be 가 7일 뒤 지우므로, 7일 안에 발행된 것만 넣는다. 취소 쪽 이벤트는 이미 끝난 일이라 넣지 않는다.
    """
    rows = []

    def event(p, event_id, actor, run, created, published):
        if published and published <= now - OUTBOX_RETENTION:
            return
        payload = {'syncJobId': str(p.register_job_id), 'preorderId': p.token, 'request': register_payload(p),
                   'actor': actor, 'run': run, 'jobType': 'REGISTER'}
        rows.append((event_id, 'PREORDER_SYNC_JOB', p.register_job_id.bytes, 'REGISTER_JOB_READY',
                     json.dumps(payload), 1 if published else 0, None, created, published))

    for p in plans:
        waiting = p.bucket in (PENDING, LOST)
        event(p, uuid_of('outbox', p.n), 'SYSTEM', 1, p.created_at,
              None if waiting else p.created_at + timedelta(milliseconds=50))
        if p.late_registered:
            event(p, uuid_of('outbox', p.n, 'run', 2), 'ADMIN', 2, p.reprocessed_at,
                  p.reprocessed_at + timedelta(milliseconds=50))
    insert(cur, 'preorder_outbox_events', ['event_id', 'aggregate_type', 'aggregate_id', 'event_type', 'payload',
                                           'publish_attempts', 'lease_until', 'created_at', 'published_at'], rows)


def _advance_campaigns(cur, plans: list[Plan]) -> None:
    """다음 신청이 받을 순번 = 그 모델의 최대 순번 + 1."""
    for product_no in BUCKETS:
        mine = [p for p in plans if p.product_no == product_no]
        cur.execute('UPDATE preorder_campaigns SET next_queue_position = %s, updated_at = %s WHERE product_id = %s',
                    (max(p.position for p in mine) + 1, max(p.created_at for p in mine), mine[0].product_id.bytes))


def _orders(plans: list[Plan], catalog: Catalog) -> list[Order]:
    """결제한 예약의 주문. 사전예약 주문은 결제 기한이 없고 예약과 같은 회원 · 같은 공개 UUID 를 갖는다. 한 줄 · 수량 1 · 보증 없음.

    이력: 생성(USER) → 결제 요청(USER) → 승인(SYSTEM). 결제 후 취소는 예약 취소를 받아 환불 중 → 취소(SYSTEM).
    환불 중 사유는 SettlePreorderCancelService 가 남기는 형식(PREORDER_CANCEL:취소 까닭)이다.
    """
    orders = []
    for p in plans:
        if p.bucket not in (PAID, CANCEL_REFUNDED):
            continue
        events = [
            Event(None, 'AWAITING_PAYMENT', 'USER', None, p.payment_started_at),
            Event('AWAITING_PAYMENT', 'AUTHORIZING', 'USER', None, p.paid_at - timedelta(minutes=1)),
            Event('AUTHORIZING', 'AWAITING_CONFIRMATION', 'SYSTEM', None, p.paid_at),
        ]
        if p.bucket == CANCEL_REFUNDED:
            events += [
                Event('AWAITING_CONFIRMATION', 'CANCELING', 'SYSTEM', 'PREORDER_CANCEL:USER',
                      p.cancel_start + seconds(0.5)),
                Event('CANCELING', 'CANCELED', 'SYSTEM', None, p.cancel_start + seconds(1.5)),
            ]
        order_id = uuid7(p.payment_started_at, 'order', p.n)
        orders.append(Order(
            id=order_id, n=p.n, customer=p.customer, source='PREORDER', preorder_id=p.id, preorder_token=p.token,
            items=[Item(uuid7(p.payment_started_at, 'order-item', p.n), p.n, p.product_id, p.option.id, 1,
                        p.option.price, 0, 0, catalog.product_titles[p.product_no], p.option.title)],
            events=events,
        ))
    return orders
