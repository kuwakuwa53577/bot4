import asyncio
import json
import os
import datetime
from threading import Thread

import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask

import firebase_admin
from firebase_admin import credentials, firestore

import logging

# ログ設定
logging.basicConfig(level=logging.INFO)

# --------------------------------------------------
# 設定 & 環境変数
# --------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("DISCORD_BOT_TOKEN")
PROXY_URL = os.getenv("PROXY_URL")
PORT = int(os.getenv("PORT", 10000)) # Render用のポート

# 各種チャンネルID
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))         # 総合ログチャンネル
SUMMARY_CHANNEL_ID = int(os.getenv("SUMMARY_CHANNEL_ID", "0")) # メンバーの招待リンク集約用チャンネル

# 管理者・保護対象ユーザーのID設定
auth_env = os.getenv("AUTHORIZED_USER_IDS", "")
if auth_env:
    AUTHORIZED_USER_IDS = {int(uid.strip()) for uid in auth_env.split(",") if uid.strip().isdigit()}
else:
    AUTHORIZED_USER_IDS = {123456789012345678}

message_spam_track = {}

# レイド防衛用の設定
RAID_TIME_WINDOW = 10  
RAID_JOIN_LIMIT = 5    
recent_joins = []      
is_raid_mode = False   

# --------------------------------------------------
# Flask サーバー（Renderのポート監視パス用）
# --------------------------------------------------
app = Flask(__name__)

@app.route("/")
@app.route("/ping")
def ping():
    return "Bot is running!", 200

# --------------------------------------------------
# Firebase 初期化
# --------------------------------------------------
firebase_creds_json = os.getenv("FIREBASE_CREDENTIALS")
if firebase_creds_json:
    try:
        cred_dict = json.loads(firebase_creds_json)
        if "private_key" in cred_dict:
            cred_dict["private_key"] = cred_dict["private_key"].replace("\\n", "\n")
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
        print("✅ Firebase Admin SDK の初期化に成功しました")
    except Exception as e:
        print(f"❌ Firebase 初期化エラー: {e}")
elif os.path.exists("serviceAccountKey.json"):
    try:
        cred = credentials.Certificate("serviceAccountKey.json")
        firebase_admin.initialize_app(cred)
        print("✅ ローカルファイルで Firebase の初期化に成功しました")
    except Exception as e:
        print(f"❌ Firebase 初期化エラー: {e}")

try:
    db = firestore.client()
except Exception as e:
    db = None
    print(f"⚠️ Firestoreクライアントの取得に失敗しました: {e}")

def load_all_data():
    if not db:
        return {}
    try:
        docs = db.collection("verifications").stream()
        return {doc.id: doc.to_dict() for doc in docs}
    except Exception as e:
        return {}

def get_user_ip_from_db(user_id: int) -> str:
    if not db:
        return "データベース未接続"
    try:
        doc_ref = db.collection("verifications").document(str(user_id))
        doc = doc_ref.get()
        if doc.exists:
            return doc.to_dict().get("ip", "IP未記録")
    except Exception as e:
        pass
    return "IP取得失敗"

def is_authorized_or_admin(member: discord.Member) -> bool:
    if not member:
        return False
    if member.id in AUTHORIZED_USER_IDS or member.guild_permissions.administrator:
        return True
    return False

# ログ送信用ヘルパー関数
async def send_log(guild: discord.Guild, title: str, description: str, color=0x5865F2):
    if LOG_CHANNEL_ID == 0:
        return
    channel = guild.get_channel(LOG_CHANNEL_ID)
    if channel:
        embed = discord.Embed(title=title, description=description, color=color, timestamp=datetime.datetime.now())
        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"❌ ログ送信エラー: {e}")

# --------------------------------------------------
# Discord Bot クラス
# --------------------------------------------------
class SecurityBot(commands.Bot):
    async def setup_hook(self):
        synced = await self.tree.sync()
        print(f"✅ スラッシュコマンド同期完了: {len(synced)} 件")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.moderation = True

bot = SecurityBot(command_prefix="!", intents=intents, proxy=PROXY_URL)

@bot.event
async def on_ready():
    print("========================================")
    print(f"✅ セキュリティ要塞Bot 接続成功: {bot.user}")
    print("========================================")

# --------------------------------------------------
# メンバー参加時の防衛チェック
# --------------------------------------------------
@bot.event
async def on_member_join(member: discord.Member):
    global is_raid_mode, recent_joins
    guild = member.guild
    
    # 1. アカウント作成日の確認（作りたて垢チェック：3日未満）
    account_age_days = (datetime.datetime.now(datetime.timezone.utc) - member.created_at).days
    if account_age_days < 3:
        await send_log(
            guild,
            "⚠️【警戒】作りたてのアカウントが参加しました",
            f"**ユーザー:** {member.mention} (`{member.id}`)\n**アカウント作成日:** {member.created_at.strftime('%Y-%m-%d')} ({account_age_days}日前)",
            color=0xFFA500
        )

    # 2. IP重複・一致チェック
    current_user_ip = get_user_ip_from_db(member.id)
    if current_user_ip not in ["データベース未接続", "IP未記録", "IP取得失敗"]:
        all_data = load_all_data()
        for uid, info in all_data.items():
            if int(uid) != member.id and info.get("ip") == current_user_ip:
                other_username = info.get("username", "不明")
                await send_log(
                    guild,
                    "🚨【重要警戒】同一IPのユーザーが入室しました！",
                    f"**新規ユーザー:** {member.mention} (`{member.id}`)\n**一致した過去ユーザー:** {other_username} (ID: `{uid}`)\n**共通IP:** `{current_user_ip}`",
                    color=0xFF0000
                )
                break

    # 3. レイド検知
    now = datetime.datetime.now()
    recent_joins.append(now)
    recent_joins = [t for t in recent_joins if (now - t).total_seconds() < RAID_TIME_WINDOW]
    
    if len(recent_joins) >= RAID_JOIN_LIMIT and not is_raid_mode:
        is_raid_mode = True
        await send_log(
            guild,
            "🚨【緊急防衛発動】自爆ドローン（レイド攻撃）検知！",
            f"短時間に {len(recent_joins)} 人の参加を検知したため、防衛モード（メッセージ自動削除）を有効化しました。`/unlock` で解除できます。",
            color=0xFF0000
        )

    await send_log(guild, "📥 メンバー入室", f"**{member}** (`{member.id}`) がサーバーに参加しました。")

@bot.event
async def on_member_remove(member: discord.Member):
    await send_log(member.guild, "📤 メンバー退室", f"**{member}** (`{member.id}`) がサーバーから退出しました。", color=0x808080)

# --------------------------------------------------
# 各種ログ監視イベント（メッセージ・ロール・チャンネル）
# --------------------------------------------------
@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    content = message.content if message.content else "[テキストなし/埋め込み等]"
    if len(content) > 500:
        content = content[:500] + "..."
    await send_log(
        message.guild,
        "🗑️ メッセージ削除",
        f"**送信者:** {message.author.mention}\n**チャンネル:** {message.channel.mention}\n**内容:** {content}",
        color=0xF39C12
    )

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    author = message.author
    
    # 管理者や権限持ちは防衛モード中でもスルーして通常処理へ
    if isinstance(author, discord.Member) and is_authorized_or_admin(author):
        await bot.process_commands(message)
        return

    # 防衛モード中の場合はチャンネル権限をいじらずにメッセージを自動削除
    global is_raid_mode
    if is_raid_mode:
        try:
            await message.delete()
        except Exception:
            pass
        return

    content = message.content
    user_id = author.id

    # メンバーが投稿した宣伝・招待リンクを自動集約
    if SUMMARY_CHANNEL_ID != 0 and ("discord.gg/" in content or "discord.com/invite/" in content):
        if message.channel.id != SUMMARY_CHANNEL_ID:
            summary_channel = message.guild.get_channel(SUMMARY_CHANNEL_ID)
            if summary_channel:
                embed = discord.Embed(
                    title="📢 メンバーからの招待リンク投稿を検知",
                    description=f"**送信者:** {author.mention}\n**チャンネル:** {message.channel.mention}\n\n{content}",
                    color=0x5865F2
                )
                await summary_channel.send(embed=embed)

    # 1. @everyone 過剰検知
    everyone_count = content.count("@everyone") + content.count("@here")
    if everyone_count >= 5 or (len(message.raw_mentions) >= 5 and message.mention_everyone):
        try:
            ip = get_user_ip_from_db(user_id)
            await message.guild.ban(author, reason=f"荒らし対策: @everyone過剰 (IP: {ip})")
            await send_log(message.guild, "🔨 自動BAN発動 (@everyone過剰)", f"**ユーザー:** {author} (`{user_id}`)\n**IP:** `{ip}`", color=0xFF0000)
            return
        except discord.Forbidden:
            pass

    # 2. 同一メッセージ連投検知
    stripped_content = content.strip()
    if user_id in message_spam_track:
        last_content, count = message_spam_track[user_id]
        if last_content == stripped_content:
            count += 1
            if count >= 4:
                try:
                    ip = get_user_ip_from_db(user_id)
                    await message.guild.ban(author, reason=f"荒らし対策: 同一メッセージ連投 (IP: {ip})")
                    message_spam_track.pop(user_id, None)
                    await send_log(message.guild, "🔨 自動BAN発動 (同一メッセージ連投)", f"**ユーザー:** {author} (`{user_id}`)\n**IP:** `{ip}`", color=0xFF0000)
                    return
                except discord.Forbidden:
                    pass
            else:
                message_spam_track[user_id] = [stripped_content, count]
        else:
            message_spam_track[user_id] = [stripped_content, 1]
    else:
        message_spam_track[user_id] = [stripped_content, 1]

    # 3. 5行以上長文検知
    if len(content.splitlines()) >= 5:
        try:
            ip = get_user_ip_from_db(user_id)
            await message.guild.ban(author, reason=f"荒らし対策: 5行以上長文 (IP: {ip})")
            await send_log(message.guild, "🔨 自動BAN発動 (5行以上長文)", f"**ユーザー:** {author} (`{user_id}`)\n**IP:** `{ip}`", color=0xFF0000)
            return
        except discord.Forbidden:
            pass

    await bot.process_commands(message)

# チャンネル・ロール・ボット導入の各種ログイベント
@bot.event
async def on_guild_channel_create(channel):
    await send_log(channel.guild, "➕ チャンネル作成", f"チャンネル **{channel.name}** ({channel.type}) が作成されました。")

@bot.event
async def on_guild_channel_delete(channel):
    await send_log(channel.guild, "❌ チャンネル削除", f"チャンネル **{channel.name}** が削除されました。", color=0xFF0000)

@bot.event
async def on_guild_role_create(role):
    await send_log(role.guild, "🛡️ ロール作成", f"ロール **{role.name}** が作成されました。")

@bot.event
async def on_guild_role_delete(role):
    await send_log(role.guild, "🛡️ ロール削除", f"ロール **{role.name}** が削除されました。", color=0xFF0000)

@bot.event
async def on_member_update(before, after):
    if not before.bot and after.bot:
        await send_log(after.guild, "🤖 ボット導入", f"新しいボット **{after.mention}** がサーバーに追加されました。")

# --------------------------------------------------
# スラッシュコマンド
# --------------------------------------------------
@bot.tree.command(name="unlock", description="レイド防衛モードを解除し、通常の状態に戻します")
@app_commands.default_permissions(administrator=True)
async def unlock_command(interaction: discord.Interaction):
    global is_raid_mode, recent_joins
    is_raid_mode = False
    recent_joins.clear()
    await interaction.response.send_message("✅ 防衛モードを解除しました。通常通りメッセージを投稿できます。", ephemeral=True)

@bot.tree.command(name="ipban", description="特定のユーザーをBANし、DBからIPを確認します")
@app_commands.describe(user1="対象1", user2="対象2", user3="対象3")
@app_commands.default_permissions(administrator=True)
async def ipban_command(interaction: discord.Interaction, user1: discord.Member, user2: discord.Member = None, user3: discord.Member = None):
    if not is_authorized_or_admin(interaction.user):
        await interaction.response.send_message("❌ 権限がありません。", ephemeral=True)
        return

    targets = [u for u in [user1, user2, user3] if u is not None]
    banned_results = []
    
    for target in targets:
        if is_authorized_or_admin(target):
            continue
        ip = get_user_ip_from_db(target.id)
        try:
            await interaction.guild.ban(target, reason=f"IP/ID BAN実行: {interaction.user} (IP: {ip})")
            banned_results.append(f"・**{target.name}** (IP: `{ip}`)")
        except Exception as e:
            pass

    result_msg = "🔨 **IP/ID BAN 処理完了**\n" + "\n".join(banned_results)
    await interaction.response.send_message(result_msg, ephemeral=True)
    await send_log(interaction.guild, "🔨 手動IPBAN実行", f"実行者: {interaction.user}\n対象者数: {len(banned_results)}人", color=0xFF0000)

# --------------------------------------------------
# メイン実行 (Flask と Discord Bot を同時に起動)
# --------------------------------------------------
if __name__ == "__main__":
    if not BOT_TOKEN:
        print("❌ エラー: BOT_TOKEN が設定されていません。")
    else:
        # Flaskを別スレッドで起動してRenderのポート要件をクリアする
        flask_thread = Thread(target=lambda: app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False))
        flask_thread.daemon = True
        flask_thread.start()
        print(f"🌐 Flask サーバーをポート {PORT} で起動しました")

        # Discord Bot 起動
        bot.run(BOT_TOKEN)
