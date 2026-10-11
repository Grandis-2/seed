"""여러 단계가 같이 쓰는 것 — 기준 시각 · id(UUID v7) · 공개 UUID · 묶어 넣기 · 이름 가리기."""
import hashlib
import uuid
from datetime import datetime, timedelta, timezone

# Faker · random 의 시작값. 고정해야 누가 돌려도 같은 데이터가 나온다(Faker 버전도 requirements.txt 로 고정).
SEED = 42


def utc_now() -> datetime:
    """기준 시각. 초 아래는 버린다.

    be · Mock 모두 DB 에 UTC 로 저장한다. DATETIME 칸에는 시간대가 없으므로 UTC 값을 그대로 넣는다.
    모든 시각은 이 값에서 잰 상대값이다 — 단계마다 지금 시각을 따로 읽으면 사이에 시간이 흘러 서로 어긋난다.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _digest(*parts) -> bytes:
    return hashlib.md5('-'.join(['seed', *map(str, parts)]).encode()).digest()


def uuid7(at: datetime, *parts) -> uuid.UUID:
    """be 엔티티 id(UUID v7). 앞 48비트는 그 행이 생긴 시각(밀리초), 나머지는 parts 의 해시라 같은 입력이면 같은 id 다.

    be 는 Hibernate @UuidGenerator(VERSION_7)로 INSERT 전에 만든다. 시각이 앞에 있어 새 행이 클러스터 인덱스 끝에 붙고,
    id 순서가 만든 순서와 같다 — 그래서 시각 자리에 실제 생성 시각을 넣는다. 기준 시각이 실행마다 달라 id 도 실행마다 바뀐다.
    DB 에는 표준 바이트 순서(UUID_TO_BIN 기본값과 같다)로 binary(16) 에 넣는다 — 넣을 때는 .bytes 를 쓴다.
    """
    millis = int(at.replace(tzinfo=timezone.utc).timestamp() * 1000)
    rand = int.from_bytes(_digest('uuid7', *parts)[:10], 'big')  # 80비트 중 74비트를 쓴다
    value = (millis & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76                                    # 버전 7
    value |= ((rand >> 62) & 0xFFF) << 64                 # rand_a 12비트
    value |= 0b10 << 62                                   # 변형(RFC 9562)
    value |= rand & ((1 << 62) - 1)                       # rand_b 62비트
    return uuid.UUID(int=value)


def uuid_of(*parts) -> str:
    """같은 입력이면 같은 값이 나오는 소문자 UUID 모양 문자열(공개 UUID · 이벤트 id · 메시지 id).

    be 는 공개 UUID 를 소문자 표준형으로만 받는다(UUID.fromString(v).toString() 이 v 와 같아야 한다).
    MD5 32자를 8-4-4-4-12 로 끊으면 그 모양이 된다.
    """
    digest = _digest(*parts).hex()
    return f'{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:]}'


def hex_id(*parts) -> str:
    """32자 16진수 — 카탈로그 옵션 값 id(ProductOptions.newValueId 와 같은 모양, '-' 가 없다)."""
    return _digest('value', *parts).hex()


def seconds(n: float) -> timedelta:
    return timedelta(seconds=n)


def mask_name(name: str | None) -> str:
    """후기 작성자 표시 — 첫 글자 + "**", 이름이 비면 "***"(catalog ReviewService.mask 와 같다)."""
    name = (name or '').strip()
    return name[0] + '**' if name else '***'


def insert(cur, table: str, columns: list[str], rows: list[tuple], batch: int = 1000) -> int:
    """여러 행을 묶어 넣는다. PyMySQL 의 executemany 는 INSERT … VALUES 를 여러 행짜리 한 문장으로 합쳐 보낸다."""
    if not rows:
        return 0
    sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})"
    for start in range(0, len(rows), batch):
        cur.executemany(sql, rows[start:start + batch])
    return len(rows)
