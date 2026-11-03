"""Python 3.10+; discord.py, playwright, requests.
Credentials are read only from environment variables. Do not share them.
"""
import asyncio
import traceback
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

import discord
from discord import app_commands
from discord.ext import commands
from playwright.async_api import async_playwright
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / '.env', override=False)
STATE_PATH = Path(os.getenv('STATE_PATH', '/tmp/combined_bot_state.json'))
STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

TARGET = 'https://m.cafe.naver.com/ca-fe/onimobile?tc=article&tab=notice'
INTERVAL = 120


def article_key(url):
    p = urlsplit(url)
    if p.hostname not in ('cafe.naver.com', 'm.cafe.naver.com'):
        return None
    m = re.fullmatch(r'/ca-fe/cafes/(\d+)/articles/(\d+)/?', p.path)
    if m:
        return f'{m[1]}:{m[2]}'
    m = re.fullmatch(r'/onimobile/(\d+)/?', p.path)
    if m:
        return 'onimobile:' + m[1]
    q = parse_qs(p.query)
    if p.path in ('/ArticleRead.nhn', '/ca-fe/ArticleRead.nhn'):
        c, a = q.get('clubid', [''])[0], q.get('articleid', [''])[0]
        if c.isdigit() and a.isdigit():
            return c + ':' + a
    return None


class CombinedBot(commands.Bot):
    def __init__(self):
        # No privileged member/message-content intents required.
        super().__init__(command_prefix='!', intents=discord.Intents.default(),
                         allowed_mentions=discord.AllowedMentions.none())
        self.state = json.loads(STATE_PATH.read_text(encoding='utf-8')) if STATE_PATH.exists() else {
            'roles': {}, 'notice_initialized': False, 'seen': []}
        self.role_lock = asyncio.Lock()
        self.notice_task = None

    def save(self):
        temp = STATE_PATH.with_suffix('.tmp')
        temp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(STATE_PATH)

    async def setup_hook(self):
        synced = await self.tree.sync()
        print(f'전역 명령어 {len(synced)}개 등록 완료', flush=True)
        self.notice_task = asyncio.create_task(self.watch_notices())

    async def on_ready(self):
        print(f'로그인 완료: {self.user} / 서버 {len(self.guilds)}개', flush=True)

    async def close(self):
        if self.notice_task:
            self.notice_task.cancel()
            try:
                await self.notice_task
            except asyncio.CancelledError:
                pass
        await super().close()

    async def watch_notices(self):
        await self.wait_until_ready()
        try:
            channel = await self.fetch_channel(int(os.environ['DISCORD_CHANNEL_ID']))
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                print('알림 채널은 텍스트 채널 또는 스레드여야 합니다.')
                return
            async with async_playwright() as p:
                context = await p.chromium.launch_persistent_context(
                    os.getenv('BROWSER_PROFILE_DIR', '/tmp/naver_browser_profile_combined'),
                    headless=os.getenv('HEADLESS', '0') == '1',
                    viewport={'width': 480, 'height': 900})
                try:
                    page = context.pages[0] if context.pages else await context.new_page()
                    await page.goto(TARGET, wait_until='domcontentloaded', timeout=60000)
                    print('브라우저에서 필요하면 로그인하고 공지 목록 화면에 두세요.')
                    if os.getenv('HEADLESS', '0') != '1':
                        await asyncio.to_thread(input, '공지 목록이 보이면 터미널에서 Enter: ')
                    while not self.is_closed():
                        try:
                            if page.is_closed():
                                print('카페 브라우저가 닫혔습니다. 역할 기능은 유지됩니다. 알림 재개는 프로그램을 재시작하세요.')
                                return
                            await page.reload(wait_until='domcontentloaded', timeout=60000)
                            await page.wait_for_timeout(8000)
                            current = {}
                            for frame in page.frames:
                                links = await frame.locator('a[href]').evaluate_all(
                                    '(nodes) => nodes.map(a => ({url:a.href,title:(a.innerText||a.textContent||"새 공지").trim()}))')
                                for item in links:
                                    key = article_key(item['url'])
                                    if key:
                                        current.setdefault(key, item)
                            seen = set(self.state['seen'])
                            if not current:
                                print('공지 링크 없음: 로그인/접근 권한/사이트 구조를 확인하세요. 기존 상태 유지.')
                            elif not self.state['notice_initialized']:
                                self.state['seen'] = sorted(seen | set(current))
                                self.state['notice_initialized'] = True
                                self.save()
                                print(f'기존 공지 {len(current)}개 기록. 기존 글은 보내지 않습니다.')
                                item = next(iter(current.values()))
                                await channel.send(
                                f'🧪 카페 알림 테스트\n'
                                f'{item["title"][:200]}\n'
                                 f'{item["url"]}'
                                )
                            else:
                                fresh = [(k, v) for k, v in current.items() if k not in seen]
                                for key, item in reversed(fresh):
                                    title = ' '.join(item['title'].split())[:300] or '새 공지'
                                    await channel.send(f'📝 네이버 카페 새 공지\n{discord.utils.escape_markdown(title)}\n{item["url"]}')
                                    seen.add(key)
                                    self.state['seen'] = sorted(seen)
                                    self.save()
                                    await asyncio.sleep(1)
                                print(f'공지 확인: 목록 {len(current)}개 / 새 글 {len(fresh)}개')
                        except Exception as exc:
                            print('카페 확인 실패:', type(exc).__name__, '— 다음 주기에 재시도')
                        await asyncio.sleep(INTERVAL)
                finally:
                    await context.close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print('카페 알림 시작 실패:', type(exc).__name__, '— 역할 기능은 계속 사용 가능', flush=True)
            traceback.print_exc()


bot = CombinedBot()


@bot.tree.command(name='개인역할', description='권한 없는 개인 역할을 만들거나 기존 개인 역할의 이름·색상을 수정합니다.')
@app_commands.guild_only()
@app_commands.describe(이름='역할 이름 (1~32자)', 색상='예: #00D4AA (검정 #000000 제외)')
@app_commands.checks.cooldown(1, 15.0, key=lambda i: (i.guild_id, i.user.id))
async def personal_role(interaction: discord.Interaction, 이름: str, 색상: str):
    name = 이름.strip()
    color = 색상.strip()
    if not 1 <= len(name) <= 32 or any(ord(c) < 32 for c in name) or '@' in name:
        await interaction.response.send_message('이름은 @·제어문자 없이 1~32자로 입력하세요.', ephemeral=True)
        return
    if not re.fullmatch(r'#?[0-9a-fA-F]{6}', color):
        await interaction.response.send_message('색상은 #00D4AA처럼 6자리 HEX로 입력하세요.', ephemeral=True)
        return
    value = int(color.lstrip('#'), 16)
    if value == 0:
        await interaction.response.send_message('#000000은 Discord에서 기본 색상으로 처리됩니다. #010101 등을 사용하세요.', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    async with bot.role_lock:
        guild = interaction.guild
        member = await guild.fetch_member(interaction.user.id)
        me = guild.me
        if me is None or not me.guild_permissions.manage_roles:
            await interaction.followup.send('봇에 역할 관리 권한이 필요합니다.', ephemeral=True)
            return
        # Fetch authoritative roles so cached hierarchy does not decide ownership.
        roles = await guild.fetch_roles()
        by_id = {r.id: r for r in roles}
        bot_top = max((by_id[r.id] for r in me.roles if r.id in by_id), default=None)
        owner_key = f'{guild.id}:{member.id}'
        recorded_id = bot.state['roles'].get(owner_key)
        role = by_id.get(recorded_id)
        marker = f'개인-{member.id}'
        if role is None:
            # Recover a newly created role if the process stopped before its ID was saved.
            matches = [r for r in roles if r.name == marker and not r.managed and r.permissions.value == 0]
            if len(matches) > 1:
                await interaction.followup.send('복구용 역할이 여러 개입니다. 운영자가 확인해 주세요.', ephemeral=True)
                return
            role = matches[0] if matches else None
        if role is not None:
            if role.is_default() or role.managed or role.permissions.value != 0 or bot_top is None or role >= bot_top:
                await interaction.followup.send('기존 개인 역할의 권한이나 순위가 변경되었습니다. 안전을 위해 운영자 확인이 필요합니다.', ephemeral=True)
                return
        else:
            role = await guild.create_role(name=marker, permissions=discord.Permissions.none(),
                colour=discord.Colour(value), hoist=False, mentionable=False,
                reason=f'Personal role requested by user {member.id}')
        # Save ownership BEFORE later edit/grant calls. Failure on a later call will
        # reuse this same role next time rather than creating another one.
        bot.state['roles'][owner_key] = role.id
        bot.save()
        await role.edit(name=name, colour=discord.Colour(value), permissions=discord.Permissions.none(),
                        hoist=False, mentionable=False, reason='Owner personal-role update')
        await member.add_roles(role, reason='Self-service personal role', atomic=True)
        await interaction.followup.send(
            f'개인 역할 적용 완료: **{discord.utils.escape_markdown(name)}** / #{value:06X}\n'
            '다시 실행하면 같은 역할의 이름·색상만 수정됩니다.', ephemeral=True)


@personal_role.error
async def role_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    original = getattr(error, 'original', error)
    if isinstance(original, app_commands.CommandOnCooldown):
        text = f'{original.retry_after:.0f}초 뒤에 다시 실행해 주세요.'
    elif isinstance(original, discord.Forbidden):
        text = '역할 관리 권한과 봇 역할 순위를 확인해 주세요.'
    elif isinstance(original, discord.HTTPException):
        text = 'Discord 요청에 실패했습니다. 역할 수 제한·권한을 확인하고 다시 시도하세요.'
    else:
        text = '처리에 실패했습니다. 상태 파일의 쓰기 권한과 설정을 확인해 주세요.'
    print('역할 처리 오류:', type(original).__name__)
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


if __name__ == '__main__':
    for key in ('DISCORD_BOT_TOKEN', 'DISCORD_CHANNEL_ID'):
        if not os.environ.get(key, '').strip():
            raise SystemExit(f'{key} 환경 변수를 설정하세요.')
    for key in ('DISCORD_CHANNEL_ID',):
        if not os.environ[key].isdigit():
            raise SystemExit(f'{key}에는 숫자 ID를 넣으세요.')
    bot.run(os.environ['DISCORD_BOT_TOKEN'])
