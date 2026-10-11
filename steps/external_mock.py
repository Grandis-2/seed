"""Mock 원장(external_mock.preorder_registrations) — be 예약과 같은 키 · 같은 번호 · 같은 내용.

be 와 같은 예약 계획에서 만든다. 로컬은 be MySQL 과 Mock MySQL 이 따로일 수 있어 JOIN 하지 않고, 한 프로그램이 두 DB 에
각각 접속해서 같은 계획을 넣는다. 맞는지는 verify 단계가 두 DB 를 직접 읽어 비교한다.

넣는 행:
  외부 등록이 성공한 예약 — 키 = preorder_token, 번호 = external_reference, 회원 · 상품 = be id(BINARY(16)), sku = 옵션 sku,
                           확정 시각 = 성공한 시도가 끝난 시각. 취소된 예약은 CANCELED(취소 시각 = 외부 취소 성공 시각)
  응답 유실 예약(LOST)     — be 는 아직 PENDING_SYNC 인데 Mock 에는 ACTIVE 로 있다. be 에 번호가 없으므로 같은 형식으로 만든다
Mock 은 등록 본문의 customerRef · itemCode(UUID 문자열)를 회원 · 상품 컬럼에 BINARY(16) 로 바꿔 저장한다 — 여기서는 be id 의 바이트를 그대로 넣는다.
"""
from datetime import datetime, timedelta

from .catalog import Catalog
from .common import insert, seconds, uuid7, uuid_of
from .preorders import LOST, Plan

COLUMNS = ['external_key', 'external_number', 'customer_id', 'product_id', 'sku', 'status', 'confirmed_at',
           'canceled_at']


def ledger(plans: list[Plan]) -> list[tuple]:
    rows = []
    for p in plans:
        if p.confirm_at:
            canceled_at = p.cancel_done - seconds(0.5) if p.cancel_start else None
            rows.append((p.token, p.external_number, p.customer.id.bytes, p.product_id.bytes, p.option.sku,
                         'CANCELED' if canceled_at else 'ACTIVE', p.confirm_at, canceled_at))
        elif p.bucket == LOST:
            confirmed = p.created_at + seconds(1)
            rows.append((p.token, f'R-{confirmed:%Y%m%d}-{p.n:010d}', p.customer.id.bytes, p.product_id.bytes,
                         p.option.sku, 'ACTIVE', confirmed, None))
    return rows


def seed(cur, plans: list[Plan]) -> int:
    return insert(cur, 'preorder_registrations', COLUMNS, ledger(plans))


def reconcile_demo(cur, plans: list[Plan], catalog: Catalog, now: datetime) -> None:
    """(선택) 정합성 시연 세트 — 어긋남 다섯 종류 × 3건.

    평소 데이터는 be 와 Mock 의 어긋남이 0 이다. 정합성 검사를 보여 줄 때만 넣는다(seed.py --reconcile-demo).
    고르는 행은 아이폰 18 Pro 결제 완료 묶음의 계획 번호 2~13 이다(1 은 시연 계정이라 뺀다). 원래대로 돌리려면 옵션 없이 다시 돌린다.
    """
    by_n = {p.n: p for p in plans}

    def keys(*ns):
        return [by_n[n].token for n in ns]

    def where(ns):
        return f"external_key IN ({', '.join(['%s'] * len(ns))})"

    # 1. Mock 에만 있음(유령 등록): be 에 없는 키로 ACTIVE 행(회원은 be 에 없는 id, 상품은 갤럭시 Z 폴드8)
    ghost = catalog.picks[(2, 0, 0)]
    insert(cur, 'preorder_registrations', COLUMNS,
           [(uuid_of('ghost', k), f'R-{now:%Y%m%d}-900000000{k}', uuid7(now, 'ghost-customer', k).bytes,
             ghost.product_id.bytes, ghost.sku, 'ACTIVE', now - timedelta(hours=k), None) for k in (1, 2, 3)])
    # 2. be 는 결제 완료인데 Mock 에 없음
    cur.execute(f'DELETE FROM preorder_registrations WHERE {where(keys(2, 3, 4))}', keys(2, 3, 4))
    # 3. 번호가 다름: 번호 끝 10자리의 첫 자리를 0 → 8 로
    cur.execute('UPDATE preorder_registrations '
                'SET external_number = CONCAT(LEFT(external_number, 11), \'8\', SUBSTRING(external_number, 13)) '
                f'WHERE {where(keys(5, 6, 7))}', keys(5, 6, 7))
    # 4. 상태가 다름: be 는 결제 완료, Mock 은 취소
    cur.execute(f'UPDATE preorder_registrations SET status = \'CANCELED\', canceled_at = %s '
                f'WHERE {where(keys(8, 9, 10))}', [now - timedelta(hours=1), *keys(8, 9, 10)])
    # 5. 내용이 다름: sku 를 같은 모델의 다른 색 옵션으로
    for n in (11, 12, 13):
        p = by_n[n]
        other = catalog.picks[(p.product_no, (p.option.color + 1) % 4, p.option.storage)]
        cur.execute('UPDATE preorder_registrations SET sku = %s WHERE external_key = %s', (other.sku, p.token))
