"""봇이 쓰는 DB 조회 함수 (동기 함수라 봇에서는 asyncio.to_thread로 호출한다)."""
import os
import re

import pymysql
from pymysql.cursors import DictCursor

SEARCH_LIMIT = 25  # 디스코드 선택 메뉴 최대 개수

PERK_COLUMNS = ("id, name_ko, name_en, description_ko, description_en, former_name_ko, former_name_en, "
                "role, owner_name_ko, owner_name_en, image_url")

# 생존자 아이템 종류 한글 이름 (게임 내 아이템 이름 기준)
ITEM_TYPE_KO = {"toolbox": "공구상자", "medkit": "구급상자", "flashlight": "손전등", "map": "지도",
                "key": "열쇠", "fogvial": "안개 유리병", "firecracker": "폭죽"}


def connect():
    # 'localhost'는 이 PC에서 이름 확인에 연결마다 약 2초가 걸려서 IP로 쓴다.
    return pymysql.connect(host=os.environ.get("DB_HOST", "127.0.0.1"),
                           user=os.environ.get("DB_USER", "root"),
                           password=os.environ["DB_PASSWORD"],
                           database=os.environ.get("DB_NAME", "dbd"),
                           charset="utf8mb4", cursorclass=DictCursor)


def _like(term):
    esc = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


def _quote_variants(term):
    """이름에는 ’ ‘ 같은 특수 따옴표가 쓰이므로 일반 ' 로 입력해도 찾을 수 있게 한다."""
    return list(dict.fromkeys([term, term.replace("'", "’"), term.replace("'", "‘")]))


def _search_ids(frm, id_col, name_cols, owner_cols, desc_cols, term, limit):
    """이름 정확히 일치 > 이름 포함 > 소유자/소속 이름 포함 > 설명 포함 순으로 ID를 돌려준다."""
    variants = _quote_variants(term)
    like = _like(term)
    exact = " OR ".join(f"{c} = %s" for c in name_cols for _ in variants)
    exact_args = [v for _ in name_cols for v in variants]
    name = " OR ".join(f"{c} LIKE %s" for c in name_cols for _ in variants)
    name_args = [_like(v) for _ in name_cols for v in variants]
    owner = " OR ".join(f"{c} LIKE %s" for c in owner_cols) or "0"  # 소속 컬럼이 없으면 항상 거짓
    owner_args = [like] * len(owner_cols)
    desc = " OR ".join(f"{c} LIKE %s" for c in desc_cols)
    desc_args = [like] * len(desc_cols)

    sql = f"""
        SELECT {id_col} AS id,
               MAX(COALESCE({exact}, 0)) AS is_exact,
               MAX(COALESCE({name}, 0)) AS by_name,
               MAX(COALESCE({owner}, 0)) AS by_owner,
               MIN({name_cols[0]}) AS sort_name
        FROM {frm}
        WHERE ({name}) OR ({owner}) OR ({desc})
        GROUP BY {id_col}
        ORDER BY is_exact DESC, by_name DESC, by_owner DESC, sort_name
        LIMIT %s"""
    args = exact_args + name_args + owner_args + name_args + owner_args + desc_args + [limit]
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return [r["id"] for r in cur.fetchall()]


def _ordered(rows, ids):
    by_id = {r["id"]: r for r in rows}
    return [by_id[i] for i in ids if i in by_id]


# ---------- 퍽 ----------
def get_perks(ids):
    ids = list(ids)
    if not ids:
        return []
    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {PERK_COLUMNS} FROM perks WHERE id IN ({','.join(['%s'] * len(ids))})", ids)
        return _ordered(cur.fetchall(), ids)


def search_perks(term, limit=SEARCH_LIMIT):
    term = term.strip()
    if not term:
        return []
    ids = _search_ids("perks", "id",
                      ("name_ko", "name_en", "former_name_ko", "former_name_en"),
                      ("owner_name_ko", "owner_name_en"),
                      ("description_ko", "description_en"), term, limit)
    return get_perks(ids)


def random_perk():
    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {PERK_COLUMNS} FROM perks WHERE image_url IS NOT NULL ORDER BY RAND() LIMIT 1")
        return cur.fetchone()


# ---------- 아이템 (생존자 아이템) ----------
def get_items(ids):
    ids = list(ids)
    if not ids:
        return []
    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT * FROM items WHERE id IN ({','.join(['%s'] * len(ids))})", ids)
        return _ordered(cur.fetchall(), ids)


def search_items(term, limit=SEARCH_LIMIT):
    term = term.strip()
    if not term:
        return []
    ids = _search_ids("items", "id", ("name_ko", "name_en"), (),
                      ("description_ko", "description_en"), term, limit)
    return get_items(ids)


# ---------- 에드온 ----------
ADDON_FROM = ("addons a LEFT JOIN addon_killers ak ON ak.addon_id = a.id "
              "LEFT JOIN killers k ON k.id = ak.killer_id")


def get_addons(ids):
    """에드온 + 소속 살인마 목록(killers)."""
    ids = list(ids)
    if not ids:
        return []
    marks = ",".join(["%s"] * len(ids))
    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT * FROM addons WHERE id IN ({marks})", ids)
        addons = _ordered(cur.fetchall(), ids)
        cur.execute(f"""SELECT ak.addon_id, k.name_ko, k.name_en FROM addon_killers ak
                        JOIN killers k ON k.id = ak.killer_id WHERE ak.addon_id IN ({marks})""", ids)
        killers = {}
        for r in cur.fetchall():
            killers.setdefault(r["addon_id"], []).append(r)
    for a in addons:
        a["killers"] = killers.get(a["id"], [])
    return addons


def search_addons(term, limit=SEARCH_LIMIT):
    term = term.strip()
    if not term:
        return []
    ids = _search_ids(ADDON_FROM, "a.id", ("a.name_ko", "a.name_en"), ("k.name_ko", "k.name_en"),
                      ("a.description_ko", "a.description_en"), term, limit)
    return get_addons(ids)


def addon_owner_text(addon):
    """에드온 소속 표시: '트래퍼' 또는 '손전등 애드온'."""
    if addon["killers"]:
        return " / ".join(k["name_ko"] for k in addon["killers"])
    if addon.get("item_type"):
        return f"{ITEM_TYPE_KO.get(addon['item_type'], addon['item_type'])} 애드온"
    return "소속 없음"


# ---------- 봇 상태 저장 (예: 마지막으로 올린 신전 번호) ----------
_STATE_DDL = ("CREATE TABLE IF NOT EXISTS bot_state (state_key VARCHAR(50) NOT NULL PRIMARY KEY, "
              "state_value VARCHAR(200) NOT NULL) CHARACTER SET utf8mb4")


def get_state(key):
    with connect() as conn, conn.cursor() as cur:
        cur.execute(_STATE_DDL)
        cur.execute("SELECT state_value FROM bot_state WHERE state_key = %s", (key,))
        row = cur.fetchone()
        return row["state_value"] if row else None


def set_state(key, value):
    with connect() as conn, conn.cursor() as cur:
        cur.execute(_STATE_DDL)
        cur.execute("INSERT INTO bot_state (state_key, state_value) VALUES (%s, %s) "
                    "ON DUPLICATE KEY UPDATE state_value = VALUES(state_value)", (key, str(value)))
        conn.commit()


# ---------- 랜덤 빌드 ----------
ADDON_SLOTS = 2  # 아이템/살인마 에드온 슬롯 수


def _random_ids(sql, args=()):
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return [r["id"] for r in cur.fetchall()]


def random_build(role):
    """role='survivor': 무작위 아이템 + 그 아이템의 에드온 2개 + 생존자 퍽 4개.
       role='killer':   무작위 살인마 + 그 살인마의 에드온 2개 + 살인마 퍽 4개."""
    if role == "survivor":
        # 에드온이 존재하는 종류(손전등, 구급상자 등)의 아이템 중 이미지가 있는 것만 뽑는다.
        # (이미지가 없는 아이템은 인게임 전용이거나 이벤트 아이템이라 랜덤에서 제외한다.)
        main = get_items(_random_ids(
            "SELECT id FROM items WHERE image_url IS NOT NULL AND item_type IN "
            "(SELECT DISTINCT item_type FROM addons WHERE item_type IS NOT NULL) ORDER BY RAND() LIMIT 1"))[0]
        addon_ids = _random_ids("SELECT id FROM addons WHERE item_type = %s ORDER BY RAND() LIMIT %s",
                                (main["item_type"], ADDON_SLOTS))
    else:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM killers ORDER BY RAND() LIMIT 1")
            main = cur.fetchone()
        addon_ids = _random_ids(
            "SELECT a.id FROM addons a JOIN addon_killers ak ON ak.addon_id = a.id "
            "WHERE ak.killer_id = %s ORDER BY RAND() LIMIT %s", (main["id"], ADDON_SLOTS))
    perk_ids = _random_ids("SELECT id FROM perks WHERE role = %s ORDER BY RAND() LIMIT 4", (role,))
    return {"main": main, "addons": get_addons(addon_ids), "perks": get_perks(perk_ids)}


# ---------- 퀴즈 ----------
def normalize(s):
    """정답 비교용: 소문자로 바꾸고 공백·기호(따옴표, 하이픈, 느낌표 등)를 모두 없앤다."""
    return re.sub(r"[\W_]+", "", (s or "").lower())


def answers_of(perk):
    return {normalize(perk[k]) for k in ("name_ko", "name_en", "former_name_ko", "former_name_en")
            if perk.get(k)} - {""}


_CHOSUNG = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"


def chosung_hint(name):
    """'결정적인 일격' -> 'ㄱㅈㅈㅇ ㅇㄱ' (한글은 초성, 그 외는 그대로)."""
    out = []
    for ch in name:
        code = ord(ch) - 0xAC00
        out.append(_CHOSUNG[code // 588] if 0 <= code < 11172 else ch)
    return "".join(out)
