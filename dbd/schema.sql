CREATE TABLE IF NOT EXISTS perks (
    id               VARCHAR(100)  NOT NULL,            -- dbd.tricky.lol 퍽 ID (예: Ace_In_The_Hole)
    name_ko          VARCHAR(200)  NOT NULL,
    name_en          VARCHAR(200)  NOT NULL,
    description_ko   TEXT          NOT NULL,
    description_en   TEXT          NOT NULL,
    former_name_ko   VARCHAR(200)  NULL,                -- 예전 한글 이름 (없으면 NULL)
    former_name_en   VARCHAR(200)  NULL,                -- 예전 영문 이름 (없으면 NULL)
    role             ENUM('survivor','killer') NOT NULL,
    owner_name_ko    VARCHAR(200)  NULL,                -- 퍽 소유 캐릭터 (공용 퍽이면 NULL)
    owner_name_en    VARCHAR(200)  NULL,
    image_path       VARCHAR(300)  NULL,                -- 게임 내부 아이콘 경로
    image_url        VARCHAR(500)  NULL,                -- 위키 이미지 URL
    source_categories JSON         NULL,                -- API가 주는 원본 분류 (참고용)
    game_version     VARCHAR(20)   NULL,
    last_updated     DATETIME      NULL,                -- 원본 데이터의 최종 업데이트 시각 (UTC)
    PRIMARY KEY (id),
    KEY idx_name_ko (name_ko),
    KEY idx_name_en (name_en),
    KEY idx_role (role)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS killers (
    id             VARCHAR(50)  NOT NULL,               -- 예: Chuckles
    name_ko        VARCHAR(200) NOT NULL,
    name_en        VARCHAR(200) NOT NULL,
    game_version   VARCHAR(20)  NULL,
    last_updated   DATETIME     NULL,
    PRIMARY KEY (id),
    KEY idx_name_ko (name_ko),
    KEY idx_name_en (name_en)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS items (
    id             VARCHAR(100) NOT NULL,
    name_ko        VARCHAR(200) NOT NULL,
    name_en        VARCHAR(200) NOT NULL,
    description_ko TEXT         NOT NULL,
    description_en TEXT         NOT NULL,
    item_type      VARCHAR(50)  NULL,                   -- toolbox, medkit, flashlight ... (생존자 아이템만 저장)
    role           ENUM('survivor','killer') NULL,
    rarity         VARCHAR(20)  NULL,
    event          VARCHAR(100) NULL,
    image_path     VARCHAR(300) NULL,
    image_url      VARCHAR(500) NULL,
    game_version   VARCHAR(20)  NULL,
    last_updated   DATETIME     NULL,
    PRIMARY KEY (id),
    KEY idx_name_ko (name_ko),
    KEY idx_name_en (name_en)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS addons (
    id             VARCHAR(100) NOT NULL,
    name_ko        VARCHAR(200) NOT NULL,
    name_en        VARCHAR(200) NOT NULL,
    description_ko TEXT         NOT NULL,
    description_en TEXT         NOT NULL,
    addon_type     VARCHAR(30)  NULL,                   -- poweraddon(살인마) / itemaddon(생존자 아이템)
    item_type      VARCHAR(50)  NULL,                   -- 생존자 아이템 애드온이 붙는 아이템 종류
    role           ENUM('survivor','killer') NULL,
    rarity         VARCHAR(20)  NULL,
    event          VARCHAR(100) NULL,                   -- 이벤트 에드온이면 이벤트 이름 (event_addons.json으로 지정)
    image_path     VARCHAR(300) NULL,
    image_url      VARCHAR(500) NULL,
    game_version   VARCHAR(20)  NULL,
    last_updated   DATETIME     NULL,
    PRIMARY KEY (id),
    KEY idx_name_ko (name_ko),
    KEY idx_name_en (name_en)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS addon_killers (          -- 살인마 에드온이 어느 살인마 것인지
    addon_id  VARCHAR(100) NOT NULL,
    killer_id VARCHAR(50)  NOT NULL,
    PRIMARY KEY (addon_id, killer_id),
    FOREIGN KEY (addon_id)  REFERENCES addons(id)  ON DELETE CASCADE,
    FOREIGN KEY (killer_id) REFERENCES killers(id) ON DELETE CASCADE
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS bot_state (                -- 봇이 기억해 둘 값 (예: 마지막으로 올린 신전 번호)
    state_key   VARCHAR(50)  NOT NULL PRIMARY KEY,
    state_value VARCHAR(200) NOT NULL
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS tags (
    id   INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(50) NOT NULL UNIQUE
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS perk_tags (
    perk_id VARCHAR(100) NOT NULL,
    tag_id  INT          NOT NULL,
    PRIMARY KEY (perk_id, tag_id),
    FOREIGN KEY (perk_id) REFERENCES perks(id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id)  REFERENCES tags(id)  ON DELETE CASCADE
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
