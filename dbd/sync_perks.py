"""DBD 퍽 데이터를 가져와 MySQL에 등록/갱신한다. 끝나면 sync_gear.py(살인마·아이템·에드온)도 이어서 실행한다.

데이터 출처
  - 퍽 이름/설명/소유자(한글·영문): https://dbd.tricky.lol/api (비공식 커뮤니티 API, 게임 데이터 기반)
  - 퍽 이미지 URL: deadbydaylight.wiki.gg (MediaWiki API)

환경변수: DB_PASSWORD (필수), DB_HOST(localhost), DB_USER(root), DB_NAME(dbd)
실행:   python dbd/sync_perks.py
"""
import html
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pymysql
import requests

HERE = Path(__file__).parent
API = "https://dbd.tricky.lol/api"
WIKI_API = "https://deadbydaylight.wiki.gg/api.php"
HEADERS = {"User-Agent": "DBDPerkBot/0.1 (personal discord bot)"}
ROLE_TAG = {"survivor": "생존자", "killer": "살인마"}

session = requests.Session()
session.headers.update(HEADERS)


def get_json(url, **params):
    r = session.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


# {Keyword.X} 자리표시자 -> 상태 효과 이름. API에 목록이 없어서 직접 관리한다.
# 한글 이름은 확인된 것만 채웠다. 비어 있는 항목은 설명에 {Keyword.X}가 그대로 남으니 채워 주세요.
KEYWORDS_EN = {
    "Haste": "Haste", "Exhausted": "Exhausted", "Broken": "Broken", "Exposed": "Exposed",
    "Undetectable": "Undetectable", "Oblivious": "Oblivious", "Endurance": "Endurance",
    "Hindered": "Hindered", "Blindness": "Blindness", "Elusive": "Elusive",
    "Hemorrhage": "Hemorrhage", "Mangled": "Mangled", "DeepWound": "Deep Wound",
}
KEYWORDS_KO = {
    "Haste": "이동 속도 상승",
    "Exhausted": "탈진",
    "Broken": "치료 불능",
    "Exposed": "약점 노출",
    "Undetectable": "감지 불가능",
    "Oblivious": "인지 불가능",
    "Endurance": "인내",
    "Hindered": "근육 손상",
    "Blindness": "실명",
    "Elusive": "신출귀몰",
    "Hemorrhage": "과다 출혈",
    "Mangled": "깊은 상흔",
    "DeepWound": "심각한 부상",
}
INPUTS_EN = {"ActivatableButton1": "[Ability 1]", "ActivatableButton2": "[Ability 2]", "UseItem": "[Use Item]"}
INPUTS_KO = {"ActivatableButton1": "[능력 1]", "ActivatableButton2": "[능력 2]", "UseItem": "[아이템 사용]"}


def fill_keywords(text, keywords, inputs):
    kw = {k.lower(): v for k, v in keywords.items()}
    text = re.sub(r"\{Keyword\.([^}]+)\}", lambda m: kw.get(m.group(1).lower(), m.group(0)), text)
    return re.sub(r"\{Input\.([^}]+)\}", lambda m: inputs.get(m.group(1), m.group(0)), text)


# 인게임에도 수치가 명시되지 않은 자리표시자는 해당 문구만 자연스럽게 바꾼다.
TEXT_FIXES = {
    "BoilOver": [
        (r"최소 \{Tunable\.S13P03\.RequiredFallHeight\}미터에서", "높은 곳에서"),
        (r"at least \{Tunable\.S13P03\.RequiredFallHeight\}m", "a height"),
    ],
}


def fmt_num(v):
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


def render(desc, tunables):
    """{Tunable.S07P03.Key%} 자리표시자를 실제 수치(티어별 a/b/c)로 바꾸고 HTML을 일반 텍스트로 만든다."""
    def sub(m):
        key = m.group(1).split(".")[-1].lower()
        vals = tunables.get(key)
        if not vals:
            return m.group(0)
        uniq = list(dict.fromkeys(vals))
        return "/".join(fmt_num(v) for v in (uniq if len(uniq) == 1 else vals))

    text = re.sub(r"\{Tunable\.([^}]+)\}", sub, desc)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<li[^>]*>", "- ", text)
    text = re.sub(r"</li>|</?ul[^>]*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def fetch_wiki_image_urls(perks_en):
    """퍽 ID -> 위키 이미지 URL.
    1) 게임 아이콘 파일명과 같은(대소문자 무시) 위키 파일을 찾고,
    2) 없으면 같은 영문 이름의 위키 퍽 문서 대표 이미지를 쓴다."""
    files, params = {}, {"action": "query", "list": "allimages", "aiprefix": "IconPerks",
                         "ailimit": 500, "format": "json"}
    while True:
        data = get_json(WIKI_API, **params)
        for img in data["query"]["allimages"]:
            files[img["name"].lower().replace(" ", "_")] = img["url"].split("?")[0]
        if "continue" not in data:
            break
        params.update(data["continue"])

    # 게임 아이콘 이름에 T_UI_ 접두사가 붙거나 기호·대소문자가 달라도 맞출 수 있게, 정규화한 이름으로도 찾는다.
    norm = lambda s: re.sub(r"[^a-z0-9]", "", re.sub(r"^t_ui_|^t_", "", s.rsplit(".", 1)[0].lower()))
    by_norm = {}
    for name, url in files.items():
        by_norm.setdefault(norm(name), url)

    out = {}
    for pid, p in perks_en.items():
        base = (p.get("image") or "").rsplit("/", 1)[-1]
        if f"{base}.png".lower() in files:
            out[pid] = files[f"{base}.png".lower()]
        elif base and norm(base) in by_norm:
            out[pid] = by_norm[norm(base)]

    todo = [(pid, p["name"]) for pid, p in perks_en.items() if pid not in out]
    for i in range(0, len(todo), 50):
        chunk = todo[i:i + 50]
        data = get_json(WIKI_API, action="query", prop="pageimages", piprop="original", redirects=1,
                        titles="|".join(n for _, n in chunk), format="json", formatversion=2)["query"]
        title_of = {n: n for _, n in chunk}
        for m in data.get("normalized", []) + data.get("redirects", []):
            for k, v in list(title_of.items()):
                if v == m["from"]:
                    title_of[k] = m["to"]
        images = {p["title"]: p["original"]["source"].split("?")[0]
                  for p in data["pages"] if "original" in p}
        for pid, name in chunk:
            if title_of[name] in images:
                out[pid] = images[title_of[name]]
    return out


def main():
    versions = get_json(f"{API}/versions")["perks"]
    version = versions["version"]
    updated = datetime.fromtimestamp(versions["lastupdate"], timezone.utc).replace(tzinfo=None)

    perks_en = get_json(f"{API}/perks")
    perks_ko = get_json(f"{API}/perks", locale="ko")
    chars_en = get_json(f"{API}/characters")
    chars_ko = get_json(f"{API}/characters", locale="ko")
    print(f"게임 버전 {version} / 퍽 {len(perks_en)}개 (ko {len(perks_ko)}개)")

    wiki_urls = fetch_wiki_image_urls(perks_en)
    # 자동 매칭에 실패한 퍽(특수 따옴표 등)은 image_overrides.json의 URL을 쓴다.
    img_file = HERE / "image_overrides.json"
    if img_file.exists():
        for pid, url in json.loads(img_file.read_text(encoding="utf-8")).items():
            wiki_urls.setdefault(pid, url)

    renames_file = HERE / "perk_renames.json"
    renames = json.loads(renames_file.read_text(encoding="utf-8")) if renames_file.exists() else {}

    # API 원본에 없는 수치를 수동으로 보정한다 (위키 등에서 확인한 값). API에 값이 있으면 API가 우선.
    overrides_file = HERE / "tunable_overrides.json"
    overrides = json.loads(overrides_file.read_text(encoding="utf-8")) if overrides_file.exists() else {}

    rows, unresolved, left_ko = [], 0, 0
    for pid, en in perks_en.items():
        ko = perks_ko.get(pid, en)
        extra = overrides.get(pid, {})
        en = {**en, "tunables": {**extra, **en.get("tunables", {})}}
        for pat, repl in TEXT_FIXES.get(pid, []):
            en["description"] = re.sub(pat, repl, en["description"])
            ko = {**ko, "description": re.sub(pat, repl, ko["description"])}
        ko = {**ko, "tunables": {**extra, **ko.get("tunables", en["tunables"])}}
        cid = en.get("character")
        owner_en = chars_en[str(cid)]["name"] if cid is not None and str(cid) in chars_en else None
        owner_ko = chars_ko[str(cid)]["name"] if cid is not None and str(cid) in chars_ko else owner_en
        d_en = fill_keywords(render(en["description"], en["tunables"]), KEYWORDS_EN, INPUTS_EN)
        d_ko = fill_keywords(render(ko["description"], ko["tunables"]), KEYWORDS_KO, INPUTS_KO)
        unresolved += ("{Tunable." in d_en) + ("{Tunable." in d_ko)
        left_ko += len(re.findall(r"\{(?:Keyword|Input)\.", d_ko))
        rn = renames.get(pid, {})
        rows.append((
            pid, ko["name"], en["name"], d_ko, d_en, rn.get("ko"), rn.get("en"), en["role"],
            owner_ko, owner_en, en.get("image"), wiki_urls.get(pid),
            json.dumps(en.get("categories", [])), version, updated,
        ))
    if unresolved:
        print(f"경고: 수치 자리표시자를 못 채운 설명 {unresolved}개 (tunable_overrides.json에 추가하면 해결)")
    if left_ko:
        print(f"안내: 한글 설명에 {{Keyword.*}}/{{Input.*}} 자리표시자 {left_ko}개 남음 (KEYWORDS_KO를 채우면 해결)")

    db = os.environ.get("DB_NAME", "dbd")
    conn = pymysql.connect(host=os.environ.get("DB_HOST", "127.0.0.1"),
                           user=os.environ.get("DB_USER", "root"),
                           password=os.environ["DB_PASSWORD"], charset="utf8mb4", autocommit=False)
    with conn, conn.cursor() as cur:
        cur.execute(f"CREATE DATABASE IF NOT EXISTS `{db}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        cur.execute(f"USE `{db}`")
        for stmt in (HERE / "schema.sql").read_text(encoding="utf-8").split(";"):
            if stmt.strip():
                cur.execute(stmt)

        # 예전 이름·태그는 수동 관리 값이므로 갱신 때 덮어쓰지 않는다 (renames 파일에 값이 있을 때만 갱신).
        cur.executemany("""
            INSERT INTO perks (id, name_ko, name_en, description_ko, description_en, former_name_ko,
                               former_name_en, role, owner_name_ko, owner_name_en, image_path, image_url,
                               source_categories, game_version, last_updated)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
              name_ko=VALUES(name_ko), name_en=VALUES(name_en),
              description_ko=VALUES(description_ko), description_en=VALUES(description_en),
              former_name_ko=COALESCE(VALUES(former_name_ko), former_name_ko),
              former_name_en=COALESCE(VALUES(former_name_en), former_name_en),
              role=VALUES(role), owner_name_ko=VALUES(owner_name_ko), owner_name_en=VALUES(owner_name_en),
              image_path=VALUES(image_path), image_url=COALESCE(VALUES(image_url), image_url),
              source_categories=VALUES(source_categories), game_version=VALUES(game_version),
              last_updated=VALUES(last_updated)
        """, rows)

        cur.executemany("INSERT IGNORE INTO tags (name) VALUES (%s)", [(t,) for t in ROLE_TAG.values()])
        for role, tag in ROLE_TAG.items():
            cur.execute("""INSERT IGNORE INTO perk_tags (perk_id, tag_id)
                           SELECT p.id, t.id FROM perks p JOIN tags t ON t.name=%s WHERE p.role=%s""", (tag, role))
        conn.commit()

        cur.execute("SELECT role, COUNT(*), SUM(image_url IS NULL), SUM(owner_name_en IS NULL) FROM perks GROUP BY role")
        for role, n, no_img, no_owner in cur.fetchall():
            print(f"{role}: {n}개 (이미지 없음 {no_img}, 공용 퍽 {no_owner})")

    # 살인마·아이템·에드온도 함께 갱신한다 (sync_gear.py가 이 파일을 불러 쓰므로 여기서 늦게 import).
    print("\n--- 살인마 / 아이템 / 에드온 ---")
    import sync_gear
    sync_gear.main()


if __name__ == "__main__":
    sys.exit(main())
