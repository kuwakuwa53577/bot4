import asyncio
import json
import os
import discord
from discord import app_commands
from discord.ext import commands

import firebase_admin
from firebase_admin import credentials, firestore

# --------------------------------------------------
# 環境変数 & 設定
# --------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("DISCORD_BOT_TOKEN")

# 📌 特定のユーザーID（複数指定可能）
auth_env = os.getenv("AUTHORIZED_USER_IDS", "")
if auth_env:
    AUTHORIZED_USER_IDS = {int(uid.strip()) for uid in auth_env.split(",") if uid.strip().isdigit()}
else:
    AUTHORIZED_USER_IDS = {1488072800278413500}  # ← ここに実際のDiscordユーザーIDを入力してください

# スパム検知用の一時メモリ: {user_id: [last_content, count]}
message_spam_track = {}

# --------------------------------------------------
# Firebase 初期化（FirestoreからIP情報を引き取るため）
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
else:
    print("⚠️ 警告: FIREBASE_CREDENTIALS または serviceAccountKey.json が見つかりません。")

try:
    db = firestore.client()
except Exception as e:
    db = None
    print(f"⚠️ Firestoreクライアントの取得に失敗しました: {e}")

# Firestoreから指定ユーザーのIP情報を取得するヘルパー関数
def get_user_ip_from_db(user_id: int) -> str:
    if not db:
        return "データベース未接続"
    try:
        doc_ref = db.collection("verifications").document(str(user_id))
        doc = doc_ref.get()
        if doc.exists:
            data = doc.to_dict()
            return data.get("ip", "IP未記録")
    except Exception as e:
        print(f"❌ DB読み込みエラー: {e}")
    return "IP取得失敗"

# --------------------------------------------------
# Discord Bot クラスの定義
# --------------------------------------------------
class MyBot(commands.Bot):
    async def setup_hook(self):
        synced = await self.tree.sync()
        print(f"✅ スラッシュコマンド同期完了: {len(synced)} 件")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = MyBot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print("========================================")
    print(f"✅ Discord Bot 接続成功: {bot.user}")
    print("========================================")

# 権限判定ヘルパー
def is_authorized_or_admin(member: discord.Member) -> bool:
    if not member:
        return False
    if member.id in AUTHORIZED_USER_IDS:
        return True
    if member.guild_permissions.administrator:
        return True
    return False

# --------------------------------------------------
# 荒らし対策（メッセージ監視イベント）
# --------------------------------------------------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    author = message.author

    # 管理者、または特定のユーザーは自動BANの対象外
    if isinstance(author, discord.Member) and is_authorized_or_admin(author):
        await bot.process_commands(message)
        return

    content = message.content
    user_id = author.id

    # 1. @everyone または @here を5回以上含んでいる場合
    everyone_count = content.count("@everyone") + content.count("@here")
    if everyone_count >= 5 or (len(message.raw_mentions) >= 5 and message.mention_everyone):
        try:
            ip = get_user_ip_from_db(user_id)
            await message.guild.ban(author, reason=f"荒らし対策: @everyone過剰 (IP: {ip})")
            return
        except discord.Forbidden:
            pass

    # 2. 同じメッセージを4回連続で送った場合
    stripped_content = content.strip()
    if user_id in message_spam_track:
        last_content, count = message_spam_track[user_id]
        if last_content == stripped_content:
            count += 1
            if count >= 4:
                try:
                    ip = get_user_ip_from_db(user_id)
                    await message.guild.ban(author, reason=f"荒らし対策: 同一メッセージ連投 (IP: {ip})")
                    del message_spam_track[user_id]
                    return
                except discord.Forbidden:
                    pass
            else:
                message_spam_track[user_id] = [stripped_content, count]
        else:
            message_spam_track[user_id] = [stripped_content, 1]
    else:
        message_spam_track[user_id] = [stripped_content, 1]

    # 3. 5行以上のメッセージを送った場合
    lines = content.splitlines()
    if len(lines) >= 5:
        try:
            ip = get_user_ip_from_db(user_id)
            await message.guild.ban(author, reason=f"荒らし対策: 5行以上長文 (IP: {ip})")
            return
        except discord.Forbidden:
            pass

    await bot.process_commands(message)

# --------------------------------------------------
# スラッシュコマンド: /ipban (特定ユーザー選択BAN ＆ DBからIP取得)
# --------------------------------------------------
@bot.tree.command(name="ipban", description="特定のユーザーを複数選択してBANし、DBからIPを確認します（管理者および特定ユーザー専用）")
@app_commands.describe(
    user1="BANする対象のユーザー1",
    user2="BANする対象のユーザー2（任意）",
    user3="BANする対象のユーザー3（任意）"
)
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
async def ipban_command(
    interaction: discord.Interaction, 
    user1: discord.Member, 
    user2: discord.Member = None, 
    user3: discord.Member = None
):
    if not is_authorized_or_admin(interaction.user):
        await interaction.response.send_message("❌ あなたにはこのコマンドを実行する権限がありません。", ephemeral=True)
        return

    targets = [u for u in [user1, user2, user3] if u is not None]
    
    banned_results = []
    failed_list = []

    for target in targets:
        if is_authorized_or_admin(target):
            failed_list.append(f"{target.name} (管理者・保護対象)")
            continue
        
        # データベースから対象ユーザーのIPを取得
        ip = get_user_ip_from_db(target.id)
        
        try:
            await interaction.guild.ban(target, reason=f"IP/ID BAN実行: {interaction.user} (IP: {ip})")
            banned_results.append(f"・**{target.name}** (IP: `{ip}`)")
        except Exception as e:
            failed_list.append(f"{target.name} (エラー: {e})")

    result_msg = "🔨 **IP/ID BAN 処理結果**\n"
    if banned_results:
        result_msg += "\n".join(banned_results) + "\n"
    if failed_list:
        result_msg += f"\n⚠️ 失敗/スキップ: {', '.join(failed_list)}"

    await interaction.response.send_message(result_msg, ephemeral=True)

# --------------------------------------------------
# Botの起動
# --------------------------------------------------
if __name__ == "__main__":
    if not BOT_TOKEN:
        print("❌ エラー: BOT_TOKEN が設定されていません。")
    else:
        bot.run(BOT_TOKEN)
