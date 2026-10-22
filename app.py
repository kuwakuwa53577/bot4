import asyncio
import json
import os
import datetime
from threading import Thread

import discord
from discord import app_commands
from discord.ext import commands, tasks
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
EXECUTION_CHANNEL_ID = int(os.getenv("EXECUTION_CHANNEL_ID", "0")) # ★公開処刑・監獄チャンネルID

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

# --------------------------------------------------
# 監獄（Jail）データベース操作関数
# --------------------------------------------------
def add_to_jail(user_id: int, username: str, ip: str, reason: str, last_message: str):
    if not db:
        return
    try:
        db.collection("jail_list").document(str(user_id)).set({
            "username": username,
            "ip": ip,
            "reason": reason,
            "last_message": last_message,
            "jailed_at": firestore.SERVER_TIMESTAMP
        })
        print(f"🔒 ユーザー {username} ({user_id}) を監獄に記録しました")
    except Exception as e:
        print(f"❌ 監獄登録エラー: {e}")

def is_user_jailed(user_id: int) -> bool:
    if not db:
        return False
    try:
        doc = db.collection("jail_list").document(str(user_id)).get()
        return doc.exists
    except Exception as e:
        return False

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
# Discord Bot クラス & バックグラウンドタスク
# --------------------------------------------------
class SecurityBot(commands.Bot):
    async def setup_hook(self):
        synced = await self.tree.sync()
        print(f"✅ スラッシュコマンド同期完了: {len(synced)} 件")
        # 永遠メンション用バックグラウンドタスク開始
        execution_loop.start()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.moderation = True

bot = SecurityBot(command_prefix="!", intents=intents, proxy=PROXY_URL)

# ★1分ごとに監獄の対象者を処刑チャンネルでメンションし続けるループ
@tasks.loop(seconds=1)
async def execution_loop():
    if EXECUTION_CHANNEL_ID == 0 or not db:
        return
    try:
        docs = db.collection("jail_list").stream()
        for doc in docs:
            data = doc.to_dict()
            user_id = int(doc.id)
            username = data.get("username", "不明")
            ip = data.get("ip", "IP不明")
            reason = data.get("reason", "理由未記録")
            last_msg = data.get("last_message", "なし")

            for guild in bot.guilds:
                channel = guild.get_channel(EXECUTION_CHANNEL_ID)
                if channel:
                    await channel.send(
                        f"🔒 **【永久公開処刑・永続監視中】** <@{user_id}>\n"
                        f"👤 **対象:** `{username}` (ID: `{user_id}`)\n"
                        f"🌐 **記録IP:** `{ip}`\n"
                        f"📜 **罪状:** {reason}\n"
                        f"💬 **検知メッセージ:** `{last_msg}`"
                    )
    except Exception as e:
        print(f"❌ 処刑ループ実行エラー: {e}")

@execution_loop.before_loop
async def before_execution_loop():
    await bot.wait_until_ready()

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
    
    # 1. アカウント作成日の確認（3日未満）
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
# メッセージ監視 & 自動監獄（Jail）判定
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
    user_id = author.id
    
    # 管理者・許可ユーザーはスルー
    if isinstance(author, discord.Member) and is_authorized_or_admin(author):
        await bot.process_commands(message)
        return

    # ★すでに監獄に送られている荒らしユーザーメッセージは即削除（言論統制）
    if is_user_jailed(user_id):
        try:
            await message.delete()
        except Exception:
            pass
        return

    # 防衛モード中の自動削除
    global is_raid_mode
    if is_raid_mode:
        try:
            await message.delete()
        except Exception:
            pass
        return

    content = message.content

    # 宣伝・招待リンク集約
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

    # 1. @everyone 過剰検知 -> 監獄（Jail）送り
    everyone_count = content.count("@everyone") + content.count("@here")
    if everyone_count >= 5 or (len(message.raw_mentions) >= 5 and message.mention_everyone):
        try:
            ip = get_user_ip_from_db(user_id)
            add_to_jail(user_id, str(author), ip, "@everyone過剰スパム", content[:100])
            await message.delete()
            await send_log(message.guild, "🔒 自動監獄送り (@everyone過剰)", f"**ユーザー:** {author} (`{user_id}`)\n**IP:** `{ip}`", color=0xFF0000)
            return
        except Exception:
            pass

    # 2. 同一メッセージ連投検知 -> 監獄（Jail）送り
    stripped_content = content.strip()
    if user_id in message_spam_track:
        last_content, count = message_spam_track[user_id]
        if last_content == stripped_content:
            count += 1
            if count >= 4:
                try:
                    ip = get_user_ip_from_db(user_id)
                    add_to_jail(user_id, str(author), ip, "同一メッセージ連投スパム", stripped_content[:100])
                    message_spam_track.pop(user_id, None)
                    await message.delete()
                    await send_log(message.guild, "🔒 自動監獄送り (同一メッセージ連投)", f"**ユーザー:** {author} (`{user_id}`)\n**IP:** `{ip}`", color=0xFF0000)
                    return
                except Exception:
                    pass
            else:
                message_spam_track[user_id] = [stripped_content, count]
        else:
            message_spam_track[user_id] = [stripped_content, 1]
    else:
        message_spam_track[user_id] = [stripped_content, 1]

    # 3. 5行以上長文検知 -> 監獄（Jail）送り
    if len(content.splitlines()) >= 5:
        try:
            ip = get_user_ip_from_db(user_id)
            add_to_jail(user_id, str(author), ip, "5行以上の長文スパム", content[:100])
            await message.delete()
            await send_log(message.guild, "🔒 自動監獄送り (5行以上長文)", f"**ユーザー:** {author} (`{user_id}`)\n**IP:** `{ip}`", color=0xFF0000)
            return
        except Exception:
            pass

    await bot.process_commands(message)

# --------------------------------------------------
# スラッシュコマンド（既存機能維持）
# --------------------------------------------------
@bot.tree.command(name="unlock", description="レイド防衛モードを解除し、通常の状態に戻します")
@app_commands.default_permissions(administrator=True)
async def unlock_command(interaction: discord.Interaction):
    global is_raid_mode, recent_joins
    is_raid_mode = False
    recent_joins.clear()
    await interaction.response.send_message("✅ 防衛モードを解除しました。通常通りメッセージを投稿できます。", ephemeral=True)

@bot.tree.command(name="ipban", description="特定のユーザーを手動でBANし、DBからIPを確認します")
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
# 監獄から解放するスラッシュコマンド
# --------------------------------------------------
@bot.tree.command(name="unjail", description="指定したユーザーを監獄（処刑リスト）から解放します")
@app_commands.describe(user="解放したいユーザー")
@app_commands.default_permissions(administrator=True)
async def unjail_command(interaction: discord.Interaction, user: discord.Member):
    if not is_authorized_or_admin(interaction.user):
        await interaction.response.send_message("❌ 権限がありません。", ephemeral=True)
        return

    if not db:
        await interaction.response.send_message("❌ データベースに接続されていません。", ephemeral=True)
        return

    user_id_str = str(user.id)
    doc_ref = db.collection("jail_list").document(user_id_str)
    
    # Firebaseからデータを削除
    doc = doc_ref.get()
    if doc.exists:
        doc_ref.delete()
        await interaction.response.send_message(f"✅ ユーザー {user.mention} を監獄から解放（釈放）しました！", ephemeral=True)
        await send_log(interaction.guild, "🔓 監獄解放（釈放）実行", f"対象者: {user.mention} (`{user.id}`)\n実行者: {interaction.user}", color=0x00FF00)
    else:
        await interaction.response.send_message(f"⚠️ {user.mention} は監獄に入っていません。", ephemeral=True)

# --------------------------------------------------
# メイン実行
# --------------------------------------------------
if __name__ == "__main__":
    if not BOT_TOKEN:
        print("❌ エラー: BOT_TOKEN が設定されていません。")
    else:
        flask_thread = Thread(target=lambda: app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False))
        flask_thread.daemon = True
        flask_thread.start()
        print(f"🌐 Flask サーバーをポート {PORT} で起動しました")

        bot.run(BOT_TOKEN)
