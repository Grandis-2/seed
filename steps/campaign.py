"""사전예약 회차 15 · 배송 차수 30. 프론트 사전예약 화면(entities/preorder/model/mockPreorders.ts)의 진행 중 3 · 오픈 예정 3 · 마감 4
프로모션을 모델마다 회차 하나로 만든다(be 는 모델마다 회차 하나다). 시각은 기준 시각에서 잰 상대값이라 언제 돌려도 같은 상태로 보인다.

  마감     아이폰 18 Pro · Pro Max(10일 전 ~ 3일 전 — 1번이 예약 3,000건), 에어팟 프로 3 · USB-C, 애플워치 시리즈 11, 갤럭시 버즈4 프로
  진행 중  갤럭시 Z 폴드8(2일 전 ~ 5일 뒤 — 예약 1,000건), 갤럭시 워치 40 · 44mm, 맥북 네오(부하 시험용, 예약 0)
  오픈 예정 아이폰 Duo · Plus(9일 뒤, 오픈 알림 전), 갤럭시 S26 울트라(23일 뒤), 아이패드 프로 11 · 13(30일 뒤)

배송 차수는 프론트 목업처럼 1차 1~500번 · 2차 501번~(상한 없음)이다.
next_queue_position 은 1 로 넣고, 예약을 넣은 뒤 preorders 단계가 "최대 순번 + 1" 로 맞춘다.
schedule_version · visibility_version 은 0 이다 — 회차 변경 이벤트를 낸 적 없는 회차라는 뜻이다. 대기열(waitingroom)은 이벤트로 일정을
받으므로 시드 회차를 대기열에 알리려면 관리자 회차 재발행을 한 번 부른다.
"""
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from .catalog import Catalog
from .common import insert, seconds, uuid7

# 차수: (번호, 시작 순번, 끝 순번, 예상 배송 시작 · 끝 = 마감일 + 일)
BATCHES = [(1, 1, 500, 4, 6), (2, 501, None, 11, 13)]

# 상품 → (오픈, 마감) 기준 시각에서 잰 일. 1 · 2 는 예약 계획(preorders)의 접수 시각 · 기한과 맞물려 있다
SCHEDULES = {
    1: (-10, -3), 5: (-10, -3),          # 아이폰 18 Pro · Pro Max — 마감
    12: (-40, -27), 13: (-40, -27),      # 에어팟 프로 3 · USB-C — 마감
    15: (-40, -27),                      # 애플워치 시리즈 11 — 마감
    14: (-54, -41),                      # 갤럭시 버즈4 프로 — 마감
    2: (-2, 5),                          # 갤럭시 Z 폴드8 — 진행 중
    6: (-10, 4), 7: (-10, 4),            # 갤럭시 워치 40 · 44mm — 진행 중
    4: (-9, 9),                          # 맥북 네오 — 진행 중(부하 시험용)
    3: (9, 22), 8: (9, 22),              # 아이폰 Duo · Plus — 오픈 예정
    9: (23, 36),                         # 갤럭시 S26 울트라 — 오픈 예정
    10: (30, 43), 11: (30, 43),          # 아이패드 프로 11 · 13 — 오픈 예정
}


@dataclass(frozen=True)
class Campaign:
    product_no: int
    opens_at: datetime
    closes_at: datetime
    batch_ids: dict[int, uuid.UUID]  # 차수 번호 → 배송 차수 id

    def batch_id(self, position: int) -> uuid.UUID:
        """순번 구간에 맞는 배송 차수."""
        return self.batch_ids[1 if position <= 500 else 2]


def seed(cur, now: datetime, catalog: Catalog) -> dict[int, Campaign]:
    campaigns, rows, batches = {}, [], []
    for product_no, (open_day, close_day) in sorted(SCHEDULES.items()):
        opens, closes = now + timedelta(days=open_day), now + timedelta(days=close_day)
        created = opens - timedelta(days=7)
        notified = opens <= now  # 오픈 알림은 오픈한 회차만 보냈다
        # 상품명 · 판매 상태는 catalog 등록 이벤트가 실어 온 사본이다(오픈 알림 문구 · 판정에 쓴다). 시드 사전예약 상품은 모두 판매 중
        rows.append((catalog.product_ids[product_no].bytes, opens, closes, 0, 1, 0, catalog.product_titles[product_no], 'ACTIVE', 1,
                     opens + seconds(1) if notified else None, created, opens if notified else created))
        batch_ids = {n: uuid7(created, 'batch', product_no, n) for n, *_ in BATCHES}
        campaigns[product_no] = Campaign(product_no, opens, closes, batch_ids)
        batches += [(batch_ids[n].bytes, catalog.product_ids[product_no].bytes, n, start, end,
                     closes.date() + timedelta(days=s), closes.date() + timedelta(days=e), created)
                    for n, start, end, s, e in BATCHES]
    insert(cur, 'preorder_campaigns', ['product_id', 'opens_at', 'closes_at', 'schedule_version', 'visible',
                                       'visibility_version', 'product_title', 'product_status', 'next_queue_position',
                                       'open_notified_at',
                                       'created_at', 'updated_at'], rows)
    insert(cur, 'shipment_batches', ['id', 'product_id', 'batch_number', 'position_from', 'position_to',
                                     'estimated_ship_start', 'estimated_ship_end', 'created_at'], batches)
    return campaigns
