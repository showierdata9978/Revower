from __future__ import annotations
from dotenv import dotenv_values, load_dotenv

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
logging.basicConfig(level=logging.INFO)

load_dotenv()

MEOWER_USERNAME = dotenv_values()["meower_username"]
MEOWER_PASSWORD = dotenv_values()["meower_password"]
REVOLT_TOKEN = dotenv_values()["revolt_token"]
DATABASE = pymongo.MongoClient(dotenv_values().get(
    "mongo_url", "mongodb://localhost:27017"))["revolt-meower"]

# check if the environment variables are set
if not MEOWER_USERNAME or not MEOWER_PASSWORD or not REVOLT_TOKEN:
    raise ValueError("Environment variables not set")


MEOWER = Bot(autoreload=0)
LINKING_USERS = {}
LINKING_CHATS = {}
BYPASS_CHAT_LINKING = str(dotenv_values().get("bypass_chat_linking", False)) == "True"

async def send_revolt_message(message: Post, chat_id: str):
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
        await chat.send(content=str(message), masquerade=Masquerade(name=message.user.username, avatar=f"https://assets.meower.org/PFP/{message.user.pfp}.svg"))
    except revolt_pkg.errors.HTTPError:
        MEOWER.send_msg("Failed to send message to revolt channel")


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

    if chat['info']['owner'] == chat['user'] and not BYPASS_CHAT_LINKING:
        MEOWER.send_msg(
            "You dont have perms to link this groupchat", to=packet['val']['payload']['chatid'])
        return

    # link the chats
    print(type(chat['revolt_chat'].id))
    DATABASE.chats.insert_one(
        {"meower_chat": chat["meower_chat"], "revolt_chat": str(chat['revolt_chat'].id)})
    
    MEOWER.send_msg(
        "Linked the chats", to=chat['meower_chat'])
    
    loop.create_task(chat['revolt_chat'].send(content=f"Successfully linked with {chat['meower_chat']}"))


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
                {"meower_username": message.user.username, "revolt_user": revolt_user, "pfp": message.user.pfp})
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

    # send the message to the revolt channel
    for chat in chats:
        loop.create_task(send_revolt_message(message, chat["revolt_chat"]))


async def send_to_chat(chat: str, post: Message):
    channel = await revolt.fetch_channel(chat)
    if type(channel) is not TextChannel:
        return

    user = DATABASE.users.find_one({"revolt_user": post.author.id})
    if user is None:
        return

    await channel.send(content=str(post.content), masquerade=Masquerade(name=user["meower_username"], avatar=f"https://assets.meower.org/PFPS/{user['pfp']}"))


async def on_message(message: Message):
    # check if the message is sent by a bot
    if message.author.bot:
        return

    # check if the user has linked a meower account
    user = DATABASE.users.find_one({"revolt_user": message.author.id})

    # check if the message is a command
    if str(message.content).startswith(revolt.user.mention):
        args = str(message.content).split(" ")
        args.pop(0)
        command = args.pop(0)

        if command == "account":

            LINKING_USERS[message.author.id] = {
                "revolt_user": message.author.id,
                "meower_username": args.pop(0),
                "revolt_chat": message.channel.id
            }

            await message.channel.send(content=f"Please send @{MEOWER_USERNAME} link {message.author.id} to livechat")
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

    # check if the message is in a known revolt channel
    db_chat = DATABASE.chats.find_one({"revolt_chat": message.channel.id})

    if db_chat is None:
        return

    if user is None:
        await message.add_reaction("❌")
        return
    # send the message to the meower channel

    MEOWER.send_msg(
        f"{user['meower_username']}: {str(message.content)}", db_chat["meower_chat"])

    try:
        await message.add_reaction("✅")
    except revolt_pkg.errors.HTTPError:
        print("Failed to add reaction")

    chats = DATABASE.chats.find({"meower_chat": db_chat["meower_chat"]}) or []
    chats = list(chats)

    try:
        chats.remove(db_chat)  # remove the original chat from the list
    except ValueError:
        print("This somehow happened, this is a major red flag")

    # remove the original chat from the list

    tasks = []
    for chat in chats:  # type: ignore
        tasks.append(send_to_chat(chat["revolt_chat"], message))

    await asyncio.gather(*tasks)


async def on_revolt_ready():
    print("Revolt bot is ready")


class RevoltClient(Client):
    async def on_ready(self):
        await on_revolt_ready()

    async def on_message(self, message: Message):
        await on_message(message)


MEOWER.callback(on_message_meower, "message")
MEOWER.callback(handle_raw, "__raw__")

async def main():
    global revolt
    global loop
    loop = asyncio.get_event_loop()

    async with revolt_pkg.utils.client_session() as session:
        revolt = RevoltClient(session, REVOLT_TOKEN)
        threading.Thread(target=MEOWER.run, args=(
            MEOWER_USERNAME, MEOWER_PASSWORD)).start()
        await revolt.start()


asyncio.run(main())
