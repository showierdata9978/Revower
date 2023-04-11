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
logging.basicConfig(level=logging.DEBUG)

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
        await chat.send(content=str(message), masquerade=Masquerade(name=message.user.username, avatar=await pfp_uri(message.user.pfp)))
    except revolt_pkg.errors.HTTPError:
        pass

@cached(PFPEXISTS)
async def pfp_uri(pfp: str) -> str:
    # https://showierdata9978.github.io/Revower/pfps/{user['pfp']}
    if type(pfp) == str:
        pfp = pfp.strip() #type: ignore
    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://showierdata9978.github.io/Revower/pfps/icon_{pfp}.png") as resp:
            if resp.status == 200:
                return f"https://showierdata9978.github.io/Revower/pfps/icon_{pfp}.png"
            else:
                return "https://showierdata9978.github.io/Revower/pfps/icon_err.png"

User_pfp = TTLCache(maxsize=1000, ttl=60*60*24*7)  # cache for 7 days


@cached(User_pfp)
async def get_user_pfp(username: str) -> str:
    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://api.meower.org/users/{username.strip()}") as resp:
            try:
                user = await resp.json()
                return user['pfp_data']
            except Exception as e:
                print(e)
                return "err"

# sync version of the 2 functions above


@cached_sync(PFPEXISTS)
def get_user_pfp_sync(username: str) -> str:
    resp = requests.get(f"https://api.meower.org/users/{username.strip()}")
    try:
        user = resp.json()
        return user['pfp_data']
    except Exception as e:
        print(e)
        return "err"


@cached_sync(User_pfp)
def pfp_uri_sync(pfp: str) -> str:
    if type(pfp) == str:
        pfp = pfp.strip() #type: ignore
    resp = requests.get(
        f"https://showierdata9978.github.io/Revower/pfps/icon_{pfp}.png")
    if resp.status_code == 200:
        return f"https://showierdata9978.github.io/Revower/pfps/icon_{pfp}.png"
    else:
        return "https://showierdata9978.github.io/Revower/pfps/icon_err.png"

def ban_user(meower_username: str):
    return DATABASE.users.update_one({"meower_username": meower_username}, {"$set": {"banned": True}}).modified_count > 0


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
            "You dont have perms to link this groupchat", to=packet['val']['payload']['chatid'])
        return

    # link the chats
    DATABASE.chats.insert_one(
        {"meower_chat": chat["meower_chat"], "revolt_chat": str(chat['revolt_chat'].id)})

    MEOWER.send_msg(
        "Linked the chats", to=chat['meower_chat'])

    loop.create_task(chat['revolt_chat'].send(
        content=f"Successfully linked with {chat['meower_chat']}"))


def on_message_meower(message: Post, bot=MEOWER):
    if message.user.username == MEOWER_USERNAME:
        return

    if str(message).startswith(f"@{MEOWER_USERNAME} "):
        # check if the user is linking a revolt account
        args = str(message).split(" ")[1:]
        cmd = args.pop(0)
        if cmd == "account":
            revolt_user = args.pop(0)

            if revolt_user not in LINKING_USERS:
                message.ctx.reply("You are not linking a revolt account")
                return

            # check if the revolt user is the same as the one that started the linking
            if LINKING_USERS[revolt_user]['meower_username'] != message.user.username:
                message.ctx.reply("You are not linking your revolt account")
                return

            # insert the user into the database
            DATABASE.users.insert_one(
                {"meower_username": message.user.username, "revolt_user": revolt_user, "pfp": get_user_pfp_sync(LINKING_USERS[revolt_user]['meower_username'])})
            message.ctx.reply("Successfully linked your revolt account")
            return
        elif cmd == "link":
            chat_id = message.chat

            if chat_id not in LINKING_CHATS:
                message.ctx.reply("You are not linking a revolt channel")
                return

            # check if the revolt user is the same as the one that started the linking, and has permission to link the channel (ie. owns the chat)
            chat_linking = LINKING_CHATS[chat_id]
            if chat_linking['meower_chat'] != message.chat:
                message.ctx.reply(
                    "Please run the command in the  gc you want to link")
                return

            LINKING_CHATS[chat_id]['user'] = message.user.username
            MEOWER.wss.sendPacket({
                "cmd": "direct",
                "val": {
                    "cmd": "get_chat_data",
                    "val": f"{message.chat}"
                }
            })
            return

    # check if a message is in a known revolt channel
    chats = DATABASE.chats.find({"meower_chat": message.chat})

    if chats is None:
        return

    pfp = pfp_uri_sync(get_user_pfp_sync(
        message.user.username))  # type: ignore

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


async def on_message(message: Message):
    # check if the message is sent by a bot
    if message.author.bot:
        return

    # check if the user has linked a meower account
    user = DATABASE.users.find_one({"revolt_user": message.author.id})
    musr: dict = user

    # check if the message is a command
    if str(message.content).startswith(revolt.user.mention):
        args = str(message.content).split(" ")
        args.pop(0)
        command = args.pop(0)

        if command == "account":
            #get the revolt account in the db, to check if the user is banned
            muser = args.pop(0)
            user = DATABASE.users.find_one({"revolt_user": message.author.id})
            if user is None:
                user = DATABASE.users.find_one({"meower_username": muser})

            if user is not None and user.get("banned", False):
                await message.channel.send(content="You are banned from using this bot")
                return
            


            LINKING_USERS[message.author.id] = {
                "revolt_user": message.author.id,
                "meower_username": muser,
                "revolt_chat": message.channel.id
            }

            await message.channel.send(content=f"Please send @{MEOWER_USERNAME} account {message.author.id} to livechat")
            return
        elif command == "link":
            DEFAULT_CHATS = [
                "livechat",
                "home"
            ]

            chat = args.pop(0)
            if chat not in DEFAULT_CHATS:
                LINKING_CHATS[chat] = {
                    "meower_chat": chat,
                    "revolt_chat": message.channel,
                    "chat_info": None
                }
                await message.channel.send(content=f"Please send @{MEOWER_USERNAME} link to the specified chat")
                return

            # add the chat to the database
            DATABASE.chats.insert_one(
                {"meower_chat": chat, "revolt_chat": message.channel.id})
            await message.channel.send(content=f"Successfully linked this channel to {chat}")
            return
        elif command == "ban":
            
            role = message.server.get_role("01GRR3PQES9SMJFNQSMFZNCDAH")
            
            if  role not in message.author.roles:
                await message.channel.send(content="You do not have permission to use this command")
                return
            
            user = args.pop(0)

            #optionally, add a reason
            reason = None
            if len(args) > 0:
                reason = " ".join(args)
            
            if ban_user(user):
                await message.channel.send(content=f"Successfully banned {user}")
                return
            
            await message.channel.send(content=f"Failed to ban {user}")

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
        except LookupError:
            reply = await message.channel.fetch_message(message_id)

        user = reply.author
        if user.id == revolt.user.id:
            # add the reply username to the message
            content = f"@{user.name} {content}"
            continue

        user_D = DATABASE.users.find_one({"revolt_user": user.id})
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
    pfp = await pfp_uri(await get_user_pfp(message.author.name)) # type: ignore

    for chat in chats:  # type: ignore
        tasks.append(send_to_chat(chat["revolt_chat"], message, pfp))

    await asyncio.gather(*tasks)


async def on_revolt_ready():
    print("Revolt bot is ready")


class RevoltClient(Client):
    async def on_ready(self):
        await on_revolt_ready()

    async def on_message(self, message: Message):
        try:
            await on_message(message)

        except Exception as e:
            import traceback
            traceback.print_exc()



MEOWER.callback(on_message_meower, "message")
MEOWER.callback(handle_raw, "__raw__")

def meower_main():
    while True:
        MEOWER.run(MEOWER_USERNAME, MEOWER_PASSWORD)
        


async def main():
    global revolt
    global loop
    loop = asyncio.get_event_loop()

    async with revolt_pkg.utils.client_session() as session:  # type: ignore
        revolt = RevoltClient(session, REVOLT_TOKEN)
        threading.Thread(target=meower_main).start()
        
        while True:
            await revolt.start()


asyncio.run(main())
