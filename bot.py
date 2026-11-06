"""DBD 퍽 디스코드 봇.

명령어
  #랜덤퍽 채널(RANDOM_CHANNEL_ID)의 버튼으로 무작위 빌드 (생존자: 아이템+에드온+퍽 4개 / 살인마: 살인마+에드온+퍽 4개)
  /퀴즈        나만 볼 수 있는 임시 채널에서 퍽 아이콘을 보고 이름 맞히기 (3분간 입력이 없으면 채널이 사라짐)

자동 기능: 새 신전이 시작되면 #주간신전 채널에 자동으로 올리고 지난주 메시지는 지운다 (10분마다 확인).
          #검색 채널(SEARCH_CHANNEL_ID)에 채팅을 치면 퍽·아이템·에드온을 자동으로 검색해서 답한다.

환경변수: DISCORD_TOKEN (필수), DB_PASSWORD (필수), DB_HOST/DB_USER/DB_NAME,
          GUILD_ID (선택: 지정하면 해당 서버에 명령어가 즉시 등록됨),
          SHRINE_CHANNEL_ID (선택: 알림 채널 ID, 기본값은 #주간신전),
          SEARCH_CHANNEL_ID (선택: #검색 채널 ID, 기본값은 #검색. 이 기능은 Message Content Intent가 필요)
실행:   python bot.py
"""
import asyncio
import os
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

from dbd import queries

SHRINE_URL = "https://dbd.tricky.lol/api/shrine"
SHRINE_CACHE_SECONDS = 300
SHRINE_CHANNEL_ID = int(os.environ.get("SHRINE_CHANNEL_ID") or 1568021283336101908)  # #주간신전
SHRINE_CHECK_MINUTES = 10
# #검색 채널 ID. 설정하면 그 채널의 채팅이 자동으로 검색된다 (일반 메시지를 읽는 'Message Content Intent' 필요).
SEARCH_CHANNEL_ID = int(os.environ.get("SEARCH_CHANNEL_ID") or 1568022737505751191)  # #검색
# #랜덤퍽 채널 ID. 이 채널에 버튼 안내 메시지를 올린다 (환경변수 RANDOM_CHANNEL_ID=0이면 꺼짐).
RANDOM_CHANNEL_ID = int(os.environ.get("RANDOM_CHANNEL_ID") or 1568164190005370900)  # #랜덤퍽
RANDOM_RESULT_PUBLIC = True  # True: 채널 모두에게 결과가 보인다 / False: 버튼을 누른 사람에게만 보인다
HINT_AT = 20  # 퀴즈 문제를 낸 뒤 이 시간(초)이 지나면 초성 힌트를 보여준다
ROLE_LABEL = {"survivor": "생존자", "killer": "살인마"}
ROLE_COLOR = {"survivor": 0x3FA7D6, "killer": 0xC0392B}
HEADERS = {"User-Agent": "DBDPerkBot/0.1 (personal discord bot)"}


def perk_embed(p):
    embed = discord.Embed(title=f"{p['name_ko']} ({p['name_en']})",
                          description=p["description_ko"][:4000],
                          color=ROLE_COLOR[p["role"]])
    owner = p["owner_name_ko"] or "공용 퍽"
    embed.set_footer(text=f"{ROLE_LABEL[p['role']]} · {owner}")
    if p["image_url"]:
        embed.set_thumbnail(url=p["image_url"])
    if p["former_name_ko"] or p["former_name_en"]:
        embed.add_field(name="예전 이름", value=" / ".join(filter(None, [p["former_name_ko"], p["former_name_en"]])))
    return embed


class Bot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        # #검색 채널과 퀴즈 채널에서 채팅 내용을 읽으므로 필요하다 (개발자 포털에서 켜 둬야 한다).
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.http_session = None
        self.shrine_cache = (0.0, None)
        self.quiz_by_channel = {}  # 퀴즈 채널 ID -> QuizSession
        self.quiz_by_user = {}     # 사용자 ID -> QuizSession (한 사람이 퀴즈 채널을 하나만 갖도록)

    async def setup_hook(self):
        self.http_session = aiohttp.ClientSession(headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15))
        guild_id = os.environ.get("GUILD_ID")
        if guild_id:
            guild = discord.Object(int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.add_view(RandomView())  # 봇을 다시 켜도 #랜덤퍽의 기존 버튼이 계속 동작하게 등록한다
        weekly_shrine.start()

    async def close(self):
        weekly_shrine.cancel()
        if self.http_session:
            await self.http_session.close()
        await super().close()

    async def get_shrine(self):
        fetched_at, data = self.shrine_cache
        # 캐시가 유효하고 아직 종료 시각 전이면 그대로 쓴다.
        if data and time.time() - fetched_at < SHRINE_CACHE_SECONDS and time.time() < data["end"]:
            return data
        try:
            async with self.http_session.get(SHRINE_URL) as r:
                r.raise_for_status()
                data = await r.json()
            self.shrine_cache = (time.time(), data)
            return data
        except Exception:
            if data:  # 조회에 실패하면 오래된 캐시라도 보여준다
                return data
            raise


bot = Bot()


# ---------- /신전 ----------
async def build_shrine_embeds(data):
    perks = {p["id"]: p for p in await asyncio.to_thread(queries.get_perks, [x["id"] for x in data["perks"]])}
    embeds = []
    for item in data["perks"]:
        p = perks.get(item["id"])
        if p is None:
            embeds.append(discord.Embed(title=item["id"], description="DB에 없는 퍽이에요. 동기화를 다시 실행해 주세요."))
            continue
        e = perk_embed(p)
        e.description = p["description_ko"][:600]
        embeds.append(e)
    embeds[0].set_author(name=f"비밀의 신전 #{data['id']}")
    embeds[-1].add_field(name="갱신", value=f"<t:{data['end'] + 1}:R> (<t:{data['end'] + 1}:f>)", inline=False)
    return embeds


# ---------- 주간 신전 알림 ----------
SHRINE_STATE_KEY = "last_posted_shrine_id"
SHRINE_MESSAGE_KEY = "last_posted_shrine_message_id"


async def post_new_shrine():
    """새 신전이 시작됐으면 알림 채널에 새 메시지를 올리고 이전 주 메시지는 지운다. 올렸으면 True."""
    data = await bot.get_shrine()
    if await asyncio.to_thread(queries.get_state, SHRINE_STATE_KEY) == str(data["id"]):
        return False  # 이미 올린 신전
    channel = bot.get_channel(SHRINE_CHANNEL_ID) or await bot.fetch_channel(SHRINE_CHANNEL_ID)
    old_message_id = await asyncio.to_thread(queries.get_state, SHRINE_MESSAGE_KEY)

    # 새 메시지를 먼저 올리고(실패하면 이전 메시지가 그대로 남는다), 성공하면 번호를 기록한 뒤 이전 메시지를 지운다.
    message = await channel.send(embeds=await build_shrine_embeds(data), silent=True)  # 푸시/소리 알림 없이 조용히
    await asyncio.to_thread(queries.set_state, SHRINE_STATE_KEY, data["id"])
    await asyncio.to_thread(queries.set_state, SHRINE_MESSAGE_KEY, message.id)
    print(f"신전 #{data['id']}을(를) 채널 {SHRINE_CHANNEL_ID}에 올렸어요.")

    if old_message_id:
        try:
            await channel.get_partial_message(int(old_message_id)).delete()
        except (discord.NotFound, discord.Forbidden):
            pass  # 이미 지워졌거나 지울 수 없으면 그냥 넘어간다
    return True


@tasks.loop(minutes=SHRINE_CHECK_MINUTES)
async def weekly_shrine():
    try:
        await post_new_shrine()
    except discord.NotFound:
        print(f"신전 알림 실패: 채널 {SHRINE_CHANNEL_ID}을(를) 찾을 수 없어요. 채널 ID가 맞는지, 봇이 그 서버에 "
              "초대돼 있는지 확인하세요. (시작할 때 출력되는 서버 목록과 비교해 보세요)")
    except discord.Forbidden:
        print("신전 알림 실패: 채널을 볼 수 없거나 메시지 보내기/임베드 링크 권한이 없어요.")
    except Exception as e:  # 루프가 멈추지 않도록 다음 확인 때 다시 시도한다
        print(f"신전 알림 실패: {type(e).__name__}: {e}")


@weekly_shrine.before_loop
async def _wait_ready():
    await bot.wait_until_ready()


# ---------- /검색 ----------
RARITY_COLOR = {"common": 0x8B6B4A, "uncommon": 0x2ECC71, "rare": 0x3498DB, "veryrare": 0x9B59B6,
                "ultrarare": 0xE91E8C, "visceral": 0xC0392B}


def item_sub(i):
    return f"생존자 아이템 · {queries.ITEM_TYPE_KO.get(i['item_type'], '기타')}"


EVENT_COLOR = 0xF39C12  # 이벤트 아이템/에드온은 희귀도 대신 이 색으로 표시한다


def gear_embed(g, sub):
    """아이템/에드온 공통 임베드. 이벤트 항목은 '이벤트' 등급(주황색)으로 표시한다."""
    event = g.get("event")
    embed = discord.Embed(title=f"{g['name_ko']} ({g['name_en']})",
                          description=(g["description_ko"] or "(설명 없음)")[:4000],
                          color=EVENT_COLOR if event else RARITY_COLOR.get(g["rarity"], 0x95A5A6))
    embed.set_footer(text=f"{sub} · 이벤트 ({event})" if event else sub)
    if g["image_url"]:
        embed.set_thumbnail(url=g["image_url"])
    return embed


# 종류별 (표시 이름, 검색 함수, 임베드 만들기, 한 줄 설명)
KINDS = {
    "perk": ("퍽", queries.search_perks, perk_embed,
             lambda p: f"{ROLE_LABEL[p['role']]} · {p['owner_name_ko'] or '공용'}"),
    "item": ("아이템", queries.search_items, lambda i: gear_embed(i, item_sub(i)), item_sub),
    "addon": ("에드온", queries.search_addons, lambda a: gear_embed(a, queries.addon_owner_text(a)),
              queries.addon_owner_text),
}


def combine_results(by_kind, cap=queries.SEARCH_LIMIT):
    """종류별 검색 결과를 번갈아 하나씩 뽑아 cap개까지 모은 뒤, 종류별로 묶어서 돌려준다.
    (한 종류가 결과를 독점하지 않도록 하면서 각 종류 안의 순위는 그대로 지킨다.)"""
    picked = {k: [] for k in KINDS}
    pools = {k: list(v) for k, v in by_kind.items()}
    total = 0
    while total < cap and any(pools.values()):
        for k in KINDS:
            if pools[k] and total < cap:
                picked[k].append(pools[k].pop(0))
                total += 1
    return [(k, r) for k in KINDS for r in picked[k]]


class SearchView(discord.ui.View):
    """#검색 채널용 선택창. 정해진 시간 안에 고르지 않으면 선택창 메시지를 지운다."""
    message = None
    done = False  # 항목을 골랐으면 True

    async def expire_after(self, seconds):
        # View 기본 timeout은 다른 사람이 눌러도 시간이 다시 시작되므로, 정확히 지키려고 직접 기다린다.
        await asyncio.sleep(seconds)
        if self.done or not self.message:
            return
        self.stop()
        try:
            await self.message.delete()
        except discord.HTTPException:  # 이미 다른 검색으로 지워졌으면 그냥 넘어간다
            pass


class ResultSelect(discord.ui.Select):
    """ephemeral: /검색(본인에게만 보임)용. in_place: #검색 채널용으로, 고르면 선택창을 그 자리에서 답변으로 바꾼다."""

    def __init__(self, entries, ephemeral, in_place=False, header=None, owner_id=None):
        self.ephemeral, self.in_place, self.header, self.owner_id = ephemeral, in_place, header, owner_id
        self.entries = {f"{k}:{r['id']}": (k, r) for k, r in entries}
        super().__init__(placeholder="자세히 볼 항목을 선택하세요", options=[
            discord.SelectOption(label=f"{r['name_ko']} ({r['name_en']})"[:100], value=f"{k}:{r['id']}",
                                 description=f"{KINDS[k][0]} · {KINDS[k][3](r)}"[:100]) for k, r in entries])

    async def callback(self, interaction: discord.Interaction):
        if self.owner_id and interaction.user.id != self.owner_id:
            await interaction.response.send_message("검색한 사람만 고를 수 있어요. 직접 검색해 보세요!", ephemeral=True)
            return
        kind, row = self.entries[self.values[0]]
        embed = KINDS[kind][2](row)
        if self.in_place:
            self.view.done = True  # 고르면 40초 후 삭제를 취소한다
            self.view.stop()
            await interaction.response.edit_message(content=self.header, embed=embed, view=None)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=self.ephemeral)


async def build_search_reply(term, ephemeral, in_place=False, header=None, owner_id=None):
    """검색어로 퍽·아이템·에드온을 찾아 보낼 내용(content/embed/view)을 만든다. /검색과 #검색 채널이 같이 쓴다.
    header: #검색 채널에서 누가 무엇을 검색했는지 알려주는 첫 줄 (원래 글은 지워지기 때문)."""
    found = await asyncio.gather(*(asyncio.to_thread(v[1], term) for v in KINDS.values()))
    entries = combine_results(dict(zip(KINDS, found)))
    head = f"{header}\n" if header else ""
    if not entries:
        return {"content": f"{head}'{term}'에 해당하는 퍽·아이템·에드온이 없어요."}
    if len(entries) == 1:
        kind, row = entries[0]
        reply = {"embed": KINDS[kind][2](row)}
        if header:
            reply["content"] = header
        return reply
    lines, n = [], 0
    for kind in KINDS:
        rows = [r for k, r in entries if k == kind]
        if rows:
            lines.append(f"**{KINDS[kind][0]}**")
            for r in rows:
                n += 1
                lines.append(f"{n}. {r['name_ko']} ({r['name_en']}) - {KINDS[kind][3](r)}")
    more = " (일부만 표시)" if len(entries) >= queries.SEARCH_LIMIT else ""
    view = SearchView(timeout=None) if in_place else discord.ui.View(timeout=120)
    view.add_item(ResultSelect(entries, ephemeral, in_place, header, owner_id))
    prefix = head or f"'{term}' "
    return {"content": prefix + f"검색 결과 {len(entries)}개{more}\n" + "\n".join(lines)[:1800], "view": view}


# ---------- #검색 채널: 채팅을 치면 자동으로 검색 (채널에는 항상 봇 메시지 하나만 남긴다) ----------
SEARCH_SELECT_SECONDS = 40  # 선택창을 이 시간 안에 고르지 않으면 지운다
background_tasks = set()
search_lock = asyncio.Lock()  # 여러 명이 동시에 검색해도 메시지가 겹치거나 둘 이상 남지 않게 한 번에 하나씩 처리


async def clear_bot_messages(channel):
    """채널의 이전 봇 메시지(이전 검색 결과, 재시작 전에 남은 것 포함)를 모두 지운다.
    정리에 실패해도 답변은 올려야 하므로 권한 문제는 경고만 하고 넘어간다."""
    try:
        async for m in channel.history(limit=50):
            if m.author.id == bot.user.id:
                try:
                    await m.delete()
                except discord.NotFound:
                    pass
    except discord.Forbidden:
        print("검색 채널: 이전 봇 메시지를 지우지 못했어요. 봇에 '메시지 기록 보기' 권한이 필요해요.")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    session = bot.quiz_by_channel.get(message.channel.id)
    if session:  # 퀴즈 채널: 참가자의 글만 정답/명령으로 처리한다
        if message.author.id == session.user.id:
            await session.handle(message)
        return
    if not SEARCH_CHANNEL_ID or message.channel.id != SEARCH_CHANNEL_ID:
        return
    term = message.content.strip()[:100]
    if not term:
        if not message.attachments and not message.embeds and not message.stickers:
            print("검색 채널: 메시지 내용이 비어 있어요. 개발자 포털의 'Message Content Intent'가 켜져 있는지 확인하세요.")
        return
    if term.startswith("/"):
        return
    print(f"검색 채널: {message.author.display_name} → '{term}'")
    channel = message.channel
    header = (f"🔎 **{discord.utils.escape_markdown(message.author.display_name)}** · "
              f"{discord.utils.escape_markdown(term)}")
    async with search_lock:
        try:
            try:
                await message.delete()  # 사용자가 쓴 글은 지운다 ('메시지 관리' 권한 필요)
            except discord.NotFound:
                pass
            except discord.Forbidden:
                print("검색 채널: 사용자 글을 지우려면 봇에 '메시지 관리' 권한이 필요해요.")
            async with channel.typing():
                reply = await build_search_reply(term, ephemeral=False, in_place=True, header=header,
                                                 owner_id=message.author.id)
            await clear_bot_messages(channel)
            # silent=True: 푸시/소리 알림 없이 조용히, allowed_mentions: 검색어에 @멘션이 있어도 아무도 호출하지 않는다.
            sent = await channel.send(**reply, silent=True, allowed_mentions=discord.AllowedMentions.none())
            if "view" in reply:
                reply["view"].message = sent
                task = asyncio.create_task(reply["view"].expire_after(SEARCH_SELECT_SECONDS))
                background_tasks.add(task)  # 작업이 끝나기 전에 사라지지 않게 붙들어 둔다
                task.add_done_callback(background_tasks.discard)
        except discord.Forbidden as e:
            print(f"검색 채널 답장 실패(권한 부족): {e}. 봇에 채널 보기/메시지 보내기/링크 임베드 권한이 있는지 확인하세요.")
        except Exception as e:
            print(f"검색 채널 처리 실패: {type(e).__name__}: {e}")


# ---------- #랜덤퍽 채널: 버튼을 누르면 랜덤 빌드 ----------
def shorten(embed, limit=250):
    """한 메시지의 임베드 글자 수 제한(6000자) 안에 7개가 들어가도록 설명을 줄인다."""
    if embed.description and len(embed.description) > limit:
        embed.description = embed.description[:limit].rstrip() + "…"
    return embed


async def build_random_embeds(role):
    """생존자: 무작위 아이템 + 그 에드온 2개 + 퍽 4개 / 살인마: 무작위 살인마 + 그 에드온 2개 + 퍽 4개."""
    build = await asyncio.to_thread(queries.random_build, role)
    main = build["main"]
    if role == "survivor":
        head = gear_embed(main, item_sub(main))
    else:
        head = discord.Embed(title=f"{main['name_ko']} ({main['name_en']})", color=ROLE_COLOR["killer"])
        head.set_footer(text="살인마")
    head.set_author(name=f"🎲 랜덤 {ROLE_LABEL[role]} 빌드")
    embeds = [shorten(head)]
    embeds += [shorten(gear_embed(a, queries.addon_owner_text(a))) for a in build["addons"]]
    embeds += [shorten(perk_embed(p)) for p in build["perks"]]
    embeds[-1].add_field(name="​", value="전체 설명은 #검색 채널에서 확인하세요.", inline=False)
    return embeds


RANDOM_BUTTON_IDS = {"random:survivor", "random:killer"}
random_lock = asyncio.Lock()  # 여러 명이 동시에 눌러도 버튼 메시지가 하나만 남도록 한 번에 하나씩 처리
random_panel_id = None        # 지금 버튼이 달려 있는 메시지의 ID


def has_random_buttons(message):
    """이 메시지가 #랜덤퍽의 버튼 메시지인지 (안내 문구든 결과든 상관없이 버튼으로 판단한다)."""
    return any(getattr(c, "custom_id", None) in RANDOM_BUTTON_IDS
               for row in message.components for c in getattr(row, "children", []))


async def send_random(interaction: discord.Interaction, role):
    global random_panel_id
    if not RANDOM_RESULT_PUBLIC:  # 비공개: 버튼을 누른 사람에게만 임시 메시지로 보여준다
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(embeds=await build_random_embeds(role), silent=True, ephemeral=True)
        return

    # 공개: 채널에는 '결과 + 버튼'이 달린 메시지 하나만 둔다. 새로 뽑으면 이전 결과는 사라진다.
    await interaction.response.defer()  # 버튼 누름만 확인하고, 아래에서 메시지를 직접 바꾼다
    embeds = await build_random_embeds(role)
    content = f"🎲 **{discord.utils.escape_markdown(interaction.user.display_name)}** 님이 뽑았어요"
    no_mentions = discord.AllowedMentions.none()  # 이름만 적어서 아무도 호출하지 않는다
    channel = interaction.channel
    async with random_lock:
        current_id = random_panel_id or interaction.message.id
        target = channel.get_partial_message(current_id)
        try:
            if getattr(channel, "last_message_id", None) == current_id:
                # 버튼 메시지가 채널 맨 아래면 그 자리에서 내용만 새 결과로 바꾼다.
                await target.edit(content=content, embeds=embeds, view=RandomView(), allowed_mentions=no_mentions)
                random_panel_id = current_id
                return
        except discord.NotFound:
            pass  # 이미 지워졌으면 아래에서 새로 올린다
        # 다른 글이 아래에 쌓였으면, 새 결과를 맨 아래에 올리고 이전 메시지는 지운다 (버튼이 위로 밀려 올라가지 않는다).
        try:
            sent = await channel.send(content=content, embeds=embeds, view=RandomView(), silent=True,
                                      allowed_mentions=no_mentions)
        except discord.Forbidden:
            # 새 메시지를 올릴 권한이 없어도 버튼이 멈추지 않게, 있는 메시지를 그 자리에서 고친다 (고치는 데는 권한이 필요 없다).
            print("랜덤퍽: 새 메시지를 올릴 권한이 없어 기존 메시지를 그 자리에서 바꿨어요. "
                  "봇에 이 채널의 '메시지 보내기' 권한을 허용하면 버튼이 항상 맨 아래로 올라와요.")
            try:
                await target.edit(content=content, embeds=embeds, view=RandomView(), allowed_mentions=no_mentions)
                random_panel_id = current_id
            except discord.HTTPException:
                pass
            return
        random_panel_id = sent.id
        try:
            await target.delete()
        except discord.HTTPException:
            pass


class RandomView(discord.ui.View):
    """#랜덤퍽 메시지의 버튼. custom_id가 고정이라 봇을 다시 켜도 같은 메시지의 버튼이 계속 동작한다."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="생존자 랜덤", emoji="🔦", style=discord.ButtonStyle.primary, custom_id="random:survivor")
    async def survivor(self, interaction: discord.Interaction, button: discord.ui.Button):
        await send_random(interaction, "survivor")

    @discord.ui.button(label="살인마 랜덤", emoji="🔪", style=discord.ButtonStyle.danger, custom_id="random:killer")
    async def killer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await send_random(interaction, "killer")


RANDOM_PANEL_TITLE = "🎲 랜덤 퍽 뽑기"


def random_panel_embed():
    return discord.Embed(title=RANDOM_PANEL_TITLE, color=0x95A5A6, description=(
        "버튼을 누르면 무작위 빌드를 뽑아줘요. 새로 뽑으면 이전 결과는 사라져요.\n"
        "**🔦 생존자** · 아이템 1개 + 그 아이템의 에드온 2개 + 퍽 4개\n"
        "**🔪 살인마** · 살인마 1명 + 그 살인마의 에드온 2개 + 퍽 4개\n"
        + ("" if RANDOM_RESULT_PUBLIC else "결과는 버튼을 누른 사람에게만 보여요.")))


async def ensure_random_panel():
    """#랜덤퍽 채널에 봇 메시지가 '버튼 메시지' 하나만 있도록 맞춘다. (봇이 켜질 때 한 번 실행)
    버튼 메시지가 없으면 새로 올리고, 있으면 가장 최신 하나만 남기고, 예전 방식으로 올라간 결과 메시지는 지운다."""
    global random_panel_id
    if not RANDOM_CHANNEL_ID:
        return
    channel = bot.get_channel(RANDOM_CHANNEL_ID)
    if channel is None:
        return  # 이유는 on_ready의 채널 점검 출력에 나온다
    try:
        mine = [m async for m in channel.history(limit=50) if m.author.id == bot.user.id]  # 최신 글부터
        panels = [m for m in mine if has_random_buttons(m)]
        keep = panels[0] if panels else None
        for m in mine:
            if m is not keep:
                try:
                    await m.delete()
                except discord.NotFound:
                    pass
        if keep:
            random_panel_id = keep.id
            if keep.embeds and keep.embeds[0].title == RANDOM_PANEL_TITLE:
                await keep.edit(embed=random_panel_embed(), view=RandomView())  # 안내 문구는 최신으로
            else:
                await keep.edit(view=RandomView())  # 결과가 떠 있으면 그대로 두고 버튼만 다시 연결한다
        else:
            sent = await channel.send(embed=random_panel_embed(), view=RandomView(), silent=True)
            random_panel_id = sent.id
    except discord.Forbidden:
        print("랜덤퍽 안내 메시지를 올리지 못했어요: 봇에 채널 보기/메시지 보내기/링크 임베드/메시지 기록 보기 권한이 필요해요.")


# ---------- /퀴즈: 나만 볼 수 있는 임시 채널에서 진행 ----------
QUIZ_IDLE_SECONDS = 180       # 이 시간 동안 퀴즈 참가자의 입력이 없으면 채널을 지운다
QUIZ_TOPIC = "dbd-quiz-session"  # 봇이 만든 퀴즈 채널 표시 (봇이 재시작된 뒤 남은 채널을 정리할 때 쓴다)
QUIZ_CATEGORY_NAME = "봇이랑놀자"  # 퀴즈 채널을 이 이름의 카테고리 아래에 만든다
QUIZ_PASS_WORDS = {"패스", "pass", "skip"}
QUIZ_END_WORDS = {"종료", "끝", "exit"}
NO_MENTIONS = discord.AllowedMentions.none()


class QuizSession:
    """한 사람 전용 퀴즈 채널 하나. 채팅으로 정답을 입력하고, 입력이 없으면 채널째 사라진다."""

    def __init__(self, channel, user, interaction):
        self.channel, self.user, self.interaction = channel, user, interaction
        self.score = self.asked = 0
        self.perk, self.answers, self.seen = None, set(), set()
        self.closed = False
        self.last_input = time.monotonic()
        self.lock = asyncio.Lock()
        self.hint_task = self.idle_task = None

    def touch(self):
        self.last_input = time.monotonic()

    async def send(self, **kw):
        # silent: 알림 없이, allowed_mentions: 멘션이 있어도 아무도 호출하지 않는다.
        return await self.channel.send(**kw, silent=True, allowed_mentions=NO_MENTIONS)

    async def start(self):
        bot.quiz_by_channel[self.channel.id] = self
        bot.quiz_by_user[self.user.id] = self
        intro = discord.Embed(title="🎯 퍽 이름 맞히기", color=0x95A5A6, description=(
            "아이콘을 보고 퍽 이름을 **채팅으로** 입력하세요. 한글·영문 모두 정답으로 인정해요.\n"
            "`패스` 건너뛰기 · `종료` 끝내기\n"
            f"{QUIZ_IDLE_SECONDS // 60}분 동안 입력이 없으면 이 채널은 자동으로 사라져요."))
        await self.send(embed=intro)
        self.idle_task = asyncio.create_task(self.watch_idle())
        await self.next_question()

    async def next_question(self):
        for _ in range(5):  # 이미 낸 퍽은 되도록 피한다
            perk = await asyncio.to_thread(queries.random_perk)
            if perk["id"] not in self.seen:
                break
        self.seen.add(perk["id"])
        self.perk, self.answers = perk, queries.answers_of(perk)
        self.asked += 1
        embed = discord.Embed(title=f"문제 {self.asked}", description="이 퍽의 이름은?", color=0x95A5A6)
        embed.set_image(url=perk["image_url"])
        message = await self.send(embed=embed)
        if self.hint_task and not self.hint_task.done():
            self.hint_task.cancel()
        self.hint_task = asyncio.create_task(self.hint_later(self.asked, message, embed))

    async def hint_later(self, number, message, embed):
        await asyncio.sleep(HINT_AT)
        if self.closed or self.asked != number:
            return
        embed.add_field(name="힌트", value=f"{ROLE_LABEL[self.perk['role']]} 퍽 · 초성: "
                                         f"{queries.chosung_hint(self.perk['name_ko'])}")
        try:
            await message.edit(embed=embed)
        except discord.HTTPException:
            pass

    def reveal_embed(self, correct):
        p = self.perk
        embed = discord.Embed(title="정답! 🎉" if correct else "패스", color=0x2ECC71 if correct else 0xE67E22,
                              description=f"**{p['name_ko']}** ({p['name_en']})")
        embed.set_footer(text=f"현재 {self.score}점")
        return embed

    async def handle(self, message):
        """퀴즈 채널에서 참가자가 쓴 글을 처리한다."""
        async with self.lock:
            if self.closed:
                return
            self.touch()
            text = message.content.strip()
            if not text:
                return
            if text.lower() in QUIZ_END_WORDS:
                await self.close("퀴즈를 끝냈어요")
            elif text.lower() in QUIZ_PASS_WORDS:
                await self.send(embed=self.reveal_embed(False))
                await self.next_question()
            elif queries.normalize(text) in self.answers:
                self.score += 1
                await self.send(embed=self.reveal_embed(True))
                await self.next_question()
            else:
                try:
                    await message.add_reaction("❌")
                except discord.HTTPException:
                    pass

    async def watch_idle(self):
        while not self.closed:
            remaining = self.last_input + QUIZ_IDLE_SECONDS - time.monotonic()
            if remaining <= 0:
                await self.close(f"{QUIZ_IDLE_SECONDS // 60}분 동안 입력이 없어 퀴즈를 끝냈어요")
                return
            await asyncio.sleep(remaining)

    async def close(self, reason, delete_channel=True):
        if self.closed:
            return
        self.closed = True
        bot.quiz_by_channel.pop(self.channel.id, None)
        bot.quiz_by_user.pop(self.user.id, None)
        current = asyncio.current_task()
        for task in (self.hint_task, self.idle_task):
            if task and task is not current and not task.done():
                task.cancel()
        if delete_channel:
            try:
                await self.channel.delete(reason="퀴즈 종료")
            except discord.NotFound:
                pass
            except discord.HTTPException as e:
                print(f"퀴즈 채널 삭제 실패: {e}. 봇에 '채널 관리' 권한이 있는지 확인하세요.")
        try:  # 처음 받은 '채널을 만들었어요' 안내를 결과로 바꿔 준다 (디스코드가 허용하는 15분 안에서만 가능)
            await self.interaction.edit_original_response(
                content=f"{reason} · 정답 {self.score}개 / 출제 {self.asked}문제")
        except discord.HTTPException:
            pass


@bot.tree.command(name="퀴즈", description="나만 볼 수 있는 임시 채널에서 퍽 이름 맞히기 퀴즈를 합니다")
@app_commands.guild_only()
async def quiz(interaction: discord.Interaction):
    user, guild = interaction.user, interaction.guild
    existing = bot.quiz_by_user.get(user.id)
    if existing:
        await interaction.response.send_message(f"이미 퀴즈 채널이 있어요: {existing.channel.mention}",
                                                ephemeral=True, silent=True)
        return
    await interaction.response.defer(ephemeral=True)
    have = guild.me.guild_permissions
    if not have.manage_channels:
        await interaction.followup.send("봇에 '채널 관리' 권한이 없어서 퀴즈 채널을 만들 수 없어요. 서버 관리자에게 알려 주세요.",
                                        ephemeral=True, silent=True)
        return
    # 모두에게는 숨기고 참가자와 봇만 볼 수 있게 한다. 봇이 가진 권한만 요청해야 오류가 나지 않는다.
    bot_wants = ("view_channel", "send_messages", "embed_links", "read_message_history", "add_reactions")
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(**{p: True for p in bot_wants if getattr(have, p)}),
    }
    # '봇이랑놀자' 카테고리 아래에 만든다. 이름에 장식이 붙어 있어도(예: "━ 봇이랑놀자 ━") 찾을 수 있게 포함 여부로 찾는다.
    category = next((c for c in guild.categories if QUIZ_CATEGORY_NAME in c.name), None)
    if category is None:
        print(f"경고: '{QUIZ_CATEGORY_NAME}' 카테고리를 찾지 못해 퀴즈 채널을 카테고리 없이 만들어요.")
    try:
        channel = await guild.create_text_channel(f"퀴즈-{user.name}"[:90], overwrites=overwrites, topic=QUIZ_TOPIC,
                                                  category=category, reason=f"{user} 님의 퀴즈")
    except discord.HTTPException as e:
        await interaction.followup.send(f"퀴즈 채널을 만들지 못했어요: {e}", ephemeral=True, silent=True)
        return
    session = QuizSession(channel, user, interaction)
    try:
        await session.start()
    except Exception as e:  # 시작에 실패하면 만들어 둔 채널을 남기지 않는다
        print(f"퀴즈 시작 실패: {type(e).__name__}: {e}")
        await session.close("퀴즈를 시작하지 못했어요")
        return
    await interaction.followup.send(f"퀴즈 채널을 만들었어요: {channel.mention}\n"
                                    f"{QUIZ_IDLE_SECONDS // 60}분 동안 입력이 없으면 자동으로 사라져요.",
                                    ephemeral=True, silent=True)


@bot.event
async def on_guild_channel_delete(channel):
    """참가자가 퀴즈 채널을 직접 지운 경우 세션을 정리한다."""
    session = bot.quiz_by_channel.get(channel.id)
    if session:
        await session.close("채널이 삭제됐어요", delete_channel=False)


PERMISSION_LABELS = {"view_channel": "채널 보기", "send_messages": "메시지 보내기", "embed_links": "링크 임베드",
                     "read_message_history": "메시지 기록 보기", "manage_messages": "메시지 관리"}
# 채널별로 봇에게 꼭 필요한 권한 (시작할 때 부족한 것을 알려준다)
CHANNEL_NEEDS = {
    "신전 알림": ("view_channel", "send_messages", "embed_links", "read_message_history"),
    "검색": ("view_channel", "send_messages", "embed_links", "read_message_history", "manage_messages"),
    "랜덤퍽": ("view_channel", "send_messages", "embed_links", "read_message_history"),
}


def missing_permissions(channel, needed):
    """봇이 이 채널에서 갖지 못한 권한의 한글 이름 목록."""
    perms = channel.permissions_for(channel.guild.me)
    return [PERMISSION_LABELS[p] for p in needed if not getattr(perms, p)]


@bot.event
async def on_ready():
    print(f"로그인: {bot.user} (서버 {len(bot.guilds)}개)")
    # 채널 ID 문제를 바로 알 수 있게, 봇이 들어가 있는 서버와 알림/검색 채널이 보이는지 출력한다.
    for g in bot.guilds:
        print(f"  서버: {g.name} (ID {g.id})")
    for label, cid in (("신전 알림", SHRINE_CHANNEL_ID), ("검색", SEARCH_CHANNEL_ID), ("랜덤퍽", RANDOM_CHANNEL_ID)):
        if not cid:
            print(f"  {label} 채널: 꺼져 있어요 (채널 ID가 0이에요)")
            continue
        ch = bot.get_channel(cid)
        if ch is None:
            print(f"  경고: {label} 채널({cid})이 봇에게 보이지 않아요. 봇이 그 채널이 있는 서버에 초대돼 있는지, "
                  f"채널 ID가 맞는지(서버 ID나 카테고리 ID가 아닌지), 채널 보기 권한이 있는지 확인하세요.")
        else:
            print(f"  {label} 채널: #{ch.name} (서버 {ch.guild.name})")
            lacking = missing_permissions(ch, CHANNEL_NEEDS[label])
            if lacking:
                print(f"  ⚠ #{ch.name}에서 봇에게 없는 권한: {', '.join(lacking)} "
                      f"(채널 편집 → 권한에서 봇 역할에 허용하세요)")
    # 봇이 꺼진 사이에 남은 퀴즈 채널(이전 실행에서 만든 것)을 정리한다.
    for g in bot.guilds:
        for ch in g.text_channels:
            if ch.topic == QUIZ_TOPIC and ch.id not in bot.quiz_by_channel:
                try:
                    await ch.delete(reason="이전 퀴즈 세션 정리")
                    print(f"  남은 퀴즈 채널 삭제: #{ch.name}")
                except discord.HTTPException as e:
                    print(f"  남은 퀴즈 채널 #{ch.name}을(를) 지우지 못했어요: {e}")
    await ensure_random_panel()  # 재접속으로 on_ready가 다시 불려도 안내 메시지는 하나만 유지된다


if __name__ == "__main__":
    try:
        bot.run(os.environ["DISCORD_TOKEN"])
    except discord.PrivilegedIntentsRequired:
        raise SystemExit("메시지 내용 권한이 꺼져 있어요. 개발자 포털 → Bot → 'Message Content Intent'를 켜고 다시 실행하세요. "
                         "(#검색 채널과 퀴즈 채널이 채팅 내용을 읽는 데 필요해요.)")
