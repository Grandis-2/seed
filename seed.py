"""로컬 시드. be shop 과 Mock external_mock 을 비우고 다시 채운다 — 다시 돌리면 그게 초기화다.

    pip install -r requirements.txt
    python seed.py --shop mysql://root:비밀번호@127.0.0.1:3306/shop
    python seed.py --shop … --reconcile-demo     정합성 시연 세트(be · Mock 어긋남 15건)까지 넣는다

--mock 을 주지 않으면 Mock 저장소 compose 의 MySQL(127.0.0.1:3307, nova/nova)에 넣는다.
접속 주소는 환경 변수 SEED_SHOP_URL · SEED_MOCK_URL 로도 줄 수 있다.
로컬 전용이다. 접속 주소가 이 PC(localhost · 127.0.0.1 · ::1)가 아니면 돌지 않는다 — 시드는 테이블을 비우고 다시 채운다.
"""
import argparse
import os
import sys
import time
from urllib.parse import unquote, urlparse

import pymysql

from steps import campaign, catalog, customers, dead_letters, external_mock, orders, preorders, verify
from steps.common import utc_now

DEFAULT_MOCK_URL = 'mysql://nova:nova@127.0.0.1:3307/external_mock'
LOCAL_HOSTS = {'localhost', '127.0.0.1', '::1'}

# 비울 shop 테이블 — flyway_schema_history 만 빼고 전부. 비우면 다음 migrate 가 처음부터 다시 적용하려다 실패한다.
# 테이블이 늘면 여기에 더한다. 빠뜨리면 두 번째 실행이 PK 충돌로 멈추거나 이전 데이터가 남는다.
SHOP_TABLES = [
    'product_reviews', 'cart_items', 'payment_transactions', 'payments', 'payment_outbox_events',
    'draw_entries', 'draw_campaigns', 'order_events', 'order_items', 'orders', 'order_outbox_events',
    'preorder_dead_letter_events', 'preorder_outbox_events', 'preorder_sync_attempts', 'preorder_sync_jobs',
    'preorder_events', 'preorders', 'shipment_batches', 'preorder_campaigns', 'refresh_tokens', 'customers',
    'option_inventories', 'product_options', 'products', 'catalog_outbox_events', 'categories',
]
# 아직 be dev 에 없는 테이블 — 있을 때만 비운다(정합성 검사 결과는 epic/NV-441 마이그레이션에만 있다). dev 에 들어오면 위로 옮긴다
OPTIONAL_SHOP_TABLES = ['reconciliation_findings', 'reconciliation_runs']


def connect(url: str, label: str) -> pymysql.connections.Connection:
    """mysql://계정:비밀번호@호스트:포트/database. 접속 문자셋은 utf8mb4 — 아니면 한글이 깨져 들어간다."""
    u = urlparse(url)
    if u.scheme != 'mysql' or not u.hostname or not u.path.strip('/'):
        raise SystemExit(f'접속 주소 형식이 아니다: {url} (mysql://계정:비밀번호@호스트:포트/database)')
    if u.hostname not in LOCAL_HOSTS:
        raise SystemExit(f'{label} 주소가 이 PC 가 아니라 멈춘다({u.hostname}). 시드는 테이블을 비우고 다시 채우는 로컬 전용이다.')
    return pymysql.connect(host=u.hostname, port=u.port or 3306, user=unquote(u.username or ''),
                           password=unquote(u.password or ''), database=u.path.strip('/'), charset='utf8mb4')


def refuse_shared(conn, label: str) -> None:
    """포트 포워딩으로 로컬 주소에 원격 DB 를 붙였을 때를 한 번 더 막는다(RDS MySQL 의 basedir 은 /rdsdbbin/ 아래다)."""
    with conn.cursor() as cur:
        cur.execute('SELECT @@basedir')
        basedir = cur.fetchone()[0]
    if basedir.startswith('/rdsdbbin/'):
        raise SystemExit(f'{label} 가 RDS 로 보여 멈춘다({basedir}). 시드는 로컬 전용이다.')


def existing(cur, tables: list[str]) -> list[str]:
    """지금 database 에 있는 것만 — 순서는 그대로"""
    cur.execute('SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE()')
    present = {name for (name,) in cur.fetchall()}
    return [table for table in tables if table in present]


def truncate(cur, tables: list[str]) -> None:
    cur.execute('SET FOREIGN_KEY_CHECKS = 0')
    for table in tables:
        cur.execute(f'TRUNCATE TABLE {table}')
    cur.execute('SET FOREIGN_KEY_CHECKS = 1')


def summary(shop, mock) -> None:
    with shop.cursor() as cur:
        cur.execute("SELECT (SELECT COUNT(*) FROM customers), (SELECT COUNT(*) FROM preorders), "
                    "(SELECT COUNT(*) FROM orders), (SELECT COUNT(*) FROM product_reviews), "
                    "(SELECT COUNT(*) FROM preorder_outbox_events WHERE published_at IS NULL), "
                    "(SELECT COUNT(*) FROM preorder_sync_jobs WHERE status = 'DEAD_LETTER')")
        c, p, o, r, unpublished, dlq = cur.fetchone()
        print(f'  회원 {c:,} · 예약 {p:,} · 주문 {o:,} · 후기 {r:,} · 미발행 아웃박스 {unpublished:,} · 등록 DLQ {dlq:,}')
        cur.execute('SELECT pr.title, p.status, COUNT(*) FROM preorders p JOIN products pr ON pr.id = p.product_id '
                    'GROUP BY pr.title, p.status ORDER BY pr.title, p.status')
        for title, status, count in cur.fetchall():
            print(f'  {title:14} {status:13} {count:>6,}')
    with mock.cursor() as cur:
        cur.execute('SELECT status, COUNT(*) FROM preorder_registrations GROUP BY status ORDER BY status')
        print('  Mock 원장 ' + ' · '.join(f'{s} {n:,}' for s, n in cur.fetchall()))


def main() -> int:
    # Windows 에서 출력을 파일 · 다른 프로그램으로 넘기면 cp949 로 쓰려다 "⇔" 같은 글자에서 멈춘다
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description='로컬 시드 — be shop 과 Mock external_mock 을 비우고 다시 채운다')
    parser.add_argument('--shop', default=os.environ.get('SEED_SHOP_URL'),
                        help='be shop DB. 예: mysql://root:비밀번호@127.0.0.1:3306/shop (환경 변수 SEED_SHOP_URL)')
    parser.add_argument('--mock', default=os.environ.get('SEED_MOCK_URL', DEFAULT_MOCK_URL),
                        help=f'Mock DB (기본 {DEFAULT_MOCK_URL}, 환경 변수 SEED_MOCK_URL)')
    parser.add_argument('--reconcile-demo', action='store_true',
                        help='be · Mock 어긋남 15건(유령 · 누락 · 번호 · 상태 · 내용 × 3)을 더 넣는다')
    args = parser.parse_args()
    if not args.shop:
        parser.error('--shop 이나 SEED_SHOP_URL 로 be shop DB 주소를 준다')

    started = time.monotonic()
    shop, mock = connect(args.shop, 'shop DB'), connect(args.mock, 'Mock DB')
    refuse_shared(shop, 'shop DB')
    refuse_shared(mock, 'Mock DB')

    # 모든 단계가 같은 기준 시각으로 상대 시각을 잰다
    now = utc_now()
    print(f'▶ shop 비우고 채우기 (기준 시각 {now:%Y-%m-%d %H:%M:%S} UTC)')
    with shop.cursor() as cur:
        truncate(cur, SHOP_TABLES + existing(cur, OPTIONAL_SHOP_TABLES))
        cat = catalog.seed(cur, now)
        campaigns = campaign.seed(cur, now, cat)
        people = customers.seed(cur, now)
        seeded = preorders.seed(cur, now, cat, campaigns, people)
        dead_letters.seed(cur, now, seeded.plans)
        orders.seed(cur, now, cat, people, seeded.orders)
    shop.commit()

    print('▶ external_mock 비우고 채우기')
    with mock.cursor() as cur:
        truncate(cur, ['preorder_registrations'])
        external_mock.seed(cur, seeded.plans)
    mock.commit()

    print('▶ 검증')
    failed = verify.run(shop, mock, now)

    if args.reconcile_demo:
        print('▶ 정합성 시연 세트 넣기 (be · Mock 어긋남 15건)')
        with mock.cursor() as cur:
            external_mock.reconcile_demo(cur, seeded.plans, cat, now)
        mock.commit()

    print('▶ 요약')
    summary(shop, mock)
    shop.close()
    mock.close()

    if failed:
        print(f'검증 실패 {failed}항목 — 위 어긋남을 확인한다.', file=sys.stderr)
        return 1
    print(f'완료 ({time.monotonic() - started:.0f}초)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
