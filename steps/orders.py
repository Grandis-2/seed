"""주문 · 결제 · 재고 · 장바구니 · 구매 후기, 그리고 배송지 없는 회원.

사전예약 주문은 preorders 단계가 계획에서 만들어 넘기고, 여기서 일반 주문 300건과 함께 넣는다.
결제(payments · payment_transactions)는 주문 이력에서 만든다 — 승인 이력이 있으면 결제가 있고, 환불 중 → 취소 이력이 있으면 환불됐다.
결제는 대상(target_type = ORDER, target_id = 주문 id)으로 남는다. 결제 키 · PG 주문 번호 형식은 가정이다.

일반 주문 번호 5,001~5,300(k = 번호 − 5,000). 상태는 k % 20 으로 나눈다:
  0~11 DELIVERED 60% · 12~14 SHIPPED 15% · 15~16 AWAITING_CONFIRMATION 10% · 17 AWAITING_PAYMENT 5% · 18~19 CANCELED 10%(결제 전 취소)
보증을 파는 옵션(NOVA 스마트폰 · 노트북)은 주문 4건에 1건꼴로 보증을 함께 샀다 — 총액에 보증가 × 보증 수량이 들어간다.
"""
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from .catalog import Catalog
from .common import insert, mask_name, uuid7, uuid_of
from .customers import DEMO, Customer, clear_addresses

GENERAL_ORDERS = 300
PAYMENT_WINDOW = timedelta(minutes=10)  # 일반 주문의 결제 기한(Order.paymentDueAt)
UNPAID = ('AWAITING_PAYMENT', 'AUTHORIZING')

REVIEW_BODIES = [
    '배송이 생각보다 빨라서 좋았어요.', '포장이 꼼꼼하게 와서 만족합니다.', '색감이 사진이랑 거의 같아요.',
    '가격 대비 품질이 괜찮습니다.', '손에 잘 맞고 가벼워요.', '마감이 깔끔해서 마음에 듭니다.',
    '두 번째 구매인데 여전히 만족해요.', '선물용으로 샀는데 반응이 좋았어요.', '설명대로 잘 작동합니다.',
    '조금 비싸지만 오래 쓸 수 있을 것 같아요.', '배송은 하루 늦었지만 상품은 괜찮아요.', '기대했던 것보다 훨씬 좋네요.',
    '무난하게 쓰기 좋습니다.', '디자인이 깔끔해서 어디에나 잘 어울려요.', '사용한 지 일주일 됐는데 문제없어요.',
    '생각보다 크기가 조금 작았어요.', '재구매 의사 있습니다.', '가족들도 하나씩 사려고 해요.',
    '처음엔 반신반의했는데 만족합니다.', '색상이 예뻐서 기분이 좋아요.', '품질은 좋은데 상자가 조금 찌그러져 왔어요.',
    '매일 쓰는데 불편한 점이 없어요.', '상세 설명이 정확했어요.', '충분히 제값을 하는 제품입니다.',
    '친구 추천으로 샀는데 잘 샀다 싶어요.', '기본에 충실한 제품이에요.', '문의 답변이 빨라서 좋았어요.',
    '오래 써 봐야 알겠지만 첫인상은 좋습니다.', '아쉬운 점이 하나도 없네요.', '다음 모델이 나와도 또 살 것 같아요.',
]
# 아쉬운 점을 적은 본문 — 별점 3
CRITICAL_REVIEWS = {10, 15, 20}


@dataclass(frozen=True)
class Event:
    from_status: str | None
    to_status: str
    actor: str
    reason: str | None
    at: datetime


@dataclass(frozen=True)
class Item:
    id: uuid.UUID
    no: int  # 계획용 번호 — 후기를 고르는 데 쓴다
    product_id: uuid.UUID
    option_id: uuid.UUID
    quantity: int
    unit_price: int
    warranty_quantity: int
    warranty_price: int  # 보증을 사지 않았으면 0
    product_title: str
    option_title: str


@dataclass
class Order:
    id: uuid.UUID
    n: int  # 계획용 번호 — 사전예약 주문은 예약 계획 번호(1~4,000), 일반 주문은 5,001~5,300
    customer: Customer
    source: str
    items: list[Item]
    events: list[Event]
    preorder_id: uuid.UUID | None = None
    preorder_token: str | None = None

    @property
    def token(self) -> str:
        """공개 주문 번호(order_token) — be 가 주문 id 로 합쳤다. 새 행에 DB 가 채우는 값(id 의 문자열)과 같다"""
        return str(self.id)

    @property
    def status(self) -> str:
        return self.events[-1].to_status

    @property
    def total(self) -> int:
        """상품가 × 수량 + 보증가 × 보증 수량(OrderLine.subtotal)."""
        return sum(i.quantity * i.unit_price + i.warranty_quantity * i.warranty_price for i in self.items)

    def at(self, to_status: str, from_status: str | None = None) -> datetime | None:
        """그 상태로 들어간 시각. 없으면 None."""
        return next((e.at for e in self.events
                     if e.to_status == to_status and (from_status is None or e.from_status == from_status)), None)

    @property
    def payment_due_at(self) -> datetime | None:
        """사전예약 주문은 예약의 24시간 기한을 따르므로 늘 비어 있다(ck_order_due)."""
        return None if self.source == 'PREORDER' else self.events[0].at + PAYMENT_WINDOW

    @property
    def stock_released_at(self) -> datetime | None:
        """결제 전에 취소된 일반 주문만 재고를 돌려놓았다. 사전예약 주문은 재고가 없다(ck_order_preorder_no_stock)."""
        if self.source != 'PREORDER' and self.status == 'CANCELED':
            return self.events[-1].at
        return None


def _general_orders(now: datetime, catalog: Catalog, customers: dict[int, Customer]) -> list[Order]:
    orders = []
    for k in range(1, GENERAL_ORDERS + 1):
        r = k % 20
        status = ('DELIVERED' if r < 12 else 'SHIPPED' if r < 15 else 'AWAITING_CONFIRMATION' if r < 17
                  else 'AWAITING_PAYMENT' if r == 17 else 'CANCELED')
        source = 'BUY_NOW' if k % 3 == 0 else 'CART'
        created = {
            'DELIVERED': now - timedelta(days=7 + k % 50, hours=k % 24),
            'SHIPPED': now - timedelta(days=2 + k % 2, hours=k % 12),
            'AWAITING_CONFIRMATION': now - timedelta(hours=1 + k % 20),
            'AWAITING_PAYMENT': now - timedelta(minutes=1 + k % 5),  # 결제 기한(10분) 안. 시드 뒤 몇 분 지나면 기한이 지난다
            'CANCELED': now - timedelta(days=3 + k % 25),
        }[status]

        # 주문상품 1~3개. 주문 가능한 일반 판매 옵션 중에서 겹치지 않게 고른다(j × 5 = 0 · 5 · 10, 옵션 수보다 작다).
        # 용량이 있는 기기(폰 · 태블릿 · 노트북)는 1개씩, 나머지는 1~2개. 보증을 파는 옵션은 4건에 1건꼴로 수량만큼 보증을 샀다
        items = []
        for j in range(1 if source == 'BUY_NOW' else 1 + (k // 3) % 3):
            option = catalog.in_stock_options[(k * 7 + j * 5) % len(catalog.in_stock_options)]
            quantity = 1 if option.storage is not None else 1 + (k + j) % 2
            warranty = option.warranty_offered and (k + j) % 4 == 0
            items.append(Item(uuid7(created, 'order-item', 5000 + k, j), 10000 + (k - 1) * 3 + j,
                              option.product_id, option.id, quantity, option.price,
                              quantity if warranty else 0, option.warranty_surcharge if warranty else 0,
                              catalog.product_titles[option.product_no], option.title))

        # 이력: 생성 → 결제 요청 → 승인 → 상품 준비 → 포장 → 출고 → 배송 완료. 관리자 전이는 사유가 필요하다(EventCause)
        events = [Event(None, 'AWAITING_PAYMENT', 'USER', None, created)]
        if status == 'CANCELED':
            events.append(Event('AWAITING_PAYMENT', 'CANCELED', 'USER', None, created + timedelta(minutes=5)))
        if status in ('AWAITING_CONFIRMATION', 'SHIPPED', 'DELIVERED'):
            events += [Event('AWAITING_PAYMENT', 'AUTHORIZING', 'USER', None, created + timedelta(minutes=2)),
                       Event('AUTHORIZING', 'AWAITING_CONFIRMATION', 'SYSTEM', None, created + timedelta(minutes=3))]
        if status in ('SHIPPED', 'DELIVERED'):
            events += [Event('AWAITING_CONFIRMATION', 'PREPARING_ITEMS', 'ADMIN', '상품 준비 시작',
                             created + timedelta(hours=24)),
                       Event('PREPARING_ITEMS', 'READY_TO_SHIP', 'ADMIN', '포장 완료', created + timedelta(hours=28)),
                       Event('READY_TO_SHIP', 'SHIPPED', 'ADMIN', '출고', created + timedelta(hours=32))]
        if status == 'DELIVERED':
            events.append(Event('SHIPPED', 'DELIVERED', 'SYSTEM', None, created + timedelta(days=3)))

        # 6~3,005 중 서로 다른 300명(37 은 3,000 과 서로소라 k × 37 mod 3,000 이 겹치지 않는다)
        orders.append(Order(uuid7(created, 'order', 5000 + k), 5000 + k, customers[6 + (k * 37) % 3000], source,
                            items, events))
    return orders


def seed(cur, now: datetime, catalog: Catalog, customers: dict[int, Customer],
         preorder_orders: list[Order]) -> list[Order]:
    general = _general_orders(now, catalog, customers)
    orders = preorder_orders + general
    _insert_orders(cur, orders)
    _insert_payments(cur, orders)
    _update_inventory(cur, catalog, general)
    _insert_cart(cur, now, catalog, customers)
    _insert_reviews(cur, general)

    # 배송지 없는 회원: 주문이 없는 회원 중 번호 끝자리 0. 주문은 배송지를 복사하므로 주문 있는 회원은 비우지 않는다
    ordered = {o.customer.no for o in orders}
    clear_addresses(cur, [c.id for n, c in customers.items() if n > DEMO and n % 10 == 0 and n not in ordered])
    return orders


def _insert_orders(cur, orders: list[Order]) -> None:
    insert(cur, 'orders', ['id', 'order_token', 'customer_id', 'source', 'preorder_id', 'preorder_token', 'idempotency_key',
                           'status', 'authorizing_provider_order_id', 'total_amount', 'payment_due_at', 'stock_released_at',
                           'paid_at',
                           'ship_to_name', 'ship_to_phone', 'ship_to_postal_code', 'ship_to_line1', 'ship_to_line2',
                           'internal_note', 'event_sequence', 'created_at', 'updated_at'],
           # 바로 구매는 화면이 누름마다 만든 멱등 키를 반드시 갖는다(ck_order_idempotency). 결제 시각은 승인 완료로 넘어간 때 —
           # be 백필(V202610161601)과 같은 기준이다
           [(o.id.bytes, o.token, o.customer.id.bytes, o.source, o.preorder_id.bytes if o.preorder_id else None,
             o.preorder_token, uuid7(o.events[0].at, 'idempotency', o.token).bytes if o.source == 'BUY_NOW' else None,
             o.status, None, o.total, o.payment_due_at, o.stock_released_at,
             o.at('AWAITING_CONFIRMATION', 'AUTHORIZING'),
             # 배송지는 회원 기본 배송지를 복사한다
             o.customer.name, o.customer.phone, o.customer.postal_code, o.customer.line1, o.customer.line2,
             None, len(o.events), o.events[0].at, o.events[-1].at)
            for o in orders])
    insert(cur, 'order_items', ['id', 'order_id', 'product_id', 'option_id', 'quantity', 'warranty_quantity',
                                'unit_price_snapshot', 'warranty_price_snapshot', 'product_title_snapshot',
                                'option_title_snapshot'],
           [(i.id.bytes, o.id.bytes, i.product_id.bytes, i.option_id.bytes, i.quantity, i.warranty_quantity,
             i.unit_price, i.warranty_price, i.product_title, i.option_title)
            for o in orders for i in o.items])
    insert(cur, 'order_events', ['order_id', 'event_sequence', 'from_status', 'to_status', 'actor', 'reason',
                                 'created_at'],
           [(o.id.bytes, n, e.from_status, e.to_status, e.actor, e.reason, e.at)
            for o in orders for n, e in enumerate(o.events, start=1)])


def _insert_payments(cur, orders: list[Order]) -> None:
    """승인 이력(→ AWAITING_CONFIRMATION)이 있는 주문마다 결제 하나. 환불 중 → 취소 이력이 있으면 환불됐다(REFUNDED ⇔ refunded_at).

    결제 승인(CAPTURE)은 결제 요청 시각에 시작해 승인 시각에 끝났다. PG 주문 번호는 be 처럼 결제창마다 새 UUID 다.
    환불(REFUND)은 환불 중에 들어가며 시작해 취소 시각에 끝났다. 승인 · 환불은 대상마다 하나씩이다(uq_payment_tx_active).
    """
    payments, transactions = [], []
    for o in orders:
        paid_at = o.at('AWAITING_CONFIRMATION')
        if not paid_at:
            continue
        refunded_at = o.at('CANCELED', from_status='CANCELING')
        key = f'seed-pay-{o.n}'
        payments.append((uuid7(paid_at, 'payment', o.n).bytes, 'ORDER', o.id.bytes, key, o.total,
                         'REFUNDED' if refunded_at else 'SUCCEEDED', paid_at, refunded_at, paid_at,
                         refunded_at or paid_at))
        requested = o.at('AUTHORIZING')
        transactions.append((uuid7(requested, 'capture', o.n).bytes, 'ORDER', o.id.bytes, 'CAPTURE', uuid_of('provider-order', o.n), key,
                             o.total, f'seed-capture-{o.n}', 'SUCCEEDED', 1, requested, paid_at, requested))
        if refunded_at:
            started = o.at('CANCELING')
            transactions.append((uuid7(started, 'refund', o.n).bytes, 'ORDER', o.id.bytes, 'REFUND', None, key,
                                 o.total, f'seed-refund-{o.n}', 'SUCCEEDED', 1, started, refunded_at, started))
    insert(cur, 'payments', ['id', 'target_type', 'target_id', 'provider_payment_key', 'amount', 'status',
                             'approved_at', 'refunded_at', 'created_at', 'updated_at'], payments)
    insert(cur, 'payment_transactions', ['id', 'target_type', 'target_id', 'transaction_type', 'provider_order_id',
                                         'provider_payment_key', 'amount', 'idempotency_key', 'status',
                                         'attempt_count', 'requested_at', 'finished_at', 'created_at'], transactions)


def _update_inventory(cur, catalog: Catalog, general: list[Order]) -> None:
    """reserved = 결제 전 주문 수량, sold = 결제된 주문 수량(취소 제외). 취소된 주문은 재고를 돌려놓았다."""
    for option in catalog.in_stock_options:
        lines = [(o, i) for o in general for i in o.items if i.option_id == option.id]
        if not lines:
            continue
        reserved = sum(i.quantity for o, i in lines if o.status in UNPAID)
        sold = sum(i.quantity for o, i in lines if o.status not in UNPAID and o.status != 'CANCELED')
        cur.execute('UPDATE option_inventories SET stock_reserved = %s, stock_sold = %s, updated_at = %s '
                    'WHERE option_id = %s', (reserved, sold, max(o.events[-1].at for o, _ in lines), option.id.bytes))


def _insert_cart(cur, now: datetime, catalog: Catalog, customers: dict[int, Customer]) -> None:
    """회원 3,006~3,205(200명) × 1~3개, 주문 가능한 일반 판매 옵션만(j × 7 = 0 · 7 · 14 라 겹치지 않는다).
    보증을 파는 옵션은 셋 중 하나꼴로 보증을 골랐다 — 같은 옵션이라도 보증 여부가 다르면 다른 줄이다."""
    rows = []
    for i in range(200):
        for j in range(1 + i % 3):
            at = now - timedelta(hours=i % 72, minutes=j)
            option = catalog.in_stock_options[(i * 3 + j * 7) % len(catalog.in_stock_options)]
            warranty = 1 if option.warranty_offered and (i + j) % 3 == 0 else 0
            rows.append((uuid7(at, 'cart', i, j).bytes, customers[3006 + i].id.bytes, option.id.bytes, warranty,
                         1 + (i + j) % 2, at, at))
    insert(cur, 'cart_items', ['id', 'customer_id', 'option_id', 'warranty_selected', 'quantity', 'created_at',
                               'updated_at'], rows)


def _insert_reviews(cur, general: list[Order]) -> None:
    """배송 완료 주문의 주문상품 60%. 리뷰어는 주문 주인, 별점은 본문과 맞춘다.
    작성자 이름은 실명이 있으면 실명, 없으면 닉네임의 첫 글자 + "**" 다(catalog ReviewService)."""
    rows = []
    for o in general:
        if o.status != 'DELIVERED':
            continue
        delivered = o.at('DELIVERED')
        for i in o.items:
            if i.no % 5 >= 3:
                continue
            body = i.no % 30
            rating = 3 if body in CRITICAL_REVIEWS else 4 if i.no % 3 == 0 else 5
            at = delivered + timedelta(hours=1 + i.no % 20)
            author = mask_name(o.customer.name or o.customer.display_name)
            rows.append((uuid7(at, 'review', i.no).bytes, i.product_id.bytes, o.customer.id.bytes, i.id.bytes, rating,
                         REVIEW_BODIES[body], i.option_title, author, at, at))
    insert(cur, 'product_reviews', ['id', 'product_id', 'customer_id', 'order_item_id', 'rating', 'body',
                                    'option_title_snapshot', 'author_name', 'created_at', 'updated_at'], rows)
