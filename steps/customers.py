"""회원 5,005명: 시연 계정 1~5 + 일반 6~5,005.

5,000명은 부하 시험(10초 5,000건, 1인 1매)에 필요한 수다. 예약은 이 회원들이 한 것이고 예약 수만큼 회원이 따로 생기지 않는다.
이름 · 이메일 · 도로명은 Faker(ko_KR)로 만든다. 시작값을 고정해서 누가 돌려도 같은 값이 나온다.

Faker 를 그대로 쓰지 않는 칸:
  전화번호 — ko_KR 이 지역번호(053-…)도 섞어 내서 휴대폰 형식(010-####-####)으로 만든다
  주소     — ko_KR 의 주소는 시와 구가 서로 맞지 않고("제주특별자치도 부천시"), 도로명은 서울 도로뿐이다("대전 테헤란로").
             시 · 구 · 우편번호 앞자리 · 도로명 후보는 실제로 맞는 목록에 두고, 그 안에서 고르는 일과 번지 · 우편번호
             뒷자리만 Faker 의 난수로 한다
배송지는 모두 채워 넣고, 주문이 없는 회원 중 일부만 orders 단계가 비운다(주문은 배송지를 복사해야 해서).
"""
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from faker import Faker

from .common import SEED, insert, uuid7

TOTAL = 5005
DEMO = 5

# (시 · 도 + 시 · 군 · 구, 우편번호 앞 두 자리, 그 지역의 도로명)
REGIONS = [
    ('서울특별시 강남구', '06', ['테헤란로', '선릉로', '도산대로', '압구정로']),
    ('서울특별시 마포구', '03', ['월드컵북로', '양화로', '독막로', '마포대로']),
    ('서울특별시 송파구', '05', ['올림픽로', '송파대로', '백제고분로', '잠실로']),
    ('서울특별시 노원구', '01', ['동일로', '노원로', '상계로', '한글비석로']),
    ('부산광역시 해운대구', '48', ['해운대해변로', '센텀중앙로', '좌동순환로', '해운대로']),
    ('대구광역시 수성구', '42', ['달구벌대로', '동대구로', '수성로', '들안로']),
    ('인천광역시 연수구', '21', ['컨벤시아대로', '송도과학로', '인천타워대로', '앵고개로']),
    ('광주광역시 서구', '61', ['상무대로', '상무중앙로', '화정로', '운천로']),
    ('대전광역시 유성구', '34', ['대학로', '유성대로', '계룡로', '엑스포로']),
    ('울산광역시 남구', '44', ['삼산로', '번영로', '돋질로', '문수로']),
    ('세종특별자치시', '30', ['한누리대로', '도움로', '갈매로', '보듬로']),
    ('경기도 성남시 분당구', '13', ['판교역로', '분당로', '황새울로', '정자일로']),
    ('경기도 수원시 영통구', '16', ['광교중앙로', '봉영로', '영통로', '매영로']),
    ('경기도 고양시 일산동구', '10', ['중앙로', '정발산로', '일산로', '장백로']),
    ('강원특별자치도 춘천시', '24', ['중앙로', '공지로', '퇴계로', '후석로']),
    ('충청북도 청주시 흥덕구', '28', ['직지대로', '가로수로', '풍산로', '2순환로']),
    ('전북특별자치도 전주시 완산구', '55', ['팔달로', '기린대로', '홍산로', '효자로']),
    ('경상남도 창원시 성산구', '51', ['중앙대로', '원이대로', '창원대로', '외동반림로']),
    ('경상북도 포항시 남구', '37', ['포스코대로', '희망대로', '대이로', '상공로']),
    ('제주특별자치도 제주시', '63', ['연북로', '노형로', '중앙로', '연삼로']),
]


@dataclass(frozen=True)
class Customer:
    no: int          # 계획용 번호 1~5,005. 시연 계정은 1~5
    id: uuid.UUID
    name: str
    display_name: str
    phone: str
    postal_code: str
    line1: str
    line2: str | None


def seed(cur, now: datetime) -> dict[int, Customer]:
    fake = Faker('ko_KR')
    fake.seed_instance(SEED)
    customers, rows = {}, []
    for n in range(1, TOTAL + 1):
        demo = n <= DEMO
        # Faker 를 부르는 순서가 바뀌면 값이 바뀐다. 칸을 늘리면 맨 뒤에서 부른다.
        name = f'시연 사용자 {n}' if demo else fake.name()
        display_name = fake.user_name() if not demo and n % 4 == 0 else name
        if demo:
            email = f'demo{n}@example.com'
        elif n % 10 == 3:
            email = None  # 카카오 이메일 동의 거부
        else:
            email = fake.safe_email()  # example.org · example.net 처럼 예시용으로 예약된 도메인만 나온다
        phone = fake.numerify('010-####-####')
        region, postal_prefix, roads = REGIONS[n % len(REGIONS)]
        line1 = f'{region} {fake.random_element(roads)} {fake.random_int(1, 300)}'
        line2 = None if n % 7 == 0 else f'{101 + n % 15}동 {(1 + n % 25) * 100 + 1 + n % 4}호'
        postal = postal_prefix + fake.numerify('###')
        joined = now - timedelta(days=30 + n % 300) + timedelta(seconds=n % 86400)  # 모든 예약보다 앞선다

        customer_id = uuid7(joined, 'customer', n)
        customers[n] = Customer(n, customer_id, name, display_name, phone, postal, line1, line2)
        # kakao_id 는 진짜 카카오 회원 번호와 겹치지 않게 접두어를 붙인다
        rows.append((customer_id.bytes, f'seed-{n:05d}', display_name, name, email, phone,
                     name, phone, postal, line1, line2, joined, joined))

    insert(cur, 'customers', ['id', 'kakao_id', 'display_name', 'name', 'email', 'phone_number',
                              'default_ship_to_name', 'default_ship_to_phone', 'default_ship_to_postal_code',
                              'default_ship_to_line1', 'default_ship_to_line2', 'created_at', 'updated_at'], rows)
    return customers


def clear_addresses(cur, customer_ids: list[uuid.UUID]) -> None:
    """기본 배송지를 비운다. 다섯 칸이 모두 있거나 모두 없어야 한다(CHECK ck_customer_default_address)."""
    if not customer_ids:
        return
    cur.execute(
        'UPDATE customers SET default_ship_to_name = NULL, default_ship_to_phone = NULL, '
        'default_ship_to_postal_code = NULL, default_ship_to_line1 = NULL, default_ship_to_line2 = NULL '
        f"WHERE id IN ({', '.join(['%s'] * len(customer_ids))})", [c.bytes for c in customer_ids])
