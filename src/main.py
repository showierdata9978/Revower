from __future__ import annotations
from dotenv import dotenv_values, load_dotenv
import re

from MeowerBot import Bot
from MeowerBot.context import CTX, Post
from revolt import Client, TextChannel,  Masquerade, Message
import revolt as revolt_pkg
import asyncio
import aiohttp
import pymongo
import threading
import logging
from json import loads
from cachetools import TTLCache, cached as cached_sync
from asyncache import cached
import aiohttp
import requests
import traceback
from revolt.ext import commands
logging.basicConfig(level=logging.INFO)

load_dotenv()


MEOWER_USERNAME = dotenv_values()["meower_username"]
MEOWER_PASSWORD = dotenv_values()["meower_password"]
REVOLT_TOKEN = dotenv_values()["revolt_token"]
LINK_SHORTENER_KEY = dotenv_values()["url_shortener_token"]


REVOLT_EMOJI = re.compile(r"\:[A-Z0-9]+\:")

DATABASE = pymongo.MongoClient(dotenv_values().get(
    "mongo_url", "mongodb://localhost:27017"))["revolt-meower"]

# check if the environment variables are set
if not MEOWER_USERNAME or not MEOWER_PASSWORD or not REVOLT_TOKEN:
    raise ValueError("Environment variables not set")

PFPEXISTS = TTLCache(maxsize=1000, ttl=60*60*24*7)  # cache for 7 days

MEOWER = Bot(autoreload=0)
if MEOWER_USERNAME in MEOWER.__bridges__:
	MEOWER.__bridges__.remove(MEOWER_USERNAME)

LINKING_USERS = {}
LINKING_CHATS = {}
BYPASS_CHAT_LINKING = str(dotenv_values().get(
    "bypass_chat_linking", False)) == "True"


async def send_revolt_message(message: Post, chat_id: str, pfp):
    # send the message to the revolt channel
    try:
        chat: TextChannel = await revolt.fetch_channel(chat_id)  # type: ignore
    except revolt_pkg.errors.HTTPError:
        # the channel doesnt exist, so remove it from the database
        DATABASE.chats.delete_one({"revolt_chat": chat_id})
        return

    if type(chat) is not TextChannel:
        return
    try:
        await chat.send(content=str(message), masquerade=Masquerade(name=message.user.username, avatar=pfp))
    except revolt_pkg.errors.HTTPError:
        pass

@cached(cache=PFPEXISTS)
def get_user_pfp(meower_username: str) -> str:
    req = requests.get(f"https://api.meower.org/users/{meower_username}")
    try:
        if req.status_code == 200:
            data = req.json()
            if data["error"]: 
                return "https://showierdata9978.github.io/Revower/pfps/icon_err.png"
            return f"https://showierdata9978.github.io/Revower/pfps/icon_{data['pfp_data']-1}.png"
        else:
            return "https://showierdata9978.github.io/Revower/pfps/icon_err.png"
    except:
        traceback.print_exc()
        return "https://showierdata9978.github.io/Revower/pfps/icon_err.png"


def ban_user(meower_username: str):
    return DATABASE.users.update_one({"meower_username": meower_username.strip()}, {"$set": {"banned": True}}).modified_count > 0


def handle_raw(packet, *args, **kwargs):
    if type(packet) == str:
        return

    if "payload" not in packet['val']:
        return

    if not packet['val']['mode'] == "chat_data":
        return

    if not packet['val']['payload']['chatid'] in LINKING_CHATS:
        MEOWER.send_msg(
            "This chat is not being linked", to=packet['val']['payload']['chatid'])

        return

    chat = LINKING_CHATS[packet['val']['payload']['chatid']]

    chat['info'] = packet['val']['payload']

    if chat['info']['owner'] != chat['user'] and not BYPASS_CHAT_LINKING:
        MEOWER.send_msg(
            "Whoops! Looks like you don't have permission to link this group chat :(", to=packet['val']['payload']['chatid'])
        return

    # link the chats
    DATABASE.chats.insert_one(
        {"meower_chat": chat["meower_chat"], "revolt_chat": str(chat['revolt_chat'].id)})

    MEOWER.send_msg(
        "Linked the chats", to=chat['meower_chat'])

    loop.create_task(chat['revolt_chat'].send(
        content=f"Successfully linked with {chat['meower_chat']}"))

@MEOWER.command()
def account(ctx, revolt_user):
    if revolt_user not in LINKING_USERS:
        message.ctx.reply("You are not linking a Revolt account to your Meower account")
        return

    # check if the revolt user is the same as the one that started the linking
    if LINKING_USERS[revolt_user]['meower_username'] != ctx.user.username:
        message.ctx.reply("You are not linking your Revolt account to your Meower account")
        return

    # insert the user into the database
    DATABASE.users.insert_one(
        {"meower_username": ctx.user.username, "revolt_user": revolt_user, "pfp": get_user_pfp(LINKING_USERS[revolt_user]['meower_username'])})
    
    ctx.reply("Successfully linked your Revolt account to your Meower account!")

    # remove the user from the linking users
    LINKING_USERS.pop(revolt_user)

@MEOWER.command()
def link(ctx, revolt_chat: str):
    chat_id = ctx.message.chat

    if chat_id not in LINKING_CHATS:
        message.ctx.reply("You are not linking a Revolt channel")
        return

    # check if the revolt user is the same as the one that started the linking, and has permission to link the channel (ie. owns the chat)
    chat_linking = LINKING_CHATS[chat_id]
    if chat_linking['meower_chat'] != ctx.message.chat:
        message.ctx.reply(
            "Please run that command in the group chat you're trying to link!")
        return

    LINKING_CHATS[chat_id]['user'] = ctx.user.username
    MEOWER.wss.sendPacket({
        "cmd": "direct",
        "val": {
            "cmd": "get_chat_data",
            "val": f"{ctx.message.chat}"
        }
    })
    


def on_message_meower(message: Post, bot=MEOWER):
    if message.user.username == MEOWER_USERNAME:
        return

    try:
        if str(message).startswith(MEOWER.prefix):
            message.data = message.data[len(MEOWER.prefix):]
            MEOWER.run_command(message)
            return
    except Exception as e:
        traceback.print_exc()

    # check if a message is in a known revolt channel
    chats = DATABASE.chats.find({"meower_chat": message.chat})

    if chats is None:
        return

    pfp = get_user_pfp(message.user.username)  # type: ignore

    # send the message to the revolt channel
    for chat in chats:
        asyncio.run_coroutine_threadsafe(send_revolt_message(
            message, chat["revolt_chat"], pfp), loop)

async def send_to_chat(chat: str, post: Message, pfp: str):
    channel = await revolt.fetch_channel(chat)
    if type(channel) is not TextChannel:
        return

    user = DATABASE.users.find_one({"revolt_user": post.author.id})
    if user is None:
        return

    await channel.send(content=str(post.content), masquerade=Masquerade(name=user["meower_username"], avatar=f"{pfp}"))


class RevoltCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        super().__init__()

    def cog_load(self):
        print(f"Loaded {self.__class__.__name__}")

    @commands.command()
    async def ban(self, ctx: commands.Context, meower_username: str):
        role = ctx.server.get_role("01GRR3PQES9SMJFNQSMFZNCDAH")

        if role not in ctx.author.roles:
            await ctx.send(f"{ctx.author.mention} You don't have permission to ban users from Revower!")
            return

        if ban_user(meower_username):
            await ctx.send(f"{ctx.author.mention} User banned")
        else:
            await ctx.send(f"{ctx.author.mention} User not found")

    @commands.command()
    async def account(self, ctx: commands.Context, muser: str):
        #get the revolt account in the db, to check if the user is banned
        user = DATABASE.users.find_one({"revolt_user": ctx.message.author.id})
        if user is None:
            user = DATABASE.users.find_one({"meower_username": muser})

        if user is not None and user.get("banned", False):
            await ctx.send(content="Whoops! Looks like you're banned from Revower :(")
            return
        


        LINKING_USERS[ctx.message.author.id] = {
            "revolt_user": ctx.message.author.id,
            "meower_username": muser,
            "revolt_chat": ctx.message.channel.id
        }

        await ctx.send(content=f"Please send `@{MEOWER_USERNAME} account {ctx.message.author.id}` to either Meower Home or Livechat")
        return

    @commands.command()
    async def link(self, ctx: commands.Context, chat: str):
        DEFAULT_CHATS = [
            "livechat",
            "home"
        ]

        if chat not in DEFAULT_CHATS:
            LINKING_CHATS[chat] = {
                "meower_chat": chat,
                "revolt_chat": ctx.channel,
                "chat_info": None
            }
            await ctx.send(content=f"Please send `@{MEOWER_USERNAME} link {ctx.channel.id}` to the specified group chat")
            return

        # add the chat to the database
        DATABASE.chats.insert_one(
            {"meower_chat": chat, "revolt_chat": ctx.message.channel.id})
        await ctx.send(content=f"Successfully linked this channel to {chat}!")
        return
    

class RevoltClient(commands.CommandsClient):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.add_cog(RevoltCog(bot=self))
    
    async def on_ready(self):
        print("Revolt bot is ready")

    async def get_prefix(self, message):
        return f"{self.user.mention}"

   

    async def on_message(self, message: Message):
        await self.process_commands(message)
        # check if the message is sent by a bot
        if message.author.bot:
            return

        # check if the user has linked a meower account
        user = DATABASE.users.find_one({"revolt_user": message.author.id})
        musr: dict = user

        # check if the message is a command
        if str(message.content).startswith(revolt.user.mention):
            return


        if message.content.startswith("!!"): return

        # check if the message is in a known revolt channel
        db_chat = DATABASE.chats.find_one({"revolt_chat": message.channel.id})

        if db_chat is None:
            return

        if user is None:
            try:
                await message.add_reaction(":x:")
            except revolt_pkg.errors.HTTPError:
                print("Failed to add '❌' reaction")
            return

        if user.get("banned", False):
            try:
                await message.add_reaction(":x:")
            except revolt_pkg.errors.HTTPError:
                print("Failed to add '❌' reaction")
            return

        # send the message to the meower channel
        content = f"{str(message.content)}"
        if musr is None:
            return

        for message_id in message.reply_ids:
            try:
                reply = revolt.get_message(message_id)
            except:
                content = f"@<Unknown User> {content}"
                continue

            reply_user = reply.author
            if reply_user.id == revolt.user.id:
                # add the reply username to the message
                content = f"@{user.name} {content}"
                continue

            user_D = DATABASE.users.find_one({"revolt_user": reply_user.id})
            if user_D is None:
                # replace the ping with <Unknown User>
                content = f"@<Unknown User> {content}"
                continue

            content = f"@{user_D['meower_username']} {content}"

        for author in message.mentions:

            if author.id == revolt.user.id:
                # add the reply username to the message
                content = content.replace(f"@{author.mention}", f"@{author.name}")
                continue

            user_D = DATABASE.users.find_one({"revolt_user": author.id})
            if user_D is None:
                # replace the ping with <Unknown User>
                content = content.replace(f"{author.mention}", "<Unknown User>")
                continue

            content = content.replace(
                f"{author.mention}", f"@{user_D['meower_username']}")

        #get all of the attachmen
        for attachment in message.attachments:
            #shorten the url with go.meower.org
            async with aiohttp.request("POST", "https://go.meower.org/submit", json={"link": attachment.url}, headers={"Authorization": LINK_SHORTENER_KEY}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    content = f"[{attachment.filename}: {data['full_url']}] {content}"
                else:
                    content = f"[{attachment.filename}: {attachment.url}] {content}"

        emojis = re.findall(REVOLT_EMOJI, message.content)
        emoji: str = ""
        for emoji in emojis:
            emoji = emoji.replace(":", "")
            try:
                em = message.server.get_emoji(emoji)

            except:
                #try to convert the external emoji to a useable one.
                em = await revolt.fetch_emoji(emoji_id=emoji)
                if em is None:
                    continue

                em = revolt_pkg.Emoji(em, state=revolt.state)
            
            if em.nsfw: #no nsfw stuff
                try:
                    await message.add_reaction(":x:")
                except revolt_pkg.errors.HTTPError:
                    print("Failed to add '❌' reaction")
                return

            async with aiohttp.request("POST", "https://go.meower.org/submit", json={"link": f"https://autumn.revolt.chat/emojis/{em.id}"}, headers={"Authorization": LINK_SHORTENER_KEY}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    content = content.replace(f":{em.id}:", f"[{em.name}: {data['full_url']}]")
                else:
                    content.replace(f":{em.id}:", f"[{em.name}: {f'https://autumn.revolt.chat/emojis/{em.id}'}]")

        content = f"{user['meower_username']}: " + content

        MEOWER.send_msg(
            content, db_chat["meower_chat"])

        try:
            await message.add_reaction("✅")
        except revolt_pkg.errors.HTTPError:
            print("Failed to add '✅' reaction")

        chats = DATABASE.chats.find({"meower_chat": db_chat["meower_chat"]}) or []
        chats = list(chats)

        try:
            chats.remove(db_chat)  # remove the original chat from the list
        except ValueError:
            print("This somehow happened, this is a major red flag")

        # remove the original chat from the list

        tasks = []
        # type: ignore
        pfp = get_user_pfp(user["meower_username"])# type: ignore

        for chat in chats:  # type: ignore
            tasks.append(send_to_chat(chat["revolt_chat"], message, pfp))

        await asyncio.gather(*tasks)


MEOWER.callback(on_message_meower, "message")
MEOWER.callback(handle_raw, "__raw__")

def meower_main():
    while True:
        try:
            MEOWER.run(MEOWER_USERNAME, MEOWER_PASSWORD)
        except: pass
        


async def main():
    global revolt
    global loop
    loop = asyncio.get_event_loop()

    async with revolt_pkg.utils.client_session() as session:
        revolt = RevoltClient(session, REVOLT_TOKEN)
        threading.Thread(target=meower_main).start()
        
        while True:
          await revolt.start()
          sleep(15)
          client.user.edit(status="Bridging Meower with Revolt!")
          sleep(295)
          client.user.edit(status="Restarting...")


asyncio.run(main())
