"""검증. 시드를 넣은 직후 모든 항목의 어긋남이 0 이어야 한다. 하나라도 있으면 seed.py 가 종료 코드 1 로 끝난다.

넣는 코드와 따로, DB 에 들어간 결과만 보고 센다. 예약 항목은 be preorder/load-test/invariant-check.sql 과 같은 검사를 모든 사전예약
모델에 한 번에 한다. 마지막 항목은 be 와 Mock 두 DB 를 직접 읽어 원장이 맞는지 본다.
"""
UUID = "'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'"
OUTBOX_RETENTION_DAYS = 7

# (검사, 어긋난 수를 세는 SQL). %(now)s 는 기준 시각이다.
CHECKS = [
    # ── 예약 원장
    ('순번 중복',
     'SELECT COUNT(*) - COUNT(DISTINCT product_id, queue_position) FROM preorders'),
    ('순번 빈틈',
     'SELECT COALESCE(SUM(gap), 0) FROM (SELECT MAX(queue_position) - COUNT(*) AS gap FROM preorders '
     'GROUP BY product_id) g'),
    ('순번 발급 불일치(회차 next_queue_position − 1 ≠ 예약 수)',
     'SELECT COUNT(*) FROM preorder_campaigns c '
     'WHERE c.next_queue_position - 1 <> (SELECT COUNT(*) FROM preorders p WHERE p.product_id = c.product_id)'),
    ('배송 차수 구간 밖 순번',
     'SELECT COUNT(*) FROM preorders p JOIN shipment_batches b ON b.id = p.shipment_batch_id '
     'WHERE p.queue_position < b.position_from OR (b.position_to IS NOT NULL AND p.queue_position > b.position_to)'),
    ('입장권 중복',
     'SELECT COUNT(admission_ticket_id) - COUNT(DISTINCT admission_ticket_id) FROM preorders'),
    ('예약 이력 번호(연속 · 예약 행과 일치)',
     'SELECT COUNT(*) FROM preorders p LEFT JOIN (SELECT preorder_id, COUNT(*) AS n, MAX(event_sequence) AS last_seq '
     'FROM preorder_events GROUP BY preorder_id) e ON e.preorder_id = p.id '
     'WHERE e.n IS NULL OR e.n <> e.last_seq OR p.event_sequence <> e.last_seq'),
    ('예약 이력의 마지막 상태 ≠ 예약 상태',
     'SELECT COUNT(*) FROM preorders p '
     'JOIN preorder_events e ON e.preorder_id = p.id AND e.event_sequence = p.event_sequence '
     'WHERE e.to_status <> p.status'),
    # ── 예약 ↔ 외부 등록 작업
    ('등록 작업 없는 예약',
     "SELECT COUNT(*) FROM preorders p WHERE NOT EXISTS "
     "(SELECT 1 FROM preorder_sync_jobs j WHERE j.preorder_id = p.id AND j.job_type = 'REGISTER')"),
    ('등록 작업 본문 ≠ 예약(키 · 회원 · 상품 · sku · 수량)',
     "SELECT COUNT(*) FROM preorder_sync_jobs j JOIN preorders p ON p.id = j.preorder_id "
     "JOIN product_options o ON o.id = p.option_id WHERE j.job_type = 'REGISTER' AND ("
     "j.request_payload->>'$.ourReservationId' <> p.preorder_token "
     "OR j.request_payload->>'$.customerRef' <> BIN_TO_UUID(p.customer_id) "
     "OR j.request_payload->>'$.itemCode' <> BIN_TO_UUID(p.product_id) "
     "OR j.request_payload->>'$.optionCode' <> o.sku OR j.request_payload->>'$.qty' <> 1)"),
    ('등록 이벤트(대기 작업마다 정확히 하나 · 어떤 작업도 둘 이상 아님)',
     "SELECT (SELECT COUNT(*) FROM (SELECT j.id FROM preorder_sync_jobs j "
     "LEFT JOIN preorder_outbox_events o ON o.aggregate_id = j.id AND o.aggregate_type = 'PREORDER_SYNC_JOB' "
     "AND o.event_type = 'REGISTER_JOB_READY' WHERE j.job_type = 'REGISTER' AND j.status = 'PENDING' "
     "GROUP BY j.id HAVING COUNT(o.id) <> 1) x) "
     "+ (SELECT COUNT(*) FROM (SELECT aggregate_id FROM preorder_outbox_events WHERE event_type = 'REGISTER_JOB_READY' "
     "GROUP BY aggregate_id HAVING COUNT(*) > 1) y)"),
    (f'보존 기간({OUTBOX_RETENTION_DAYS}일)이 지난 발행 완료 아웃박스(be 가 지웠어야 할 행)',
     f'SELECT COUNT(*) FROM preorder_outbox_events WHERE published_at <= %(now)s - INTERVAL {OUTBOX_RETENTION_DAYS} DAY'),
    ('결제 가능했던 예약인데 외부 번호 · 결제 가능 시각 없음',
     "SELECT COUNT(*) FROM preorders p WHERE EXISTS "
     "(SELECT 1 FROM preorder_events e WHERE e.preorder_id = p.id AND e.to_status = 'REGISTERED') "
     "AND (p.payable_from IS NULL OR p.external_reference IS NULL)"),
    ('등록 작업 SUCCEEDED ⇔ 외부 번호 있음',
     "SELECT COUNT(*) FROM preorders p "
     "JOIN preorder_sync_jobs j ON j.preorder_id = p.id AND j.job_type = 'REGISTER' "
     "WHERE (j.status = 'SUCCEEDED') <> (p.external_reference IS NOT NULL)"),
    ('취소 완료 예약 ⇔ 외부 취소 작업 SUCCEEDED',
     "SELECT COUNT(*) FROM preorders p "
     "LEFT JOIN preorder_sync_jobs j ON j.preorder_id = p.id AND j.job_type = 'CANCEL' AND j.status = 'SUCCEEDED' "
     "WHERE (p.status = 'CANCELED') <> (j.id IS NOT NULL)"),
    ('작업 시도 번호 연속',
     'SELECT COUNT(*) FROM (SELECT sync_job_id FROM preorder_sync_attempts '
     'GROUP BY sync_job_id HAVING COUNT(*) <> MAX(attempt_number)) x'),
    ('시드 직후 결제 기한(24시간)이 지난 결제 가능 예약',
     "SELECT COUNT(*) FROM preorders p WHERE p.status = 'REGISTERED' "
     "AND p.payable_from + INTERVAL 24 HOUR <= %(now)s "
     "AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.preorder_id = p.id)"),
    # ── 예약 ↔ 주문 · 결제
    ('결제 완료(RESERVED) ⇔ 결제된 주문(환불 전)',
     "SELECT COUNT(*) FROM preorders p LEFT JOIN orders o ON o.preorder_id = p.id "
     "LEFT JOIN payments pay ON pay.target_type = 'ORDER' AND pay.target_id = o.id "
     "WHERE p.status <> 'CANCELING' AND (p.status = 'RESERVED') <> COALESCE(pay.status = 'SUCCEEDED', FALSE)"),
    ('결제 시작 시각 ⇔ 사전예약 주문 · 결제 확인 시각 ⇔ 결제',
     "SELECT COUNT(*) FROM preorders p LEFT JOIN orders o ON o.preorder_id = p.id "
     "LEFT JOIN payments pay ON pay.target_type = 'ORDER' AND pay.target_id = o.id "
     "WHERE (p.payment_started_at IS NOT NULL) <> (o.id IS NOT NULL) "
     "OR (p.reserved_at IS NOT NULL) <> (pay.id IS NOT NULL)"),
    ('사전예약 주문의 회원 · 공개 UUID 가 예약과 다름',
     'SELECT COUNT(*) FROM orders o JOIN preorders p ON p.id = o.preorder_id '
     'WHERE o.customer_id <> p.customer_id OR o.preorder_token <> p.preorder_token'),
    # ── 주문 · 결제 · 재고
    ('주문 이력 번호(연속 · 주문 행과 일치 · 마지막 상태)',
     'SELECT COUNT(*) FROM orders o JOIN (SELECT order_id, COUNT(*) AS n, MAX(event_sequence) AS last_seq '
     'FROM order_events GROUP BY order_id) e ON e.order_id = o.id '
     'JOIN order_events l ON l.order_id = o.id AND l.event_sequence = e.last_seq '
     'WHERE e.n <> e.last_seq OR o.event_sequence <> e.last_seq OR l.to_status <> o.status'),
    ('주문 총액 ≠ 주문상품 합계(보증 포함)',
     'SELECT COUNT(*) FROM orders o JOIN (SELECT order_id, '
     'SUM(quantity * unit_price_snapshot + warranty_quantity * warranty_price_snapshot) AS total '
     'FROM order_items GROUP BY order_id) t ON t.order_id = o.id WHERE o.total_amount <> t.total'),
    ('보증을 사지 않았는데 보증가가 있음',
     'SELECT COUNT(*) FROM order_items WHERE warranty_quantity = 0 AND warranty_price_snapshot <> 0'),
    ('결제된 주문 ⇔ 결제 행 · 금액',
     "SELECT COUNT(*) FROM orders o LEFT JOIN payments p ON p.target_type = 'ORDER' AND p.target_id = o.id "
     "WHERE (EXISTS (SELECT 1 FROM order_events e WHERE e.order_id = o.id "
     "AND e.to_status = 'AWAITING_CONFIRMATION')) <> (p.id IS NOT NULL) OR p.amount <> o.total_amount"),
    ('결제 ⇔ 승인 거래 · 환불된 결제 ⇔ 환불 거래',
     "SELECT COUNT(*) FROM payments p "
     "LEFT JOIN payment_transactions c ON c.target_type = p.target_type AND c.target_id = p.target_id "
     "AND c.transaction_type = 'CAPTURE' AND c.status = 'SUCCEEDED' "
     "LEFT JOIN payment_transactions r ON r.target_type = p.target_type AND r.target_id = p.target_id "
     "AND r.transaction_type = 'REFUND' AND r.status = 'SUCCEEDED' "
     "WHERE c.id IS NULL OR c.amount <> p.amount OR (p.status = 'REFUNDED') <> (r.id IS NOT NULL)"),
    ('재고 ≠ 주문 수량',
     "SELECT COUNT(*) FROM option_inventories i LEFT JOIN (SELECT oi.option_id, "
     "SUM(IF(o.status IN ('AWAITING_PAYMENT', 'AUTHORIZING'), oi.quantity, 0)) AS reserved, "
     "SUM(IF(o.status NOT IN ('AWAITING_PAYMENT', 'AUTHORIZING', 'CANCELED'), oi.quantity, 0)) AS sold "
     "FROM order_items oi JOIN orders o ON o.id = oi.order_id WHERE o.source <> 'PREORDER' "
     "GROUP BY oi.option_id) x ON x.option_id = i.option_id "
     "WHERE i.stock_reserved <> COALESCE(x.reserved, 0) OR i.stock_sold <> COALESCE(x.sold, 0)"),
    # ── 카탈로그
    ('목록에 보일 상품인데 준비 안 됨(사전예약은 회차 · 일반은 재고 행)',
     "SELECT COUNT(*) FROM products p WHERE p.visible = 1 AND p.status = 'ACTIVE' AND NOT (CASE "
     "WHEN p.sale_mode = 'PREORDER' THEN EXISTS (SELECT 1 FROM preorder_campaigns c WHERE c.product_id = p.id) "
     "ELSE EXISTS (SELECT 1 FROM product_options o JOIN option_inventories i ON i.option_id = o.id "
     "WHERE o.product_id = p.id) END)"),
    ('사전예약 옵션의 재고 행 · 회차가 있는 일반 판매 상품',
     "SELECT (SELECT COUNT(*) FROM option_inventories i JOIN product_options o ON o.id = i.option_id "
     "JOIN products p ON p.id = o.product_id WHERE p.sale_mode = 'PREORDER') "
     "+ (SELECT COUNT(*) FROM preorder_campaigns c JOIN products p ON p.id = c.product_id "
     "WHERE p.sale_mode <> 'PREORDER')"),
    ('후기가 배송 완료 주문의 주인 · 그 상품이 아님',
     "SELECT COUNT(*) FROM product_reviews r JOIN order_items oi ON oi.id = r.order_item_id "
     "JOIN orders o ON o.id = oi.order_id "
     "WHERE o.status <> 'DELIVERED' OR r.customer_id <> o.customer_id OR r.product_id <> oi.product_id"),
    ('주문이 있는데 배송지가 없는 회원',
     'SELECT COUNT(*) FROM customers c WHERE c.default_ship_to_line1 IS NULL '
     'AND EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id)'),
    # ── 데이터 모양
    # 미래 시각이 있으면 화면 · 만료 배치가 엉뚱하게 동작한다
    ('기준 시각보다 미래인 기록',
     'SELECT (SELECT COUNT(*) FROM preorders WHERE GREATEST(created_at, updated_at) > %(now)s) '
     '+ (SELECT COUNT(*) FROM preorder_events WHERE created_at > %(now)s) '
     '+ (SELECT COUNT(*) FROM preorder_sync_jobs WHERE updated_at > %(now)s) '
     '+ (SELECT COUNT(*) FROM preorder_sync_attempts WHERE finished_at > %(now)s) '
     '+ (SELECT COUNT(*) FROM orders WHERE updated_at > %(now)s) '
     '+ (SELECT COUNT(*) FROM order_events WHERE created_at > %(now)s) '
     '+ (SELECT COUNT(*) FROM payments WHERE updated_at > %(now)s) '
     '+ (SELECT COUNT(*) FROM product_reviews WHERE created_at > %(now)s) '
     '+ (SELECT COUNT(*) FROM cart_items WHERE created_at > %(now)s)'),
    ('이력 시각 역전(앞 이력보다 이른 다음 이력)',
     'SELECT (SELECT COUNT(*) FROM preorder_events a JOIN preorder_events b '
     'ON b.preorder_id = a.preorder_id AND b.event_sequence = a.event_sequence + 1 WHERE b.created_at < a.created_at) '
     '+ (SELECT COUNT(*) FROM order_events a JOIN order_events b '
     'ON b.order_id = a.order_id AND b.event_sequence = a.event_sequence + 1 WHERE b.created_at < a.created_at) '
     '+ (SELECT COUNT(*) FROM preorder_sync_attempts a JOIN preorder_sync_attempts b '
     'ON b.sync_job_id = a.sync_job_id AND b.attempt_number = a.attempt_number + 1 WHERE b.started_at < a.finished_at) '
     '+ (SELECT COUNT(*) FROM preorder_sync_attempts WHERE finished_at < started_at)'),
    # be 는 공개 UUID 를 소문자 표준형으로만 받는다
    ('소문자 표준형이 아닌 공개 UUID',
     f'SELECT (SELECT COUNT(*) FROM preorders WHERE preorder_token NOT REGEXP {UUID}) '
     f'+ (SELECT COUNT(*) FROM orders WHERE order_token NOT REGEXP {UUID})'),
    # be 엔티티 id 는 UUID v7 이다(버전 니블이 7)
    ('UUID v7 이 아닌 id',
     "SELECT (SELECT COUNT(*) FROM customers WHERE SUBSTRING(HEX(id), 13, 1) <> '7') "
     "+ (SELECT COUNT(*) FROM products WHERE SUBSTRING(HEX(id), 13, 1) <> '7') "
     "+ (SELECT COUNT(*) FROM preorders WHERE SUBSTRING(HEX(id), 13, 1) <> '7') "
     "+ (SELECT COUNT(*) FROM orders WHERE SUBSTRING(HEX(id), 13, 1) <> '7')"),
    # 접속 문자셋이 latin1 이면 한글이 두 번 인코딩돼 들어간다(UTF-8 바이트를 다시 UTF-8 로)
    ('두 번 인코딩된 한글',
     "SELECT (SELECT COUNT(*) FROM categories WHERE HEX(name) REGEXP '^(..)*C3[89AB]') "
     "+ (SELECT COUNT(*) FROM customers WHERE HEX(name) REGEXP '^(..)*C3[89AB]')"),
]


def ledger_mismatches(shop, mock) -> tuple[int, int]:
    """be 예약과 Mock 원장을 직접 비교한다. @return (어긋난 수, Mock 행 수)

    be 결제 가능 · 결제 완료 · 응답 유실 ⇔ Mock ACTIVE, be 취소 ⇔ Mock CANCELED. 회원 · 상품 · sku 가 예약과 같아야 하고,
    be 에 번호가 있으면 Mock 번호와 같아야 한다. 회원 · 상품은 두 쪽 모두 BINARY(16) 이라 바이트로 비교한다.
    """
    with shop.cursor() as cur:
        cur.execute("SELECT p.preorder_token, p.status, p.external_reference, p.customer_id, p.product_id, o.sku "
                    "FROM preorders p JOIN product_options o ON o.id = p.option_id "
                    "WHERE p.status IN ('REGISTERED', 'RESERVED', 'CANCELED') OR p.internal_note LIKE %s",
                    ('[시드] 응답 유실%',))
        expected = {key: rest for key, *rest in cur.fetchall()}
    with mock.cursor() as cur:
        cur.execute('SELECT external_key, external_number, customer_id, product_id, sku, status '
                    'FROM preorder_registrations')
        actual = {key: rest for key, *rest in cur.fetchall()}

    mismatches = len(expected.keys() ^ actual.keys())
    for key in expected.keys() & actual.keys():
        status, number, customer, product, sku = expected[key]
        m_number, m_customer, m_product, m_sku, m_status = actual[key]
        if (m_status != ('CANCELED' if status == 'CANCELED' else 'ACTIVE')
                or (bytes(m_customer), bytes(m_product), m_sku) != (bytes(customer), bytes(product), sku)
                or (number is not None and number != m_number)):
            mismatches += 1
    return mismatches, len(actual)


def run(shop, mock, now) -> int:
    """검사를 모두 돌리고 출력한다. @return 실패한 검사 수"""
    failed = 0
    with shop.cursor() as cur:
        for name, sql in CHECKS:
            cur.execute(sql, {'now': now} if '%(now)s' in sql else None)
            count = int(cur.fetchone()[0])
            failed += count != 0
            print(f"  {'ok' if count == 0 else f'어긋남 {count}':8} {name}")
    mismatches, rows = ledger_mismatches(shop, mock)
    failed += mismatches != 0
    print(f"  {'ok' if mismatches == 0 else f'어긋남 {mismatches}':8} be 예약 ⇔ Mock 원장 ({rows}건)")
    return failed
