"""카탈로그: 카테고리 14(2단계) · 사전예약 모델 15 · 일반 판매 상품 40 · 일반 판매 재고. 프론트(fe develop)의 화면 목업에 맞춘다.

  카테고리   헤더 메뉴(entities/product/model/categoryMenu.ts brandMenus) — 모바일 · PC/주변기기 · 웨어러블 아래 하위 카테고리.
             메뉴 썸네일(widgets/category-nav/model/menu.ts)이 하위 카테고리 이름으로 붙는다. 브랜드(Apple · Samsung · LG)는
             프론트의 제조사 필터인데 be 에 브랜드 컬럼이 없어 넣지 않는다
  사전예약   사전예약 화면 목업(entities/preorder/model/mockPreorders.ts)의 모델 15개 — 이름 · 가격 · 혜택 문구(설명에 둔다)
  일반 판매  상품 목업(shared/api/mock/fixtures/product.ts)의 일반 상품 — NOVA 스마트폰 1~8 · 태블릿 · 노트북 · 모니터 · 키보드 ·
             스마트워치 · 무선이어폰, 품절 스마트밴드 · 판매 중지 마우스. 시작가 + 50,000원씩
  옵션       목업과 같은 색상 4가지(실버 · 블러쉬 · 시트러스 · 인디고)와 사진(/images/macbook_neo_{색}{1~4}.png, 프론트 public 에 있음).
             저장 용량(256GB · 512GB +130,000원)은 용량이 있는 기기(폰 · 태블릿 · 노트북)에만 둔다 — 목업은 모든 상품에 두지만
             시계 · 이어폰 · 키보드에 용량 옵션은 맞지 않는다
  보증       스마트폰 · 노트북 일반 상품만(목업은 모두 미제공). 사전예약 주문에는 보증을 붙일 수 없다

상품은 번호로 계획한다 — 1~15 사전예약(1 아이폰 18 Pro = 예약 3,000건, 2 갤럭시 Z 폴드8 = 1,000건, 3 아이폰 Duo = 오픈 전,
4 맥북 네오 = 부하 시험용), 101~140 일반 판매. id(UUID v7)는 넣을 때 만든다.
옵션 정의는 상품의 options(JSON) 한 칸에 둔다 — catalog ProductOptions 모양 그대로다:
  {"axes":[{"key","label","values":[{"id","value","normalized","hex","surcharge","images":[{"url","primary"}]}]}],
   "defaultImages":[], "detailImages":[], "warranty":{"offered","surcharge"}}
옵션 행(product_options)의 표시명 · 조합 키 · 필터 JSON 은 catalog OptionCombination 규칙을 따른다:
  표시명   축 순서의 값 표시명을 " / " 로 잇는다(실버 / 256GB)
  조합 키  값 id 를 "짧은 것 먼저, 같으면 문자열 순" 으로 '-' 로 잇는다
  필터     color · storage 축의 정규화값 JSON(용량은 공백 없이 대문자 — 256GB)
  가격     상품 기본가 + 고른 값의 추가금
썸네일은 첫 색상의 첫 사진이다(ProductOptions.thumbnailUrl 규칙).
"""
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .common import hex_id, insert, uuid7

PREORDER, IN_STOCK = 'PREORDER', 'IN_STOCK'

# 상위 → 하위(메뉴 순서). 메뉴는 sort_order 순, 같으면 id 순이다
CATEGORY_TREE = [
    ('모바일', ['스마트폰', '태블릿', '폴더블']),
    ('PC/주변기기', ['노트북', '데스크탑', '모니터', '키보드', '마우스']),
    ('웨어러블', ['스마트워치', '무선이어폰', '스마트밴드']),
]

# 색상: (sku 코드, 표시명, hex, 사진 파일 이름의 색 — 프론트 파일 이름이 sliver 다)
COLORS = [('SLV', '실버', '#D9D9DE', 'sliver'), ('BLS', '블러쉬', '#E8B4B8', 'blush'),
          ('CTR', '시트러스', '#D9F523', 'citrus'), ('IDG', '인디고', '#3B3A6E', 'indigo')]
STORAGES = [('256', '256GB', 0), ('512', '512GB', 130_000)]

# 사전예약 모델: (번호, sku 코드, 하위 카테고리, 이름, 기본가, 용량 옵션, 설명 — 사전예약 혜택 문구)
PREORDER_MODELS = [
    (1, 'IP18P', '스마트폰', '아이폰 18 Pro', 2_278_100, True, '사전예약 시 맥세이프 케이스 증정'),
    (2, 'FOLD8', '폴더블', '갤럭시 Z 폴드8', 2_398_000, True, '저장용량 무료 업그레이드'),
    (3, 'IPDUO', '스마트폰', '아이폰 Duo', 1_890_000, True, '알림 신청자 대상 쿠폰 지급'),
    (4, 'MBNEO', '노트북', '맥북 네오', 1_690_000, True, '카드 결제 시 10만 원 할인'),
    (5, 'IP18PM', '스마트폰', '아이폰 18 Pro Max', 2_590_000, True, '사전예약 시 맥세이프 케이스 증정'),
    (6, 'WATCH40', '스마트워치', '갤럭시 워치 40mm', 399_000, False, '스트랩 1종 추가 증정'),
    (7, 'WATCH44', '스마트워치', '갤럭시 워치 44mm', 429_000, False, '스트랩 1종 추가 증정'),
    (8, 'IPDUOP', '스마트폰', '아이폰 Duo Plus', 2_090_000, True, '알림 신청자 대상 쿠폰 지급'),
    (9, 'S26U', '스마트폰', '갤럭시 S26 울트라', 1_798_000, True, '버즈4 프로 50% 할인 구매'),
    (10, 'IPDPRO11', '태블릿', '아이패드 프로 11', 1_299_000, True, '펜슬 프로 할인 구매'),
    (11, 'IPDPRO13', '태블릿', '아이패드 프로 13', 1_699_000, True, '펜슬 프로 할인 구매'),
    (12, 'AP3', '무선이어폰', '에어팟 프로 3', 369_000, False, '각인 서비스 무료'),
    (13, 'AP3USB', '무선이어폰', '에어팟 프로 3 (USB-C 케이스)', 389_000, False, '각인 서비스 무료'),
    (14, 'BZ4P', '무선이어폰', '갤럭시 버즈4 프로', 299_000, False, '무선 충전 패드 증정'),
    (15, 'AWS11', '스마트워치', '애플워치 시리즈 11', 599_000, False, '밴드 1종 추가 증정'),
]

# 일반 판매 상품군: (하위 카테고리, sku 코드, 개수, 시작가, 용량 옵션, 보증 추가금) — 이름은 "NOVA {하위 카테고리} {i}"
IN_STOCK_LINES = [
    ('스마트폰', 'SP', 8, 1_250_000, True, 149_000),
    ('태블릿', 'TB', 5, 890_000, True, 0),
    ('노트북', 'NB', 6, 1_290_000, True, 199_000),
    ('모니터', 'MN', 4, 450_000, False, 0),
    ('키보드', 'KB', 4, 89_000, False, 0),
    ('스마트워치', 'SW', 5, 390_000, False, 0),
    ('무선이어폰', 'EP', 6, 259_000, False, 0),
]
# 목록의 품절 · 판매 중지 표시용: (하위 카테고리, sku 코드, 이름, 기본가, 재고, 판매 중지)
IN_STOCK_EXTRAS = [
    ('스마트밴드', 'BAND', 'NOVA 스마트밴드 (품절)', 79_000, 0, False),
    ('마우스', 'MOUSE', 'NOVA 마우스 (판매 중지)', 59_000, 30, True),
]
STOCK = 30  # 옵션마다 배정한 총량
# 제조사(be products.brand — CHECK 로 이 셋만 받는다). 사전예약 모델은 이름대로, NOVA 일반 상품은 브랜드 필터에 셋이 다 보이게 번갈아
BRANDS = ['SAMSUNG', 'APPLE', 'LG']


@dataclass(frozen=True)
class Option:
    id: uuid.UUID
    product_no: int
    product_id: uuid.UUID
    sku: str
    title: str
    price: int
    warranty_surcharge: int  # 보증을 제공하지 않으면 0
    color: int               # COLORS 의 번호
    storage: int | None      # STORAGES 의 번호. 용량 옵션이 없으면 None
    orderable: bool          # 판매 중이고 재고가 있다 — 일반 주문 · 장바구니가 이 옵션만 고른다

    @property
    def warranty_offered(self) -> bool:
        return self.warranty_surcharge > 0


@dataclass
class Catalog:
    product_ids: dict[int, uuid.UUID] = field(default_factory=dict)
    product_titles: dict[int, str] = field(default_factory=dict)
    options: dict[uuid.UUID, Option] = field(default_factory=dict)
    picks: dict[tuple[int, int, int | None], Option] = field(default_factory=dict)  # (상품, 색, 용량) → 옵션
    in_stock_options: list[Option] = field(default_factory=list)  # 일반 판매 · 주문 가능 옵션 — 주문 · 장바구니가 번호로 고른다


@dataclass(frozen=True)
class _Product:
    no: int
    code: str
    category: str
    mode: str
    title: str
    base_price: int
    storage: bool
    description: str | None
    warranty: int = 0
    stock: int = STOCK
    paused: bool = False


def _products() -> list[_Product]:
    rows = [_Product(no, code, category, PREORDER, title, price, storage, description)
            for no, code, category, title, price, storage, description in PREORDER_MODELS]
    no = 100
    for category, code, count, price, storage, warranty in IN_STOCK_LINES:
        for i in range(1, count + 1):
            no += 1
            rows.append(_Product(no, f'{code}{i}', category, IN_STOCK, f'NOVA {category} {i}', price + (i - 1) * 50_000,
                                 storage, None, warranty))
    for category, code, title, price, stock, paused in IN_STOCK_EXTRAS:
        no += 1
        rows.append(_Product(no, code, category, IN_STOCK, title, price, False, None, 0, stock, paused))
    return rows


def _brand(p: _Product) -> str:
    if p.mode == PREORDER:
        return 'SAMSUNG' if p.title.startswith('갤럭시') else 'APPLE'
    return BRANDS[p.no % len(BRANDS)]


def _axes(p: _Product) -> list[dict]:
    colors = {'key': 'color', 'label': '색상', 'values': [
        {'id': hex_id(p.no, 'color', name), 'value': name, 'normalized': name, 'hex': hex_color, 'surcharge': 0,
         'images': [{'url': f'/images/macbook_neo_{file}{shot}.png', 'primary': shot == 1} for shot in range(1, 5)]}
        for _, name, hex_color, file in COLORS]}
    if not p.storage:
        return [colors]
    storages = {'key': 'storage', 'label': '저장 용량', 'values': [
        {'id': hex_id(p.no, 'storage', label), 'value': label, 'normalized': label, 'hex': None, 'surcharge': extra,
         'images': []}
        for _, label, extra in STORAGES]}
    return [colors, storages]


def _key_order(value_id: str) -> tuple[int, str]:
    return len(value_id), value_id


def seed(cur, now: datetime) -> Catalog:
    at = now - timedelta(days=90)
    category_ids, categories = {}, []
    for order, (parent, children) in enumerate(CATEGORY_TREE):
        parent_id = category_ids[parent] = uuid7(at, 'category', parent)
        categories.append((parent_id.bytes, None, parent, order, at, at))
        for child_order, child in enumerate(children):
            child_id = category_ids[child] = uuid7(at, 'category', parent, child)
            categories.append((child_id.bytes, parent_id.bytes, child, child_order, at, at))
    insert(cur, 'categories', ['id', 'parent_id', 'name', 'sort_order', 'created_at', 'updated_at'], categories)

    catalog = Catalog()
    products, options, inventories = [], [], []
    for p in _products():
        created = at + timedelta(minutes=p.no)
        product_id = uuid7(created, 'product', p.no)
        catalog.product_ids[p.no] = product_id
        catalog.product_titles[p.no] = p.title
        axes = _axes(p)
        document = {'axes': axes, 'defaultImages': [], 'detailImages': [],
                    'warranty': {'offered': p.warranty > 0, 'surcharge': p.warranty}}
        thumbnail = axes[0]['values'][0]['images'][0]['url']
        # 모델명(be products.model_name)은 sku 코드를 쓴다
        products.append((product_id.bytes, None, category_ids[p.category].bytes, p.mode, p.title, _brand(p), p.code,
                         p.base_price,
                         json.dumps(document, ensure_ascii=False), thumbnail, p.description, None,
                         # 판매 중지는 be 관리자 기능처럼 상품 상태만 바꾼다(옵션은 그대로) — 회원 목록에서 빠지고 상세에 판매 중지로 보인다
                         'PAUSED' if p.paused else 'ACTIVE',
                         # 일반 상품의 판매 준비(be products.stock_ready_at) — 재고 행을 같이 넣으니 만든 시각. 비면 catalog 가 목록에서 빼고
                         # 상세를 404 로 준다. 사전예약 상품은 회차가 준비를 정해 NULL
                         created if p.mode == IN_STOCK else None, 1, None,
                         created, created))

        storages = list(enumerate(STORAGES)) if p.storage else [(None, None)]
        for c, (color_code, *_) in enumerate(COLORS):
            for s, storage in storages:
                picks = [(axes[0], axes[0]['values'][c])] + ([(axes[1], axes[1]['values'][s])] if storage else [])
                sku = f'{p.code}-{color_code}' + (f'-{storage[0]}' if storage else '')
                option_id = uuid7(created, 'option', sku)
                filters = {axis['key']: value['normalized'] for axis, value in picks}
                option = Option(option_id, p.no, product_id, sku, ' / '.join(value['value'] for _, value in picks),
                                p.base_price + sum(value['surcharge'] for _, value in picks), p.warranty, c, s,
                                p.mode == IN_STOCK and not p.paused and p.stock > 0)
                catalog.options[option_id] = option
                catalog.picks[(p.no, c, s)] = option
                options.append((option_id.bytes, product_id.bytes, sku, option.title, option.price,
                                json.dumps(filters, ensure_ascii=False),
                                '-'.join(sorted((value['id'] for _, value in picks), key=_key_order)),
                                'ACTIVE', created, created))
                if p.mode == IN_STOCK:
                    if option.orderable:
                        catalog.in_stock_options.append(option)
                    # reserved · sold 는 일반 주문을 넣은 뒤 orders 단계가 주문 수량으로 맞춘다
                    inventories.append((option_id.bytes, p.stock, 0, 0, created))

    insert(cur, 'products', ['id', 'idempotency_key', 'category_id', 'sale_mode', 'title', 'brand', 'model_name',
                             'base_price', 'options',
                             'thumbnail_url', 'description', 'tags', 'status', 'stock_ready_at', 'visible',
                             'campaign_canceled_at',
                             'created_at', 'updated_at'], products)
    insert(cur, 'product_options', ['id', 'product_id', 'sku', 'title', 'price', 'filter_attributes',
                                    'combination_key', 'status', 'created_at', 'updated_at'], options)
    insert(cur, 'option_inventories', ['option_id', 'stock_total', 'stock_reserved', 'stock_sold', 'updated_at'],
           inventories)
    return catalog
