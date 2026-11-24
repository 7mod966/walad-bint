"""ولد بنت حيوان جماد — authoritative realtime game server.

One process holds every room in memory. Clients talk over a single WebSocket;
the server owns all timers and rules and pushes each room's state only to that
room's players. Run:  python server.py   (PORT env var, default 8080)
"""

import asyncio
import json
import logging
import os
import random
import secrets
import time
import unicodedata
from collections import deque
from pathlib import Path

from aiohttp import WSMsgType, web

import judge as ai

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("game")

STATIC = Path(__file__).parent / "public"

LETTERS = list("أبتثجحخدذرزسشصضطظعغفقكلمنهوي")
BASE_CATEGORIES = [
    {"key": "boy", "label": "ولد", "emoji": "👦"},
    {"key": "girl", "label": "بنت", "emoji": "👧"},
    {"key": "animal", "label": "حيوان", "emoji": "🐫"},
    {"key": "thing", "label": "جماد", "emoji": "🪑"},
    {"key": "food", "label": "طعام", "emoji": "🍽️"},
    {"key": "country", "label": "بلاد", "emoji": "🗺️"},
]

MAX_PLAYERS = 12
NAME_MAX = 20
ANSWER_MAX = 40
CHAT_MAX = 300
CHAT_HISTORY = 100
PICK_SECONDS = 30
COUNTDOWN_SECONDS = 3
SUBMIT_GRACE = 1.5          # wait for last autosaves after the timer
APPEAL_SECONDS = 25
HOST_HANDOFF_SECONDS = 20
DROP_GRACE = 10             # seconds a dropped player still counts (page refresh, signal blip)
ROOM_IDLE_TTL = 15 * 60
POINTS_UNIQUE = 10
POINTS_SHARED = 5

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


# ---------------------------------------------------------------- arabic utils

_ALEF = {"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا"}


def normalize(word):
    """Fold spelling variants so equal words compare equal."""
    w = "".join(c for c in unicodedata.normalize("NFKC", word) if not unicodedata.category(c).startswith("M"))
    w = w.replace("ـ", "").strip()
    w = "".join(_ALEF.get(c, c) for c in w)
    w = w.replace("ة", "ه").replace("ى", "ي").replace("ؤ", "و").replace("ئ", "ي")
    w = " ".join(w.split())
    if w.startswith("ال") and len(w) > 3:
        w = w[2:]
    return w


def first_letter_ok(answer, letter):
    n = normalize(answer)
    return bool(n) and n[0] == normalize(letter)[0]


def clean_text(s, limit):
    s = "".join(ch for ch in str(s) if ch.isprintable())
    return " ".join(s.split())[:limit]


# ---------------------------------------------------------------- model

class Player:
    def __init__(self, name, order):
        self.id = secrets.token_hex(4)
        self.token = secrets.token_urlsafe(18)
        self.name = name
        self.order = order
        self.score = 0
        self.round_points = 0
        self.ws = None
        self.disconnected_at = None
        self.chat_times = deque(maxlen=5)

    @property
    def online(self):
        return self.ws is not None

    @property
    def present(self):
        """Online, or dropped so recently (refresh, network blip) that we still wait for them."""
        return self.ws is not None or (
            self.disconnected_at is not None and time.time() - self.disconnected_at < DROP_GRACE)


class Room:
    def __init__(self, code):
        self.code = code
        self.players = {}        # id -> Player, insertion = seat order
        self.host_id = None
        self.banned = set()      # tokens of kicked players
        self.phase = "lobby"
        self.settings = {"roundSeconds": 90, "totalRounds": 6}
        self.round_no = 0
        self.used_letters = []
        self.turn_idx = -1
        self.picker_id = None
        self.letter = None
        self.categories = list(BASE_CATEGORIES)
        self.answers = {}        # pid -> {cat: text}
        self.submitted = set()
        self.verdicts = {}       # "pid:cat" -> {"valid": bool|None, "reason": str, "by": "ai"|"rule"|"vote"}
        self.points = {}         # "pid:cat" -> int
        self.appeals = {}        # aid -> dict
        self.ready = set()
        self.judge_error = False
        self.deadline = None     # loop.time() when the current timed phase ends
        self.chat = deque(maxlen=CHAT_HISTORY)
        self.timer_task = None
        self.host_task = None
        self.cleanup_task = None
        self.pending_broadcast = False
        self.version = 0

    # ---- helpers
    def online_players(self):
        return [p for p in self.players.values() if p.online]

    def present_players(self):
        return [p for p in self.players.values() if p.present]

    def seat_list(self):
        return list(self.players.values())

    def standings(self):
        return sorted(self.players.values(), key=lambda p: (-p.score, p.order))


rooms = {}


def new_code():
    while True:
        code = "".join(random.choice(CODE_ALPHABET) for _ in range(5))
        if code not in rooms:
            return code


def loop_now():
    return asyncio.get_running_loop().time()


# ---------------------------------------------------------------- broadcasting

def schedule_broadcast(room):
    """Coalesce many changes in one tick into a single push per player."""
    room.version += 1
    if room.pending_broadcast:
        return
    room.pending_broadcast = True
    asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(_flush(room)))


async def _flush(room):
    room.pending_broadcast = False
    for p in room.online_players():
        await send(p, {"t": "state", "s": view_for(room, p)})


async def send(player, msg):
    ws = player.ws
    if ws is None or ws.closed:
        return
    try:
        await ws.send_str(json.dumps(msg, ensure_ascii=False))
    except Exception:  # connection is going away; disconnect handler cleans up
        pass


async def send_room(room, msg):
    data = json.dumps(msg, ensure_ascii=False)
    for p in room.online_players():
        try:
            await p.ws.send_str(data)
        except Exception:
            pass


def view_for(room, me):
    remaining = None
    if room.deadline is not None:
        remaining = max(0, int((room.deadline - loop_now()) * 1000))
    show_answers = room.phase in ("review", "final")
    players = []
    for p in room.seat_list():
        players.append({
            "id": p.id, "name": p.name, "score": p.score, "online": p.online,
            "host": p.id == room.host_id,
            "submitted": p.id in room.submitted,
            "ready": p.id in room.ready,
        })
    s = {
        "v": room.version,
        "code": room.code,
        "me": me.id,
        "phase": room.phase,
        "hostId": room.host_id,
        "settings": room.settings,
        "round": room.round_no,
        "usedLetters": room.used_letters,
        "pickerId": room.picker_id,
        "letter": room.letter,
        "categories": room.categories,
        "players": players,
        "remainingMs": remaining,
        "judgeError": room.judge_error,
    }
    if room.phase == "letter" and room.picker_id == me.id:
        top3 = [p.id for p in room.standings()[:3]]
        s["canAddCategory"] = room.round_no > 1 and me.id in top3
    if room.phase == "playing":
        s["myAnswers"] = room.answers.get(me.id, {})
    if show_answers:
        s["answers"] = room.answers
        s["verdicts"] = room.verdicts
        s["points"] = room.points
        s["appeals"] = list(room.appeals.values())
    return s


def system_chat(room, text):
    msg = {"id": secrets.token_hex(4), "sys": True, "text": text, "ts": int(time.time() * 1000)}
    room.chat.append(msg)
    asyncio.ensure_future(send_room(room, {"t": "chat", "m": msg}))


# ---------------------------------------------------------------- timers

def set_timer(room, seconds, callback):
    cancel_timer(room)
    room.deadline = loop_now() + seconds

    async def run():
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        room.timer_task = None
        await callback()

    room.timer_task = asyncio.ensure_future(run())


def cancel_timer(room):
    if room.timer_task and not room.timer_task.done():
        room.timer_task.cancel()
    room.timer_task = None
    room.deadline = None


# ---------------------------------------------------------------- phases

def next_picker(room):
    seats = room.seat_list()
    if not seats:
        return None
    for step in range(1, len(seats) + 1):
        idx = (room.turn_idx + step) % len(seats)
        if seats[idx].present:
            room.turn_idx = idx
            return seats[idx].id
    return None


def start_letter_phase(room):
    room.round_no += 1
    room.phase = "letter"
    room.letter = None
    room.categories = list(BASE_CATEGORIES)
    room.answers, room.submitted = {}, set()
    room.verdicts, room.points, room.appeals = {}, {}, {}
    room.ready = set()
    room.judge_error = False
    room.picker_id = next_picker(room)
    set_timer(room, PICK_SECONDS, lambda: auto_pick(room))
    schedule_broadcast(room)


async def auto_pick(room):
    if room.phase != "letter":
        return
    free = [l for l in LETTERS if l not in room.used_letters] or LETTERS
    begin_round(room, random.choice(free), None)
    system_chat(room, "انتهى وقت الاختيار — تم اختيار حرف عشوائي")


def begin_round(room, letter, extra):
    room.letter = letter
    room.used_letters.append(letter)
    if extra:
        room.categories = BASE_CATEGORIES + [{"key": "extra", "label": extra, "emoji": "⭐"}]
    room.phase = "countdown"
    set_timer(room, COUNTDOWN_SECONDS, lambda: start_playing(room))
    schedule_broadcast(room)


async def start_playing(room):
    if room.phase != "countdown":
        return
    room.phase = "playing"
    set_timer(room, room.settings["roundSeconds"], lambda: end_playing(room, grace=True))
    schedule_broadcast(room)


async def end_playing(room, grace=False):
    if room.phase != "playing":
        return
    if grace:
        # Timer ran out: clients auto-submit; give their last saves a moment to land.
        room.phase = "collecting"
        room.deadline = None
        schedule_broadcast(room)
        await asyncio.sleep(SUBMIT_GRACE)
        if room.phase != "collecting":
            return
    cancel_timer(room)
    room.phase = "judging"
    schedule_broadcast(room)
    await run_judging(room)


def maybe_all_submitted(room):
    needed = [p.id for p in room.present_players()]
    if room.phase == "playing" and needed and all(pid in room.submitted for pid in needed):
        asyncio.ensure_future(end_playing(room))


async def run_judging(room):
    letter = room.letter
    meta = {c["key"]: {"label": c["label"], "rule": ai.CATEGORY_RULES.get(c["key"], f"أي شيء ينتمي إلى تصنيف: {c['label']}")}
            for c in room.categories}
    to_judge = []
    for p in room.seat_list():
        mine = room.answers.get(p.id, {})
        for c in room.categories:
            key = f"{p.id}:{c['key']}"
            text = mine.get(c["key"], "").strip()
            if not text:
                room.verdicts[key] = {"valid": False, "reason": "لا توجد إجابة", "by": "rule"}
            elif not first_letter_ok(text, letter):
                room.verdicts[key] = {"valid": False, "reason": f"لا تبدأ بحرف {letter}", "by": "rule"}
            else:
                to_judge.append({"id": key, "category": c["key"], "answer": text})

    results = {}
    if to_judge:
        try:
            results = await ai.judge(letter, to_judge, meta)
        except Exception as e:
            log.warning("AI judging failed in room %s: %r", room.code, e)
            room.judge_error = True
        if room.phase != "judging":   # room was reset while waiting
            return

    for item in to_judge:
        key = item["id"]
        if key in results:
            valid, reason = results[key]
            room.verdicts[key] = {"valid": valid, "reason": reason, "by": "ai"}
        else:
            room.verdicts[key] = {"valid": None, "reason": "لم يتمكن الحكم من الرد — صوّتوا", "by": "pending"}
            open_appeal(room, key, opener=None)

    recompute_points(room)
    room.phase = "review"
    room.deadline = None
    schedule_broadcast(room)
    check_review_done(room)


def recompute_points(room):
    """Valid & unique = 10, valid & shared = 5, otherwise 0. Totals rebuilt from scratch."""
    room.points = {}
    for c in room.categories:
        groups = {}
        for p in room.seat_list():
            key = f"{p.id}:{c['key']}"
            v = room.verdicts.get(key)
            if v and v["valid"]:
                groups.setdefault(normalize(room.answers[p.id][c["key"]]), []).append(key)
        for keys in groups.values():
            for key in keys:
                room.points[key] = POINTS_UNIQUE if len(keys) == 1 else POINTS_SHARED
    for p in room.seat_list():
        p.round_points = sum(v for k, v in room.points.items() if k.startswith(p.id + ":"))


def eligible_voters(room, appeal):
    owner = appeal["key"].split(":")[0]
    return [p.id for p in room.present_players() if p.id != owner]


def open_appeal(room, key, opener):
    if any(a["key"] == key and a["status"] == "open" for a in room.appeals.values()):
        return None
    if any(a["key"] == key for a in room.appeals.values() if a["opener"] is not None) and opener is not None:
        return None  # one human appeal per answer
    aid = secrets.token_hex(4)
    cur = room.verdicts[key]["valid"]
    appeal = {
        "id": aid, "key": key, "opener": opener,
        # The question voters answer: "is this answer correct?"
        "votes": {}, "status": "open",
        "was": cur,
    }
    room.appeals[aid] = appeal
    asyncio.ensure_future(_appeal_timeout(room, aid))
    return appeal


async def _appeal_timeout(room, aid):
    await asyncio.sleep(APPEAL_SECONDS)
    a = room.appeals.get(aid)
    if a and a["status"] == "open":
        resolve_appeal(room, a)
        schedule_broadcast(room)
        check_review_done(room)


def maybe_resolve_appeal(room, appeal):
    voters = eligible_voters(room, appeal)
    if not voters or all(v in appeal["votes"] for v in voters):
        resolve_appeal(room, appeal)


def resolve_appeal(room, appeal):
    if appeal["status"] != "open":
        return
    yes = sum(1 for v in appeal["votes"].values() if v)
    no = sum(1 for v in appeal["votes"].values() if not v)
    key = appeal["key"]
    v = room.verdicts[key]
    if yes > no:
        new_valid = True
    elif no > yes:
        new_valid = False
    else:
        new_valid = appeal["was"] if appeal["was"] is not None else False
    changed = new_valid != v["valid"]
    v["valid"] = new_valid
    if changed or v["by"] == "pending":
        v["by"] = "vote"
        v["reason"] = "قرار التصويت: " + ("مقبولة" if new_valid else "مرفوضة")
    appeal["status"] = "accepted" if new_valid else "rejected"
    recompute_points(room)


def check_review_done(room):
    if room.phase != "review":
        return
    if any(a["status"] == "open" for a in room.appeals.values()):
        return
    needed = [p.id for p in room.present_players()]
    if needed and all(pid in room.ready for pid in needed):
        finish_round(room)


def finish_round(room):
    for a in room.appeals.values():
        if a["status"] == "open":
            resolve_appeal(room, a)
    for p in room.seat_list():
        p.score += getattr(p, "round_points", 0)
        p.round_points = 0
    if room.round_no >= room.settings["totalRounds"]:
        room.phase = "final"
        cancel_timer(room)
        room.points = {}
        schedule_broadcast(room)
    else:
        start_letter_phase(room)


def reset_to_lobby(room):
    cancel_timer(room)
    room.phase = "lobby"
    room.round_no = 0
    room.used_letters = []
    room.turn_idx = -1
    room.picker_id = None
    room.letter = None
    room.categories = list(BASE_CATEGORIES)
    room.answers, room.submitted, room.verdicts, room.points, room.appeals = {}, set(), {}, {}, {}
    room.ready = set()
    for p in room.players.values():
        p.score = 0
        p.round_points = 0
    schedule_broadcast(room)


# ---------------------------------------------------------------- membership

def unique_name(room, name, exclude=None):
    taken = {p.name for p in room.players.values() if p.id != exclude}
    if name not in taken:
        return name
    i = 2
    while f"{name} {i}" in taken:
        i += 1
    return f"{name} {i}"


def remove_player(room, pid, reason_text):
    p = room.players.pop(pid, None)
    if not p:
        return
    # Keep the letter-turn pointer on the same next player.
    idx = sum(1 for q in room.players.values() if q.order < p.order)
    if idx <= room.turn_idx:
        room.turn_idx -= 1
    room.answers.pop(pid, None)
    room.submitted.discard(pid)
    room.ready.discard(pid)
    for k in [k for k in room.verdicts if k.startswith(pid + ":")]:
        room.verdicts.pop(k, None)
    for aid in [a for a, ap in room.appeals.items() if ap["key"].startswith(pid + ":")]:
        room.appeals.pop(aid, None)
    for a in room.appeals.values():
        a["votes"].pop(pid, None)
    if room.phase in ("review",):
        recompute_points(room)
    system_chat(room, reason_text)
    if room.host_id == pid:
        transfer_host(room)
    after_membership_change(room)


def transfer_host(room):
    candidates = room.online_players() or room.seat_list()
    old = room.host_id
    room.host_id = candidates[0].id if candidates else None
    if room.host_id and room.host_id != old:
        system_chat(room, f"أصبح {room.players[room.host_id].name} المضيف")


def after_membership_change(room):
    """Re-check every 'waiting for everyone' condition after someone joins/leaves/drops."""
    online = room.online_players()
    if room.phase not in ("lobby", "final") and len(room.players) < 2:
        system_chat(room, "لا يوجد عدد كافٍ من اللاعبين — العودة لغرفة الانتظار")
        reset_to_lobby(room)
        return
    if room.phase == "letter" and (room.picker_id not in room.players or not room.players[room.picker_id].present):
        room.picker_id = next_picker(room)
        set_timer(room, PICK_SECONDS, lambda: auto_pick(room))
    if room.phase == "playing":
        maybe_all_submitted(room)
    if room.phase == "review":
        for a in list(room.appeals.values()):
            if a["status"] == "open":
                maybe_resolve_appeal(room, a)
        check_review_done(room)
    if not online:
        schedule_cleanup(room)
    schedule_broadcast(room)


def schedule_cleanup(room):
    if room.cleanup_task and not room.cleanup_task.done():
        return

    async def run():
        await asyncio.sleep(ROOM_IDLE_TTL)
        if not room.online_players():
            cancel_timer(room)
            rooms.pop(room.code, None)
            log.info("room %s removed (idle)", room.code)

    room.cleanup_task = asyncio.ensure_future(run())


def schedule_host_handoff(room, pid):
    if room.host_task and not room.host_task.done():
        room.host_task.cancel()

    async def run():
        await asyncio.sleep(HOST_HANDOFF_SECONDS)
        p = room.players.get(pid)
        if room.host_id == pid and (p is None or not p.online) and room.online_players():
            transfer_host(room)
            schedule_broadcast(room)

    room.host_task = asyncio.ensure_future(run())


# ---------------------------------------------------------------- message handlers

class GameError(Exception):
    pass


def require(cond, msg):
    if not cond:
        raise GameError(msg)


def handle(room, me, msg):
    t = msg.get("t")
    is_host = me.id == room.host_id

    if t == "settings":
        require(is_host and room.phase == "lobby", "فقط المضيف يغيّر الإعدادات في غرفة الانتظار")
        secs = int(msg.get("roundSeconds", room.settings["roundSeconds"]))
        rounds = int(msg.get("totalRounds", room.settings["totalRounds"]))
        room.settings = {"roundSeconds": max(30, min(300, secs)), "totalRounds": max(1, min(28, rounds))}
        schedule_broadcast(room)

    elif t == "start":
        require(is_host, "فقط المضيف يبدأ اللعبة")
        require(room.phase == "lobby", "اللعبة بدأت بالفعل")
        require(len(room.online_players()) >= 2, "تحتاج لاعبَين على الأقل")
        room.used_letters = []
        for p in room.players.values():
            p.score = 0
        system_chat(room, "بدأت اللعبة! 🎲")
        start_letter_phase(room)

    elif t == "pick":
        require(room.phase == "letter", "ليس وقت اختيار الحرف")
        require(room.picker_id == me.id, "ليس دورك لاختيار الحرف")
        letter = msg.get("letter")
        require(letter in LETTERS, "حرف غير صالح")
        require(letter not in room.used_letters, "هذا الحرف استُخدم من قبل")
        extra = clean_text(msg.get("extra") or "", 20)
        top3 = [p.id for p in room.standings()[:3]]
        if not (room.round_no > 1 and me.id in top3):
            extra = ""
        begin_round(room, letter, extra or None)

    elif t in ("draft", "submit"):
        require(room.phase in ("playing", "collecting"), "الجولة ليست جارية")
        if me.id in room.submitted:
            return
        answers = msg.get("answers") or {}
        keys = {c["key"] for c in room.categories}
        room.answers[me.id] = {k: clean_text(v, ANSWER_MAX) for k, v in answers.items() if k in keys and isinstance(v, str)}
        if t == "submit":
            room.submitted.add(me.id)
            schedule_broadcast(room)
            maybe_all_submitted(room)

    elif t == "appeal":
        require(room.phase == "review", "الاعتراض متاح فقط أثناء المراجعة")
        key = f"{msg.get('pid')}:{msg.get('cat')}"
        require(key in room.verdicts, "إجابة غير موجودة")
        require(room.answers.get(msg.get("pid"), {}).get(msg.get("cat"), "").strip(), "لا يمكن الاعتراض على خانة فارغة")
        a = open_appeal(room, key, opener=me.id)
        require(a is not None, "يوجد اعتراض على هذه الإجابة بالفعل")
        room.ready.clear()
        owner = room.players.get(key.split(":")[0])
        cat = next((c["label"] for c in room.categories if c["key"] == key.split(":")[1]), "")
        system_chat(room, f"اعتراض من {me.name} على إجابة {owner.name if owner else ''} في {cat}")
        maybe_resolve_appeal(room, a)
        schedule_broadcast(room)
        check_review_done(room)

    elif t == "vote":
        a = room.appeals.get(msg.get("aid"))
        require(room.phase == "review" and a and a["status"] == "open", "التصويت مغلق")
        require(me.id in eligible_voters(room, a), "لا يمكنك التصويت على إجابتك")
        a["votes"][me.id] = bool(msg.get("yes"))
        maybe_resolve_appeal(room, a)
        schedule_broadcast(room)
        check_review_done(room)

    elif t == "ready":
        require(room.phase == "review", "ليس وقت الاستعداد")
        room.ready.add(me.id)
        schedule_broadcast(room)
        check_review_done(room)

    elif t == "force":
        require(is_host, "فقط المضيف")
        if room.phase == "letter":
            asyncio.ensure_future(auto_pick(room))
        elif room.phase == "playing":
            asyncio.ensure_future(end_playing(room, grace=True))
        elif room.phase == "review":
            system_chat(room, "المضيف أنهى المراجعة")
            finish_round(room)

    elif t == "kick":
        require(is_host, "فقط المضيف يمكنه الطرد")
        pid = msg.get("pid")
        require(pid in room.players and pid != me.id, "لاعب غير موجود")
        victim = room.players[pid]
        room.banned.add(victim.token)
        ws = victim.ws
        victim.ws = None
        if ws is not None:
            asyncio.ensure_future(_kick_socket(ws))
        remove_player(room, pid, f"طرد المضيف {victim.name}")

    elif t == "again":
        require(is_host and room.phase == "final", "فقط المضيف")
        reset_to_lobby(room)

    elif t == "chat":
        text = clean_text(msg.get("text", ""), CHAT_MAX)
        if not text:
            return
        now = time.monotonic()
        if len(me.chat_times) == me.chat_times.maxlen and now - me.chat_times[0] < 5:
            raise GameError("أرسلت رسائل كثيرة بسرعة — انتظر قليلًا")
        me.chat_times.append(now)
        m = {"id": secrets.token_hex(4), "pid": me.id, "name": me.name, "text": text, "ts": int(time.time() * 1000)}
        room.chat.append(m)
        asyncio.ensure_future(send_room(room, {"t": "chat", "m": m}))

    elif t == "leave":
        ws = me.ws
        me.ws = None
        remove_player(room, me.id, f"غادر {me.name} الغرفة")
        if ws is not None:
            asyncio.ensure_future(ws.close())

    elif t == "ping":
        asyncio.ensure_future(send(me, {"t": "pong", "c": msg.get("c")}))

    else:
        raise GameError("طلب غير معروف")


async def _kick_socket(ws):
    try:
        await ws.send_str(json.dumps({"t": "kicked"}))
        await ws.close()
    except Exception:
        pass


# ---------------------------------------------------------------- websocket

async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=16 * 1024)
    await ws.prepare(request)
    room = me = None

    async def fail(text):
        await ws.send_str(json.dumps({"t": "fatal", "msg": text}, ensure_ascii=False))
        await ws.close()

    try:
        # First message must be create or join.
        first = await ws.receive(timeout=15)
        if first.type != WSMsgType.TEXT:
            await ws.close()
            return ws
        hello = json.loads(first.data)
        name = clean_text(hello.get("name", ""), NAME_MAX)

        if hello.get("t") == "create":
            if not name:
                await fail("اكتب اسمك أولًا")
                return ws
            room = Room(new_code())
            rooms[room.code] = room
            me = Player(name, 0)
            room.players[me.id] = me
            room.host_id = me.id
            log.info("room %s created", room.code)
        elif hello.get("t") == "join":
            room = rooms.get(str(hello.get("code", "")).upper().strip())
            if room is None:
                await fail("لا توجد غرفة بهذا الرمز")
                return ws
            token = hello.get("token")
            if token and token in room.banned:
                await fail("تم طردك من هذه الغرفة")
                return ws
            me = next((p for p in room.players.values() if token and secrets.compare_digest(p.token, token)), None)
            if me is None:
                if not name:
                    await fail("اكتب اسمك أولًا")
                    return ws
                if len(room.players) >= MAX_PLAYERS:
                    await fail("الغرفة ممتلئة")
                    return ws
                order = max((p.order for p in room.players.values()), default=-1) + 1
                me = Player(unique_name(room, name), order)
                room.players[me.id] = me
                system_chat(room, f"انضم {me.name} 👋")
            else:
                if me.ws is not None and not me.ws.closed:
                    old = me.ws
                    asyncio.ensure_future(_replace_socket(old))
                if me.disconnected_at and time.time() - me.disconnected_at > DROP_GRACE:
                    system_chat(room, f"عاد {me.name}")
        else:
            await fail("طلب غير صالح")
            return ws

        me.ws = ws
        me.disconnected_at = None
        if room.cleanup_task and not room.cleanup_task.done():
            room.cleanup_task.cancel()
        await ws.send_str(json.dumps({"t": "welcome", "code": room.code, "pid": me.id, "token": me.token,
                                      "chat": list(room.chat)}, ensure_ascii=False))
        after_membership_change(room)

        async for raw in ws:
            if raw.type != WSMsgType.TEXT:
                continue
            if me.ws is not ws:
                break  # replaced by a newer tab or kicked
            try:
                msg = json.loads(raw.data)
                if not isinstance(msg, dict):
                    continue
                handle(room, me, msg)
            except GameError as e:
                await ws.send_str(json.dumps({"t": "error", "msg": str(e)}, ensure_ascii=False))
            except (ValueError, TypeError) as e:
                await ws.send_str(json.dumps({"t": "error", "msg": "طلب غير صالح"}, ensure_ascii=False))
                log.debug("bad message: %r", e)
    except asyncio.TimeoutError:
        pass
    except Exception:
        log.exception("websocket error")
    finally:
        if me is not None and room is not None and me.ws is ws:
            me.ws = None
            me.disconnected_at = time.time()
            if me.id in room.players:
                if room.host_id == me.id:
                    schedule_host_handoff(room, me.id)
                after_membership_change(room)
                # Once the grace period is over, stop waiting for them if they haven't returned.
                gone_at = me.disconnected_at

                def recheck(p=me, r=room, at=gone_at):
                    if p.id in r.players and not p.online and p.disconnected_at == at:
                        after_membership_change(r)
                asyncio.get_running_loop().call_later(DROP_GRACE + 0.2, recheck)
    return ws


async def _replace_socket(ws):
    try:
        await ws.send_str(json.dumps({"t": "fatal", "msg": "تم فتح اللعبة في تبويب آخر"}, ensure_ascii=False))
        await ws.close()
    except Exception:
        pass


# ---------------------------------------------------------------- http

async def index(request):
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


async def health(request):
    return web.json_response({"ok": True, "rooms": len(rooms)})


def make_app():
    app = web.Application()
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/health", health)
    app.router.add_get("/", index)
    app.router.add_get("/room/{code}", index)
    app.router.add_static("/static/", STATIC)
    return app


if __name__ == "__main__":
    if not os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("WBHJ_FAKE_JUDGE") != "1":
        log.warning("ANTHROPIC_API_KEY is not set — AI judging will fail and rounds will fall back to voting.")
    web.run_app(make_app(), host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
