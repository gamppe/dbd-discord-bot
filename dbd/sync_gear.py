"""DBD 살인마·아이템·에드온 데이터를 가져와 MySQL에 등록/갱신한다.

데이터 출처: https://dbd.tricky.lol/api (items, addons, characters) + deadbydaylight.wiki.gg 이미지
환경변수: DB_PASSWORD (필수), DB_HOST(localhost), DB_USER(root), DB_NAME(dbd)
실행:   python dbd/sync_gear.py   (퍽은 sync_perks.py)
"""
import collections
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pymysql

from sync_perks import API, KEYWORDS_EN, KEYWORDS_KO, WIKI_API, fill_keywords, get_json, render

HERE = Path(__file__).parent


def norm_key(name):
    """파일명 비교용 키: 확장자·T_UI_ 접두사·기호를 없애고 소문자로 만든다."""
    s = name.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
    s = re.sub(r"^t_ui_|^t_", "", s)
    return re.sub(r"[^a-z0-9]", "", s)


def wiki_icon_index():
    index, params = {}, {"action": "query", "list": "allimages", "aiprefix": "Icon",
                         "ailimit": 500, "format": "json"}
    while True:
        data = get_json(WIKI_API, **params)
        for img in data["query"]["allimages"]:
            index.setdefault(norm_key(img["name"]), img["url"].split("?")[0])
        if "continue" not in data:
            break
        params.update(data["continue"])
    return index


def name_based_addon_images(missing, name_counts, icons):
    """게임 아이콘 경로로 못 찾은 에드온의 이미지를 영문 이름으로 위키에서 찾는다.
    1) 영문 이름과 같은 이름의 IconAddon 파일, 2) 같은 제목의 위키 문서 대표 이미지(에드온 아이콘일 때만).
    같은 영문 이름이 둘 이상인 에드온은 엉뚱한 이미지가 붙을 수 있어 건너뛴다.
    missing: [(에드온 ID, 영문 이름)], name_counts: 영문 이름(정규화) -> 에드온 개수, icons: 위키 아이콘 색인."""
    key = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    found, rest = {}, []
    for aid, name in missing:
        if name_counts.get(key(name), 0) != 1:
            continue
        url = icons.get("iconaddon" + key(name))
        if url:
            found[aid] = url
        else:
            rest.append((aid, name))
    for i in range(0, len(rest), 50):
        chunk = rest[i:i + 50]
        data = get_json(WIKI_API, action="query", prop="pageimages", piprop="original", redirects=1,
                        titles="|".join(n for _, n in chunk), format="json", formatversion=2)["query"]
        alias = {m["from"]: m["to"] for m in data.get("normalized", []) + data.get("redirects", [])}
        images = {p["title"]: p["original"]["source"].split("?")[0] for p in data["pages"] if "original" in p}
        for aid, name in chunk:
            title = alias.get(name, name)
            title = alias.get(title, title)
            url = images.get(title)
            # 문서 대표 이미지가 에드온 아이콘일 때만 쓴다 (T_UI_iconAddon_... 형태도 있다)
            if url and re.match(r"(?i)(T_UI_)?iconAddon", url.rsplit("/", 1)[-1]):
                found[aid] = url
    return found


def decommissioned_addon_ids(addons_en):
    """위키의 'Decommissioned Add-ons' 분류와 영문 이름이 같은 에드온 ID 집합.
    API는 폐기된 에드온도 계속 내려주므로(예: 이상한 도장) 위키 기준으로 걸러낸다.
    이름이 둘 이상 겹치는 에드온은 잘못 지울 위험이 있어 건너뛴다. 위키 조회에 실패하면 마지막으로 성공한 결과를 쓴다."""
    cache = HERE / "decommissioned_addons.json"
    try:
        members, params = [], {"action": "query", "list": "categorymembers", "cmnamespace": 0, "cmlimit": 500,
                               "cmtitle": "Category:Decommissioned Add-ons", "format": "json", "formatversion": 2}
        while True:
            data = get_json(WIKI_API, **params)
            members += [m["title"] for m in data["query"]["categorymembers"]]
            if "continue" not in data:
                break
            params.update(data["continue"])
        key = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
        by_name = collections.defaultdict(list)
        for aid, a in addons_en.items():
            by_name[key(a["name"] or "")].append(aid)
        ids = set()
        for title in members:
            found = by_name.get(key(title), [])
            if len(found) == 1:
                ids.add(found[0])
            elif len(found) > 1:
                print(f"경고: 폐기 에드온 '{title}'과 이름이 같은 에드온이 {len(found)}개라 건너뜁니다: {found}")
        cache.write_text(json.dumps(sorted(ids), indent=1), encoding="utf-8")
        return ids
    except Exception as e:
        print(f"경고: 위키에서 폐기 에드온 목록을 가져오지 못했어요({type(e).__name__}). 이전 결과를 씁니다.")
        return set(json.loads(cache.read_text(encoding="utf-8"))) if cache.exists() else set()


def version_of(name):
    v = get_json(f"{API}/versions")[name]
    return v["version"], datetime.fromtimestamp(v["lastupdate"], timezone.utc).replace(tzinfo=None)


def text(item, lang_item):
    """설명 하나를 일반 텍스트로 정리한다 (자리표시자가 있으면 치환)."""
    return fill_keywords(render(item.get("description") or "", item.get("tunables") or {}),
                         KEYWORDS_KO if lang_item == "ko" else KEYWORDS_EN, {})


def main():
    items_en, items_ko = get_json(f"{API}/items"), get_json(f"{API}/items", locale="ko")
    addons_en, addons_ko = get_json(f"{API}/addons"), get_json(f"{API}/addons", locale="ko")
    chars_en, chars_ko = get_json(f"{API}/characters"), get_json(f"{API}/characters", locale="ko")
    print(f"아이템 {len(items_en)} / 에드온 {len(addons_en)} / 캐릭터 {len(chars_en)}")

    icons = wiki_icon_index()
    img_url = lambda path: icons.get(norm_key(path)) if path else None
    overrides_file = HERE / "image_overrides.json"
    overrides = json.loads(overrides_file.read_text(encoding="utf-8")) if overrides_file.exists() else {}

    v_char, t_char = version_of("characters")
    v_item, t_item = version_of("items")
    v_addon, t_addon = version_of("addons")

    killers = [(c["id"], chars_ko.get(k, c)["name"], c["name"], v_char, t_char)
               for k, c in chars_en.items() if c["role"] == "killer"]
    # 살인마 고유 능력(곰덫 등)은 저장하지 않고, 그 능력의 에드온을 살인마에게 바로 연결한다.
    power_owner = {c["item"]: c["id"] for c in chars_en.values() if c["role"] == "killer" and c.get("item")}

    def usable(d):
        """이름이 없거나 내부 코드명/템플릿(@#..., {0})인 항목은 아직 완성되지 않은 데이터라 제외한다."""
        n = d.get("name")
        return bool(n) and not n.startswith("@#") and "{" not in n

    skipped = [i for i, it in items_en.items()
               if it["type"] != "power" and not (usable(it) and usable(items_ko.get(i, it)))]
    items_en = {i: it for i, it in items_en.items() if it["type"] != "power" and i not in skipped}
    if skipped:
        print(f"건너뜀(이름 미완성) 아이템: {skipped}")

    item_rows = []
    for iid, it in items_en.items():
        ko = items_ko.get(iid, it)
        item_rows.append((iid, ko["name"], it["name"], text(ko, "ko"), text(it, "en"), it.get("item_type"),
                          it.get("role"), it.get("rarity"), it.get("event"), it.get("image"),
                          overrides.get(iid) or img_url(it.get("image")), v_item, t_item))

    # API는 에드온의 이벤트 여부를 알려주지 않으므로 event_addons.json({"에드온ID": "이벤트 이름"})으로 직접 지정한다.
    event_file = HERE / "event_addons.json"
    event_addons = json.loads(event_file.read_text(encoding="utf-8")) if event_file.exists() else {}
    unknown = [a for a in event_addons if a not in addons_en]
    if unknown:
        print(f"경고: event_addons.json에 있지만 API에 없는 에드온 ID: {unknown}")

    decommissioned = decommissioned_addon_ids(addons_en)
    if decommissioned:
        print("폐기되어 제외하는 에드온:", ", ".join(f"{addons_ko.get(a, addons_en[a])['name']}({a})"
                                                  for a in sorted(decommissioned)))

    addon_rows, parent_rows = [], []
    for aid, ad in addons_en.items():
        ko = addons_ko.get(aid, ad)
        if aid in decommissioned:  # 현재 게임에 없는 에드온
            continue
        if not (usable(ad) and usable(ko)):  # 이름이 내부 코드명(@#...)인 미완성 에드온은 제외
            print(f"건너뜀(이름 미완성) 에드온: {aid}")
            continue
        addon_rows.append((aid, ko["name"], ad["name"], text(ko, "ko"), text(ad, "en"),
                           ad.get("type"), ad.get("item_type"), ad.get("role"), ad.get("rarity"),
                           ad.get("image"), overrides.get(aid) or img_url(ad.get("image")), v_addon, t_addon,
                           event_addons.get(aid)))
        parent_rows += [(aid, power_owner[p]) for p in ad.get("parents") or [] if p in power_owner]

    # 아이콘 경로로 못 찾은 에드온은 영문 이름으로 위키에서 한 번 더 찾는다.
    name_counts = collections.Counter(re.sub(r"[^a-z0-9]", "", r[2].lower()) for r in addon_rows)
    missing = [(r[0], r[2]) for r in addon_rows if not r[10]]
    try:
        found = name_based_addon_images(missing, name_counts, icons)
    except Exception as e:
        print(f"경고: 이름으로 에드온 이미지를 찾는 데 실패했어요({type(e).__name__}). 이번에는 건너뜁니다.")
        found = {}
    addon_rows = [r[:10] + (found.get(r[0], r[10]),) + r[11:] for r in addon_rows]
    print(f"이름으로 위키에서 찾은 에드온 이미지: {len(found)}개 (이미지가 없던 {len(missing)}개 중)")

    left = sum(("{Tunable." in r[3] or "{Tunable." in r[4] or "{Keyword." in r[3]) for r in item_rows + addon_rows)
    if left:
        print(f"경고: 자리표시자가 남은 설명 {left}개")

    conn = pymysql.connect(host=os.environ.get("DB_HOST", "127.0.0.1"), user=os.environ.get("DB_USER", "root"),
                           password=os.environ["DB_PASSWORD"], charset="utf8mb4", autocommit=False)
    with conn, conn.cursor() as cur:
        db = os.environ.get("DB_NAME", "dbd")
        cur.execute(f"CREATE DATABASE IF NOT EXISTS `{db}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        cur.execute(f"USE `{db}`")
        # 이전 구조(addon_parents)가 남아 있으면 이 스크립트가 채우는 표만 지우고 새 구조로 다시 만든다.
        # (모두 API에서 다시 채워지는 표라 데이터 손실이 없다. 퍽/태그 표는 건드리지 않는다.)
        cur.execute("SHOW TABLES LIKE 'addon_parents'")
        if cur.fetchone():
            cur.execute("SET FOREIGN_KEY_CHECKS = 0")
            for t in ("addon_parents", "addon_killers", "addons", "items", "killers"):
                cur.execute(f"DROP TABLE IF EXISTS `{t}`")
            cur.execute("SET FOREIGN_KEY_CHECKS = 1")
            print("이전 구조의 살인마/아이템/에드온 표를 새 구조로 다시 만듭니다.")
        for stmt in (HERE / "schema.sql").read_text(encoding="utf-8").split(";"):
            if stmt.strip():
                cur.execute(stmt)
        # 이미 만들어진 addons 표에 event 컬럼이 없으면 추가한다.
        cur.execute("SHOW COLUMNS FROM addons LIKE 'event'")
        if not cur.fetchone():
            cur.execute("ALTER TABLE addons ADD COLUMN event VARCHAR(100) NULL AFTER rarity")

        cur.executemany("""INSERT INTO killers (id, name_ko, name_en, game_version, last_updated)
            VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE name_ko=VALUES(name_ko), name_en=VALUES(name_en),
            game_version=VALUES(game_version), last_updated=VALUES(last_updated)""", killers)
        cur.executemany("""INSERT INTO items (id, name_ko, name_en, description_ko, description_en, item_type,
                role, rarity, event, image_path, image_url, game_version, last_updated)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE name_ko=VALUES(name_ko), name_en=VALUES(name_en),
              description_ko=VALUES(description_ko), description_en=VALUES(description_en),
              item_type=VALUES(item_type), role=VALUES(role), rarity=VALUES(rarity), event=VALUES(event),
              image_path=VALUES(image_path), image_url=COALESCE(VALUES(image_url), image_url),
              game_version=VALUES(game_version), last_updated=VALUES(last_updated)""", item_rows)
        cur.executemany("""INSERT INTO addons (id, name_ko, name_en, description_ko, description_en, addon_type,
                item_type, role, rarity, image_path, image_url, game_version, last_updated, event)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE name_ko=VALUES(name_ko), name_en=VALUES(name_en),
              description_ko=VALUES(description_ko), description_en=VALUES(description_en),
              addon_type=VALUES(addon_type), item_type=VALUES(item_type), role=VALUES(role), rarity=VALUES(rarity),
              image_path=VALUES(image_path), image_url=COALESCE(VALUES(image_url), image_url),
              game_version=VALUES(game_version), last_updated=VALUES(last_updated), event=VALUES(event)""",
                        addon_rows)
        cur.execute("DELETE FROM addons WHERE name_en LIKE '@#%%' OR name_ko LIKE '@#%%'")  # 이전에 들어간 미완성 항목 정리
        if decommissioned:  # 이전에 들어간 폐기 에드온 정리
            cur.execute(f"DELETE FROM addons WHERE id IN ({','.join(['%s'] * len(decommissioned))})",
                        sorted(decommissioned))
        # 연결 관계는 매번 원본 기준으로 다시 만든다.
        cur.execute("DELETE FROM addon_killers")
        cur.executemany("INSERT IGNORE INTO addon_killers (addon_id, killer_id) VALUES (%s,%s)", parent_rows)
        conn.commit()

        for label, sql in (("살인마", "SELECT COUNT(*) FROM killers"),
                           ("아이템", "SELECT COUNT(*) FROM items"),
                           ("에드온", "SELECT COUNT(*) FROM addons"),
                           ("살인마에 연결된 에드온", "SELECT COUNT(DISTINCT addon_id) FROM addon_killers"),
                           ("이미지 없는 아이템", "SELECT COUNT(*) FROM items WHERE image_url IS NULL"),
                           ("이미지 없는 에드온", "SELECT COUNT(*) FROM addons WHERE image_url IS NULL")):
            cur.execute(sql)
            print(f"{label}: {cur.fetchone()[0]}")


if __name__ == "__main__":
    sys.exit(main())
