import os, re, json, aiohttp, discord
from discord.ext import commands
from datetime import datetime
from collections import defaultdict

TOKEN = os.environ["DISCORD_TOKEN"]
GROQ_KEY = os.environ["GROQ_API_KEY"]
OWNER_ID = int(os.environ["OWNER_ID"])
MODEL = os.environ.get("MODEL_NAME", "openai/gpt-oss-120b")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

SEP = "・"
MOD_ROLE = "مشرف"
ADMIN_ROLE = "إداري"
INTEREST_ROLES = {"قصص": "📖", "ألعاب": "🎮"}
OBSOLETE_ROLES = ["تقنية", "نيكس"]
IDENTITY_ROLES = {"كاتب": "📝", "مستمع": "🎧"}
PICK_ROLES = {**INTEREST_ROLES, **IDENTITY_ROLES}

STRUCTURE = [
    ("👋", "ترحيب", None, [
        ("📜", "القواعد", "ro"), ("📢", "الإعلانات", "ro"), ("🎭", "الرتب", "ro"),
        ("🙋", "عرف بنفسك", "text")]),
    ("💬", "عام", None, [
        ("💬", "الدردشة", "text"), ("😂", "ميمز وصور", "text"),
        ("💡", "اقتراحات السيرفر", "text"), ("🤖", "أوامر البوتات", "text")]),
    ("📖", "قصص", ["قصص"], [
        ("📺", "تحديثات الفيديوهات", "ro"), ("💬", "نقاش القصص", "text"),
        ("💡", "اقتراح قصة", "forum"), ("📝", "قصصكم", "text"),
        ("👻", "رعب وغموض", "text"), ("🔍", "جرائم حقيقية", "text"),
        ("🏺", "تاريخ وحضارات", "text"),
        ("🎬", "أفكار للقناة", "text")]),
    ("🎮", "ألعاب", ["ألعاب"], [
        ("💬", "دردشة الألعاب", "text"), ("🤝", "دور لاعبين", "text"),
        ("🐧", "ألعاب لينكس", "text"), ("🔥", "عروض وألعاب مجانية", "text"),
        ("🧩", "مودات وتعديلات", "text"), ("🏆", "لقطات وإنجازات", "text")]),
    ("🔊", "صوتي", None, [
        ("🔊", "غرفة عامة", "voice"), ("🎮", "غرفة ألعاب", "voice"),
        ("🎤", "غرفة قصص", "voice"), ("🤫", "غرفة هادئة", "voice")]),
    ("🔒", "إدارة", "mods", [
        ("🔒", "خاص بالمشرفين", "text"), ("🧾", "سجل البوت", "text")]),
]

def norm(s):
    return re.sub(r"[^\w]", "", s.lower())

def slug(s):
    return re.sub(r"[\s\-]+", SEP, s.strip().lower())

def full(emoji, label):
    return f"{emoji}{SEP}{slug(label)}"

def find_matches(guild, name):
    n = norm(name)
    return [c for c in guild.channels if n and norm(c.name) == n]

def find_channel(guild, name):
    m = find_matches(guild, name)
    return m[0] if m else None

def find_category(guild, name):
    n = norm(name)
    return next((c for c in guild.categories if n and norm(c.name) == n), None)

def category_overwrites(guild, access, roles, staff):
    if access is None:
        return {}
    ow = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    for s_ in staff:
        ow[s_] = discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True)
    if access != "mods":
        for r in access:
            ow[roles[r]] = discord.PermissionOverwrite(view_channel=True)
    return ow

def channel_overwrites(guild, base, kind, staff):
    ow = {t: discord.PermissionOverwrite.from_pair(*o.pair()) for t, o in base.items()}
    if kind == "ro":
        ow.setdefault(guild.default_role, discord.PermissionOverwrite()).send_messages = False
        ow.setdefault(guild.me, discord.PermissionOverwrite()).send_messages = True
        for s_ in staff:
            ow.setdefault(s_, discord.PermissionOverwrite()).send_messages = True
    return ow

async def make_channel(guild, cat, base_ow, staff, emoji, label, kind):
    name = full(emoji, label)
    ow = channel_overwrites(guild, base_ow, kind, staff)
    ex = next((c for c in guild.channels
               if c.category_id == cat.id and norm(c.name) == norm(label)), None)
    if ex:
        kw = {"overwrites": ow}
        if ex.name != name.lower():
            kw["name"] = name
        await ex.edit(**kw)
        return ex
    if kind == "voice":
        return await guild.create_voice_channel(name, category=cat, overwrites=ow)
    if kind == "forum":
        try:
            return await guild.create_forum(name, category=cat, overwrites=ow)
        except discord.HTTPException:
            pass
    return await guild.create_text_channel(name, category=cat, overwrites=ow)

async def ensure_role(guild, name, errors, **kw):
    r = discord.utils.get(guild.roles, name=name)
    if r:
        return r
    try:
        return await guild.create_role(name=name, **kw)
    except discord.HTTPException as e:
        errors.append(f"رتبة {name}: {e}")
        return None

async def build(guild, say):
    errors = []
    admin = await ensure_role(guild, ADMIN_ROLE, errors, colour=discord.Colour.orange(), hoist=True,
                              permissions=discord.Permissions(manage_messages=True, manage_channels=True))
    mod = await ensure_role(guild, MOD_ROLE, errors, colour=discord.Colour.red(), hoist=True,
                            permissions=discord.Permissions(manage_messages=True))
    staff = [r for r in (admin, mod) if r]
    roles = {r: await ensure_role(guild, r, errors, mentionable=True) for r in PICK_ROLES}

    for emoji, label, access, chans in STRUCTURE:
        cname = full(emoji, label)
        try:
            cat_ow = category_overwrites(guild, access, roles, staff)
            cat = find_category(guild, label)
            if cat:
                kw = {"overwrites": cat_ow}
                if cat.name != cname.lower():
                    kw["name"] = cname
                await cat.edit(**kw)
            else:
                cat = await guild.create_category(cname, overwrites=cat_ow)
            made = set()
            for e, l, kind in chans:
                ch = await make_channel(guild, cat, cat_ow, staff, e, l, kind)
                made.add(ch.id)
            for c in guild.channels:
                if c.category_id == cat.id and c.id not in made:
                    await c.edit(overwrites=channel_overwrites(guild, cat_ow, "text", staff))
            await say(f"تم: {cname}")
        except Exception as e:
            errors.append(f"{cname}: {e}")
    return errors

class RolesView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for n, e in PICK_ROLES.items():
            b = discord.ui.Button(label=n, emoji=e, custom_id=f"role:{n}",
                                  style=discord.ButtonStyle.secondary)
            b.callback = self.make_cb(n)
            self.add_item(b)

    def make_cb(self, n):
        async def cb(inter: discord.Interaction):
            role = discord.utils.get(inter.guild.roles, name=n)
            if role in inter.user.roles:
                await inter.user.remove_roles(role)
                msg = f"شلت رتبة {n}"
            else:
                await inter.user.add_roles(role)
                msg = f"أخذت رتبة {n}، تنفتح لك قنواتها"
            await inter.response.send_message(msg, ephemeral=True)
        return cb

async def post_roles(guild):
    ch = next((c for c in guild.text_channels if norm(c.name) == norm("الرتب")), None)
    if not ch:
        return
    try:
        await ch.purge(limit=20, check=lambda m: m.author == guild.me)
    except discord.HTTPException:
        pass
    await ch.send("اختر اهتماماتك وتنفتح لك قنواتها (قصص، ألعاب). "
                  "رتبتا كاتب ومستمع للتعريف والتنبيهات بس ما تفتح قنوات. "
                  "اضغط مرة ثانية على الزر عشان تشيل الرتبة:",
                  view=RolesView())

class Confirm(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=30)
        self.ok = False

    async def _check(self, inter):
        if inter.user.id != OWNER_ID:
            await inter.response.send_message("مو لك", ephemeral=True)
            return False
        await inter.response.defer()
        return True

    @discord.ui.button(label="تأكيد", style=discord.ButtonStyle.danger)
    async def yes(self, inter, b):
        if await self._check(inter):
            self.ok = True
            self.stop()

    @discord.ui.button(label="إلغاء", style=discord.ButtonStyle.secondary)
    async def no(self, inter, b):
        if await self._check(inter):
            self.stop()

async def confirm(msg, text):
    v = Confirm()
    await msg.channel.send(f"{text}\nتأكد خلال ٣٠ ثانية.", view=v)
    await v.wait()
    return v.ok

# ---------- الأدوات ----------
async def t_list_channels(g, m):
    out = []
    for cat in g.categories:
        out.append(f"[{cat.name}] " + ", ".join(c.name for c in cat.channels))
    loose = [c.name for c in g.channels if c.category is None and not isinstance(c, discord.CategoryChannel)]
    if loose:
        out.append("[بدون فئة] " + ", ".join(loose))
    return "\n".join(out)

async def t_create_category(g, m, name):
    await g.create_category(slug(name))
    return f"تم إنشاء الفئة {name}"

async def t_create_channel(g, m, name, kind="text", category=None):
    cat = find_category(g, category) if category else None
    if category and not cat:
        return f"ما لقيت الفئة {category}"
    if kind == "voice":
        await g.create_voice_channel(slug(name), category=cat)
    elif kind == "forum":
        try:
            await g.create_forum(slug(name), category=cat)
        except discord.HTTPException:
            pass
    else:
        await g.create_text_channel(slug(name), category=cat)
    return f"تم إنشاء {name}"

async def t_rename_channel(g, m, old, new):
    c = find_channel(g, old)
    if not c:
        return f"ما لقيت {old}"
    await c.edit(name=slug(new))
    return "تم"

async def t_move_channel(g, m, channel, category):
    c, cat = find_channel(g, channel), find_category(g, category)
    if not c or not cat:
        return "القناة أو الفئة مو موجودة"
    await c.edit(category=cat, sync_permissions=True)
    return "تم"

async def t_delete_channel(g, m, name):
    ms = find_matches(g, name)
    if len(ms) != 1:
        return f"الاسم غامض أو غير موجود ({len(ms)} نتيجة)"
    c = ms[0]
    kids = list(c.channels) if isinstance(c, discord.CategoryChannel) else []
    q = f"أحذف {c.name}" + (f" مع {len(kids)} قناة داخلها؟" if kids else "؟")
    if not await confirm(m, q):
        return "المستخدم رفض أو انتهى الوقت"
    for k in kids:
        await k.delete()
    await c.delete()
    return "تم الحذف"

async def t_create_role(g, m, name, color_hex="#99aab5"):
    await g.create_role(name=name, colour=discord.Colour(int(color_hex.lstrip("#"), 16)))
    return f"تم إنشاء رتبة {name}"

async def t_send_message(g, m, channel, content):
    c = next((x for x in find_matches(g, channel) if isinstance(x, discord.abc.Messageable)), None)
    if not c:
        return f"ما لقيت قناة نصية {channel}"
    await c.send(content[:2000])
    return "تم الإرسال"

async def t_set_slowmode(g, m, channel, seconds):
    c = find_channel(g, channel)
    if not isinstance(c, discord.TextChannel):
        return "قناة نصية غير موجودة"
    await c.edit(slowmode_delay=max(0, min(int(seconds), 21600)))
    return "تم"

async def t_lock_channel(g, m, channel, locked):
    c = find_channel(g, channel)
    if not isinstance(c, discord.TextChannel):
        return "قناة نصية غير موجودة"
    await c.set_permissions(g.default_role, send_messages=False if locked else None)
    return "تم القفل" if locked else "تم الفتح"

async def t_purge(g, m, channel, amount):
    c = find_channel(g, channel)
    if not isinstance(c, discord.TextChannel):
        return "قناة نصية غير موجودة"
    amount = max(1, min(int(amount), 100))
    if not await confirm(m, f"أمسح آخر {amount} رسالة من {c.name}؟"):
        return "المستخدم رفض أو انتهى الوقت"
    d = await c.purge(limit=amount)
    return f"انمسح {len(d)}"

async def t_delete_role(g, m, name):
    r = discord.utils.get(g.roles, name=name)
    if not r or r.is_default() or r.managed:
        return "ما لقيت الرتبة أو ما ينحذف"
    if not await confirm(m, f"أحذف رتبة {r.name} ({len(r.members)} عضو عنده)؟"):
        return "المستخدم رفض أو انتهى الوقت"
    await r.delete()
    return "تم حذف الرتبة"

def find_member(g, who):
    m = re.search(r"\d{15,}", who)
    if m:
        return g.get_member(int(m.group()))
    w = who.strip().lstrip("@").lower()
    return discord.utils.find(lambda x: w in (x.name.lower(), x.display_name.lower()), g.members)

async def t_assign_role(g, m, user, role, action="add"):
    mem, r = find_member(g, user), discord.utils.get(g.roles, name=role)
    if not mem or not r:
        return "ما لقيت العضو أو الرتبة"
    p = r.permissions
    if p.administrator or p.manage_guild or p.manage_roles:
        return "هذي الرتبة حساسة، عطها يدوي من إعدادات السيرفر"
    if r.name in (ADMIN_ROLE, MOD_ROLE):
        if not await confirm(m, f"أعطي/أشيل رتبة {r.name} لـ {mem.display_name}؟"):
            return "المستخدم رفض أو انتهى الوقت"
    if action == "remove":
        await mem.remove_roles(r)
        return f"انشالت {r.name} من {mem.display_name}"
    await mem.add_roles(r)
    return f"انعطت {r.name} لـ {mem.display_name}"

async def t_create_scheduled_event(g, m, name, description, start_time_iso, channel_name=None):
    ch = find_channel(g, channel_name) if channel_name else next((c for c in g.voice_channels), None)
    try:
        dt = datetime.fromisoformat(start_time_iso)
        await g.create_scheduled_event(
            name=name,
            description=description,
            start_time=dt,
            channel=ch,
            entity_type=discord.ChannelType.voice if ch else discord.ChannelType.external,
            privacy_level=discord.PrivacyLevel.guild_only,
            location="السيرفر" if not ch else None
        )
        return f"تم إنشاء الحدث: {name}"
    except Exception as e:
        return f"فشل إنشاء الحدث: {e}"

async def t_list_scheduled_event(g, m):
    events = g.scheduled_events
    if not events:
        return "لا توجد أحداث قادمة حالياً."
    return "\n".join([f"- {e.name} (البداية: {e.start_time})" for e in events])

TOOL_FUNCS = {
    "assign_role": t_assign_role, "delete_role": t_delete_role,
    "list_channels": t_list_channels, "create_category": t_create_category,
    "create_channel": t_create_channel, "rename_channel": t_rename_channel,
    "move_channel": t_move_channel, "delete_channel": t_delete_channel,
    "create_role": t_create_role, "send_message": t_send_message,
    "set_slowmode": t_set_slowmode, "lock_channel": t_lock_channel, "purge": t_purge,
    "create_scheduled_event": t_create_scheduled_event,
    "list_scheduled_event": t_list_scheduled_event,
}

def tool(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}

S, I = {"type": "string"}, {"type": "integer"}
TOOLS = [
    tool("list_channels", "اعرض كل الفئات والقنوات", {}, []),
    tool("create_category", "أنشئ فئة", {"name": S}, ["name"]),
    tool("create_channel", "أنشئ قناة. kind: text أو voice أو forum", {"name": S, "kind": S, "category": S}, ["name"]),
    tool("rename_channel", "غيّر اسم قناة", {"old": S, "new": S}, ["old", "new"]),
    tool("move_channel", "انقل قناة لفئة", {"channel": S, "category": S}, ["channel", "category"]),
    tool("delete_channel", "احذف قناة أو فئة (تحتاج تأكيد)", {"name": S}, ["name"]),
    tool("create_role", "أنشئ رتبة", {"name": S, "color_hex": S}, ["name"]),
    tool("send_message", "أرسل رسالة لقناة", {"channel": S, "content": S}, ["channel", "content"]),
    tool("set_slowmode", "وضع بطيء بالثواني", {"channel": S, "seconds": I}, ["channel", "seconds"]),
    tool("lock_channel", "اقفل أو افتح قناة", {"channel": S, "locked": {"type": "boolean"}}, ["channel", "locked"]),
    tool("purge", "امسح آخر N رسالة (تحتاج تأكيد)", {"channel": S, "amount": I}, ["channel", "amount"]),
    tool("assign_role", "أعط عضو رتبة أو شيلها منه. user: منشن أو ايدي أو اسم. action: add أو remove", {"user": S, "role": S, "action": S}, ["user", "role"]),
    tool("delete_role", "احذف رتبة (تحتاج تأكيد)", {"name": S}, ["name"]),
    tool("create_scheduled_event", "أنشئ حدثاً مجدولاً في السيرفر. start_time_iso بصيغة ISO مثل 2026-06-01T20:00:00", {"name": S, "description": S, "start_time_iso": S, "channel_name": S}, ["name", "start_time_iso"]),
    tool("list_scheduled_event", "اعرض الأحداث المجدولة في السيرفر", {}, []),
]

SYSTEM = ("أنت مساعد ذكي وإداري لسيرفر دسكورد تتحدث بالعامية السعودية. "
          "تستطيع الإجابة على الأسئلة العامة (مثل الطقس، العلوم، السوالف) وإدارة السيرفر عبر الأدوات المتاحة عند الطلب. "
          "تذكر السياق من الرسائل السابقة واجعل ردودك مختصرة وودودة.")

# تخزين الذاكرة لكل مستخدم (يحفظ آخر 15 رسالة)
user_histories = defaultdict(list)

async def ask_llm(messages):
    hdr = {"Authorization": f"Bearer {GROQ_KEY}"}
    body = {"model": MODEL, "messages": messages, "tools": TOOLS, "temperature": 0.5}
    async with aiohttp.ClientSession() as s:
        async with s.post(GROQ_URL, headers=hdr, json=body) as r:
            data = await r.json()
    if "choices" not in data:
        raise RuntimeError(str(data)[:300])
    return data["choices"][0]["message"]

async def agent(msg: discord.Message, text: str):
    uid = msg.author.id
    history = user_histories[uid]
    
    # إضافة الرسالة الجديدة للذاكرة
    history.append({"role": "user", "content": text})
    
    # الاحتفاظ بآخر 15 رسالة فقط كحد أقصى للذاكرة
    if len(history) > 15:
        user_histories[uid] = history[-15:]
        history = user_histories[uid]

    messages = [{"role": "system", "content": SYSTEM}] + history
    
    for _ in range(6):
        reply = await ask_llm(messages)
        calls = reply.get("tool_calls")
        if not calls:
            content = reply.get("content") or "تم"
            # حفظ رد البوت في الذاكرة أيضاً
            history.append({"role": "assistant", "content": content})
            return content
            
        messages.append(reply)
        history.append(reply)
        
        for c in calls:
            fn = TOOL_FUNCS.get(c["function"]["name"])
            try:
                args = json.loads(c["function"]["arguments"] or "{}")
                res = await fn(msg.guild, msg, **args) if fn else "أداة غير موجودة"
            except Exception as e:
                res = f"فشل: {e}"
            await log(msg.guild, f"🤖 المساعد: {c['function']['name']} {c['function']['arguments']} -> {res}")
            tool_msg = {"role": "tool", "tool_call_id": c["id"], "content": str(res)}
            messages.append(tool_msg)
            history.append(tool_msg)
            
    return "وقفت بعد ٦ خطوات، الطلب كبير، قسمه."

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
bot.quiet = False

async def log(guild, text):
    ch = next((c for c in guild.text_channels if norm(c.name) == norm("سجل البوت")), None)
    if ch:
        try:
            await ch.send(text[:1900], allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

@bot.event
async def on_member_join(m):
    await log(m.guild, f"📥 دخل {m.mention} ({m}) | الحساب انصنع {discord.utils.format_dt(m.created_at, 'R')}")

@bot.event
async def on_member_remove(m):
    await log(m.guild, f"📤 طلع {m} ({m.id})")

@bot.event
async def on_member_ban(guild, user):
    await log(guild, f"⛔ انحظر {user} ({user.id})")

@bot.event
async def on_member_unban(guild, user):
    await log(guild, f"✅ انفك الحظر عن {user} ({user.id})")

@bot.event
async def on_member_update(before, after):
    if before.roles == after.roles:
        return
    add = [r.name for r in after.roles if r not in before.roles]
    rem = [r.name for r in before.roles if r not in after.roles]
    if all(n in PICK_ROLES for n in add + rem):
        return
    await log(after.guild, f"🎭 رتب {after.mention}: " + (f"+{', +'.join(add)} " if add else "")
              + (f"-{', -'.join(rem)}" if rem else ""))

@bot.event
async def on_message_delete(m):
    if m.guild and not m.author.bot:
        await log(m.guild, f"🗑 انحذفت رسالة من {m.author.mention} في {m.channel.mention}:\n"
                           f"{m.content[:500] or '(مرفق أو فاضية)'}")

@bot.event
async def on_message_edit(b, a):
    if a.guild and not a.author.bot and b.content != a.content:
        await log(a.guild, f"✏ تعديل من {a.author.mention} في {a.channel.mention}:\n"
                           f"قبل: {b.content[:400]}\nبعد: {a.content[:400]}")

@bot.event
async def on_bulk_message_delete(msgs):
    if msgs and msgs[0].guild:
        await log(msgs[0].guild, f"🧹 انمسح {len(msgs)} رسالة في {msgs[0].channel.mention}")

@bot.event
async def on_guild_channel_create(ch):
    if not bot.quiet:
        await log(ch.guild, f"➕ انصنعت قناة {ch.mention if hasattr(ch, 'mention') else ch.name}")

@bot.event
async def on_guild_channel_delete(ch):
    if not bot.quiet:
        await log(ch.guild, f"➖ انحذفت قناة {ch.name}")

@bot.event
async def setup_hook():
    bot.add_view(RolesView())

@bot.event
async def on_ready():
    print("جاهز:", bot.user)

@bot.command()
@commands.is_owner()
async def setup(ctx):
    status = await ctx.send("ببني الهيكل... ياخذ كم دقيقة بسبب حدود دسكورد.")
    async def say(t):
        try:
            await status.edit(content=t)
        except discord.HTTPException:
            pass
    bot.quiet = True
    try:
        errors = await build(ctx.guild, say)
    finally:
        bot.quiet = False
    try:
        await post_roles(ctx.guild)
    except Exception as e:
        errors.append(f"رسالة الرتب: {e}")
    out = "خلص. الأعضاء الجدد يشوفون الترحيب والعام والصوتي بس، والباقي يفتح لهم باختيار الرتب."
    if errors:
        out += "\nصار خطأ في:\n" + "\n".join(errors)[:1500]
    await say(out)

@bot.command()
@commands.is_owner()
async def clean(ctx):
    g = ctx.guild
    keep_cats = {norm(c[1]) for c in STRUCTURE}
    keep_ch = {(norm(c[1]), norm(l)) for c in STRUCTURE for _, l, _ in c[3]}
    protected = {x.id for x in (g.rules_channel, g.public_updates_channel, g.system_channel,
                                getattr(g, "safety_alerts_channel", None)) if x}
    def key(c):
        return (norm(c.category.name) if c.category else "", norm(c.name))
    chans = [c for c in g.channels if not isinstance(c, discord.CategoryChannel)]
    known = [c for c in chans if key(c) in keep_ch]
    if len(known) < len(keep_ch) // 2:
        return await ctx.send("ما تعرفت على أغلب القنوات اللي المفروض تبقى، شغل !setup أول وبعدها !clean.")
    extra_ch = [c for c in chans if c.id not in protected and key(c) not in keep_ch]
    extra_cat = [c for c in g.categories if norm(c.name) not in keep_cats]
    extra_roles = [r for r in g.roles if r.name in OBSOLETE_ROLES]
    if not (extra_ch or extra_cat or extra_roles):
        return await ctx.send("ما فيه شي زايد، السيرفر مطابق للقائمة.")
    lines = []
    if extra_cat:
        lines.append("فئات: " + "، ".join(c.name for c in extra_cat))
    if extra_ch:
        lines.append("قنوات: " + "، ".join(c.name for c in extra_ch[:20]) + (" ..." if len(extra_ch) > 20 else ""))
    if extra_roles:
        lines.append("رتب: " + "، ".join(r.name for r in extra_roles))
    if not await confirm(ctx.message, "بحذف هذي نهائياً، ما ترجع:\n" + "\n".join(lines)[:1500]):
        return await ctx.send("انلغى.")
    bot.quiet = True
    try:
        for c in extra_ch + extra_cat:
            await c.delete()
        for r in extra_roles:
            await r.delete()
    finally:
        bot.quiet = False
    await ctx.send("انحذف الزايد.")

@bot.event
async def on_message(msg: discord.Message):
    if msg.author.bot:
        return
    await bot.process_commands(msg)
    if msg.guild and msg.author.id == OWNER_ID and bot.user in msg.mentions and not msg.content.startswith("!"):
        text = msg.content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()
        if not text:
            return
        async with msg.channel.typing():
            try:
                out = await agent(msg, text)
            except Exception as e:
                out = f"صار خطأ: {e}"
        await msg.reply(out[:2000])

bot.owner_id = OWNER_ID
bot.run(TOKEN)

