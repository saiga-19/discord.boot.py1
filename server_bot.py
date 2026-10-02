import os, re, json, aiohttp, discord
from datetime import datetime, timedelta, timezone
from discord.ext import commands

TOKEN = os.environ["DISCORD_TOKEN"]
GROQ_KEY = os.environ["GROQ_API_KEY"]
OWNER_ID = int(os.environ["OWNER_ID"])
MODEL = os.environ.get("MODEL_NAME", "llama-3.3-70b-versatile")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

TZ = timezone(timedelta(hours=3))  # توقيت مكة
SEP = "・"  # الفاصل بين الإيموجي والاسم وبين كلمات الاسم، غيره إذا تبي
MOD_ROLE = "مشرف"
ADMIN_ROLE = "إداري"
# رتب الاهتمامات (تفتح القنوات): الاسم -> إيموجي الزر
INTEREST_ROLES = {"قصص": "📖", "ألعاب": "🎮"}
# رتب قديمة ما نبيها، !clean يحذفها بعد تأكيد
OBSOLETE_ROLES = ["تقنية", "نيكس"]
# رتب تعريفية (ما تفتح قنوات، بس تنعرض وتنمنشن): كاتب، ومستمع للقصص
IDENTITY_ROLES = {"كاتب": "📝", "مستمع": "🎧"}
PICK_ROLES = {**INTEREST_ROLES, **IDENTITY_ROLES}

# فئة: (إيموجي, اسم, من يشوفها, [قنوات])
# من يشوفها: None = الكل | "mods" = المشرفين فقط | قائمة رتب اهتمامات
# قناة: (إيموجي, اسم, النوع)  النوع: text | ro (قراءة فقط) | voice | forum
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


# ---------- أدوات الأسماء ----------
def norm(s):
    """يشيل الإيموجي والفواصل ويخلي الحروف بس، عشان نقارن الأسماء"""
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


# ---------- الصلاحيات ----------
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
        # بدون هذا حتى البوت والمشرفين ما يقدرون يكتبون في القنوات القراءة-فقط
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
            pass  # Community مو مفعلة، نسويها نصية
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
    # صلاحيات الرتب محدودة بالعمد: دسكورد ما يسمح للبوت يعطي رتبة صلاحية هو ما يملكها
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
            # أي قناة قديمة بقيت في الفئة تاخذ نفس صلاحيات الفئة، عشان ما تبقى مفتوحة للكل
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


# ---------- الأدوات اللي يقدر الـ AI يستدعيها ----------
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
            await g.create_text_channel(slug(name), category=cat)  # Community مو مفعلة
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


async def t_create_event(g, m, name, start, channel=None, location=None,
                         description="", duration_minutes=60):
    try:
        st = datetime.strptime(start, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    except ValueError:
        return "صيغة الوقت لازم تكون YYYY-MM-DD HH:MM بتوقيت مكة"
    if st <= datetime.now(TZ):
        return "الوقت في الماضي"
    end = st + timedelta(minutes=max(15, int(duration_minutes)))
    kw = dict(name=name[:100], start_time=st, end_time=end, description=description[:1000],
              privacy_level=discord.PrivacyLevel.guild_only)
    ch = find_channel(g, channel) if channel else None
    if isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
        kw["channel"] = ch
    elif location:
        kw["location"] = location[:100]
        kw["entity_type"] = discord.EntityType.external
    else:
        return "حدد قناة صوتية أو مكان للفعالية"
    ev = await g.create_scheduled_event(**kw)
    return f"انصنعت الفعالية {ev.name} وقتها {discord.utils.format_dt(st, 'F')}"


async def t_list_events(g, m):
    evs = g.scheduled_events
    if not evs:
        return "ما فيه فعاليات"
    return "\n".join(f"{e.name} - {e.start_time.astimezone(TZ):%Y-%m-%d %H:%M}" for e in evs)


async def t_delete_event(g, m, name):
    ms = [e for e in g.scheduled_events if norm(e.name) == norm(name)]
    if len(ms) != 1:
        return f"الاسم غامض أو غير موجود ({len(ms)} نتيجة)"
    if not await confirm(m, f"ألغي الفعالية {ms[0].name}؟"):
        return "المستخدم رفض أو انتهى الوقت"
    await ms[0].delete()
    return "انحذفت الفعالية"


async def t_timeout_member(g, m, user, minutes, reason="بدون سبب"):
    mem = find_member(g, user)
    if not mem:
        return "ما لقيت العضو"
    if mem.id == OWNER_ID or mem.bot:
        return "ما أكتم المالك أو البوتات"
    minutes = int(minutes)
    if minutes <= 0:
        await mem.timeout(None)
        return f"انفك الكتم عن {mem.display_name}"
    minutes = min(minutes, 40320)  # الحد الأقصى في دسكورد ٢٨ يوم
    if minutes > 1440 and not await confirm(m, f"أكتم {mem.display_name} لمدة {minutes} دقيقة؟"):
        return "المستخدم رفض أو انتهى الوقت"
    await mem.timeout(timedelta(minutes=minutes), reason=reason[:100])
    return f"انكتم {mem.display_name} لمدة {minutes} دقيقة"


async def _target_message(g, m, message_id, channel):
    """الرسالة المقصودة: ايدي إذا انعطى، أو الرسالة اللي رديت عليها، أو آخر رسالة في القناة"""
    ch = m.channel
    if channel:
        ch = next((x for x in find_matches(g, channel) if isinstance(x, discord.abc.Messageable)), None)
        if not ch:
            return None, "ما لقيت القناة"
    try:
        if message_id and str(message_id).isdigit():
            return await ch.fetch_message(int(message_id)), None
        if not channel and m.reference and m.reference.message_id:
            return await ch.fetch_message(m.reference.message_id), None
        kw = {"before": m} if ch.id == m.channel.id else {}
        async for h in ch.history(limit=1, **kw):
            return h, None
    except discord.NotFound:
        return None, "ما لقيت الرسالة"
    return None, "ما لقيت رسالة"


async def t_pin_message(g, m, message_id=None, channel=None):
    t, err = await _target_message(g, m, message_id, channel)
    if not t:
        return err
    await t.pin(reason="بأمر المالك")
    return f"تثبتت رسالة {t.author.display_name}: {t.content[:60] or '(مرفق)'}"


async def t_unpin_message(g, m, message_id=None, channel=None):
    t, err = await _target_message(g, m, message_id, channel)
    if not t:
        return err
    await t.unpin(reason="بأمر المالك")
    return f"انشال تثبيت رسالة {t.author.display_name}: {t.content[:60] or '(مرفق)'}"


async def t_set_nickname(g, m, user, nickname=""):
    mem = find_member(g, user)
    if not mem:
        return "ما لقيت العضو"
    if mem.id == g.owner_id:
        return "دسكورد ما يسمح لأي بوت يغير اسم مالك السيرفر، غيره بنفسك من دسكورد"
    await mem.edit(nick=nickname[:32] or None)
    return f"اسم {mem.name} صار {nickname[:32] or 'الاسم الأصلي'}"


async def t_kick_member(g, m, user, reason="بدون سبب"):
    mem = find_member(g, user)
    if not mem or mem.id == OWNER_ID or mem.bot:
        return "ما لقيت العضو أو ما ينطرد"
    if not await confirm(m, f"أطرد {mem.display_name}؟"):
        return "المستخدم رفض أو انتهى الوقت"
    await mem.kick(reason=reason[:100])
    return f"انطرد {mem.display_name}"


async def t_ban_member(g, m, user, reason="بدون سبب"):
    mem = find_member(g, user)
    target = mem or (discord.Object(id=int(user)) if user.strip().isdigit() else None)
    if not target or getattr(target, "id", 0) == OWNER_ID or getattr(mem, "bot", False):
        return "ما لقيت العضو أو ما ينحظر"
    if not await confirm(m, f"أحظر {mem.display_name if mem else user}؟"):
        return "المستخدم رفض أو انتهى الوقت"
    await g.ban(target, reason=reason[:100])
    return "انحظر"


async def t_unban_member(g, m, user):
    w = user.strip().lower()
    async for entry in g.bans(limit=None):
        u = entry.user
        if w in (str(u.id), u.name.lower(), (u.global_name or "").lower()):
            await g.unban(u)
            return f"انفك الحظر عن {u.name}"
    return "ما لقيته في قائمة المحظورين"


SAFE_PERMS = {"view_channel", "send_messages", "read_message_history", "add_reactions",
              "attach_files", "embed_links", "connect", "speak"}


async def t_set_channel_permission(g, m, channel, target, permission, value):
    ch = find_channel(g, channel)
    if not ch:
        return f"ما لقيت {channel}"
    if permission not in SAFE_PERMS:
        return "صلاحية غير مسموحة. المتاح: " + ", ".join(sorted(SAFE_PERMS))
    t = g.default_role if target.strip().lower() in ("everyone", "@everyone", "الكل") else (
        discord.utils.get(g.roles, name=target) or find_member(g, target))
    if not t:
        return "ما لقيت الرتبة أو العضو"
    v = {"allow": True, "deny": False, "reset": None}.get(value)
    if value not in ("allow", "deny", "reset"):
        return "value لازم allow أو deny أو reset"
    await ch.set_permissions(t, **{permission: v})
    return "تم"


async def t_create_invite(g, m, channel=None, max_uses=0, max_age_hours=24):
    ch = find_channel(g, channel) if channel else (g.text_channels[0] if g.text_channels else None)
    if not ch or isinstance(ch, discord.CategoryChannel):
        return "ما لقيت قناة"
    inv = await ch.create_invite(max_age=int(max_age_hours) * 3600, max_uses=int(max_uses), unique=True)
    return inv.url


async def t_edit_role(g, m, role, new_name=None, color_hex=None, hoist=None, mentionable=None):
    r = discord.utils.get(g.roles, name=role)
    if not r or r.is_default() or r.managed:
        return "ما لقيت الرتبة أو ما تتعدل"
    kw = {}
    if new_name:
        kw["name"] = new_name[:100]
    if color_hex:
        kw["colour"] = discord.Colour(int(color_hex.lstrip("#"), 16))
    if hoist is not None:
        kw["hoist"] = bool(hoist)
    if mentionable is not None:
        kw["mentionable"] = bool(mentionable)
    await r.edit(**kw)
    return "تم تعديل الرتبة"


def _msgbl(g, m, channel):
    if not channel:
        return m.channel
    return next((x for x in find_matches(g, channel) if isinstance(x, discord.abc.Messageable)), None)


async def t_send_embed(g, m, title, description="", channel=None, color_hex="#5865f2"):
    ch = _msgbl(g, m, channel)
    if not ch:
        return "ما لقيت القناة"
    await ch.send(embed=discord.Embed(title=title[:256], description=description[:4000],
                                      colour=discord.Colour(int(color_hex.lstrip("#"), 16))))
    return "تم الإرسال"


async def t_create_poll(g, m, question, options, channel=None, hours=24, multiple=False):
    ch = _msgbl(g, m, channel)
    if not ch:
        return "ما لقيت القناة"
    opts = [o.strip() for o in re.split(r"[,،\n]", options) if o.strip()][:10]
    if len(opts) < 2:
        return "لازم خيارين على الأقل (افصل بينهم بفاصلة)"
    poll = discord.Poll(question=question[:300], duration=timedelta(hours=max(1, min(int(hours), 768))),
                        multiple=bool(multiple))
    for o in opts:
        poll.add_answer(text=o[:55])
    await ch.send(poll=poll)
    return "اننشر التصويت"


async def t_add_reaction(g, m, emoji, message_id=None, channel=None):
    t, err = await _target_message(g, m, message_id, channel)
    if not t:
        return err
    await t.add_reaction(emoji)
    return "تم"


async def t_edit_message(g, m, content, message_id=None, channel=None):
    t, err = await _target_message(g, m, message_id, channel)
    if not t:
        return err
    if t.author.id != bot.user.id:
        return "أقدر أعدل رسائلي أنا بس"
    await t.edit(content=content[:2000])
    return "تم التعديل"


async def t_delete_message(g, m, message_id=None, channel=None):
    t, err = await _target_message(g, m, message_id, channel)
    if not t:
        return err
    if not await confirm(m, f"أحذف رسالة {t.author.display_name}: {t.content[:80] or '(مرفق)'}؟"):
        return "المستخدم رفض أو انتهى الوقت"
    await t.delete()
    return "انحذفت"


async def t_create_thread(g, m, name, channel=None, message_id=None):
    ch = _msgbl(g, m, channel)
    if not isinstance(ch, discord.TextChannel):
        return "الثريد يحتاج قناة نصية عادية"
    if message_id and str(message_id).isdigit():
        th = await (await ch.fetch_message(int(message_id))).create_thread(name=name[:100])
    else:
        th = await ch.create_thread(name=name[:100], type=discord.ChannelType.public_thread)
    return f"انصنع الثريد {th.name}"


async def t_archive_thread(g, m, name, lock=False):
    th = discord.utils.find(lambda t: norm(t.name) == norm(name), g.threads)
    if not th:
        return "ما لقيت الثريد"
    await th.edit(archived=True, locked=bool(lock))
    return "تمت أرشفته"


async def t_set_channel_topic(g, m, channel, topic):
    c = find_channel(g, channel)
    if not isinstance(c, (discord.TextChannel, discord.ForumChannel)):
        return "قناة نصية أو منتدى بس"
    await c.edit(topic=topic[:1024])
    return "تم"


async def t_move_member(g, m, user, channel=None):
    mem = find_member(g, user)
    if not mem or not mem.voice:
        return "العضو مو في روم صوتي"
    if not channel:
        await mem.move_to(None)
        return f"انفصل {mem.display_name} من الصوتي"
    ch = find_channel(g, channel)
    if not isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
        return "ما لقيت الروم الصوتي"
    await mem.move_to(ch)
    return f"اننقل {mem.display_name} إلى {ch.name}"


async def t_voice_mute(g, m, user, muted=True):
    mem = find_member(g, user)
    if not mem or not mem.voice:
        return "العضو مو في روم صوتي"
    await mem.edit(mute=bool(muted))
    return "انكتم صوتياً" if muted else "انفك الكتم الصوتي"


async def t_member_info(g, m, user):
    mem = find_member(g, user)
    if not mem:
        return "ما لقيت العضو"
    roles = ", ".join(r.name for r in mem.roles[1:]) or "ما فيه"
    joined = f"{mem.joined_at:%Y-%m-%d}" if mem.joined_at else "؟"
    return (f"{mem} | دخل: {joined} | الحساب: {mem.created_at:%Y-%m-%d} | الرتب: {roles} | "
            f"مكتوم: {'نعم' if mem.is_timed_out() else 'لا'}")


async def t_server_info(g, m):
    return (f"{g.name} | الأعضاء {g.member_count} | القنوات {len(g.channels)} | "
            f"الرتب {len(g.roles)} | المالك {g.owner}")


async def t_list_roles(g, m):
    return ", ".join(f"{r.name}({len(r.members)})" for r in reversed(g.roles) if not r.is_default())


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


TOOL_FUNCS = {
    "assign_role": t_assign_role, "delete_role": t_delete_role,
    "create_event": t_create_event, "list_events": t_list_events, "delete_event": t_delete_event,
    "set_nickname": t_set_nickname, "kick_member": t_kick_member, "ban_member": t_ban_member,
    "unban_member": t_unban_member, "set_channel_permission": t_set_channel_permission,
    "create_invite": t_create_invite, "edit_role": t_edit_role,
    "send_embed": t_send_embed, "create_poll": t_create_poll, "add_reaction": t_add_reaction,
    "edit_message": t_edit_message, "delete_message": t_delete_message,
    "create_thread": t_create_thread, "archive_thread": t_archive_thread,
    "set_channel_topic": t_set_channel_topic, "move_member": t_move_member, "voice_mute": t_voice_mute,
    "member_info": t_member_info, "server_info": t_server_info, "list_roles": t_list_roles,
    "timeout_member": t_timeout_member, "pin_message": t_pin_message, "unpin_message": t_unpin_message,
    "list_channels": t_list_channels, "create_category": t_create_category,
    "create_channel": t_create_channel, "rename_channel": t_rename_channel,
    "move_channel": t_move_channel, "delete_channel": t_delete_channel,
    "create_role": t_create_role, "send_message": t_send_message,
    "set_slowmode": t_set_slowmode, "lock_channel": t_lock_channel, "purge": t_purge,
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
    tool("assign_role", "أعط عضو رتبة أو شيلها منه. user: منشن أو ايدي أو اسم. action: add أو remove",
         {"user": S, "role": S, "action": S}, ["user", "role"]),
    tool("delete_role", "احذف رتبة (تحتاج تأكيد)", {"name": S}, ["name"]),
    tool("create_event", "أنشئ فعالية مجدولة. start بصيغة YYYY-MM-DD HH:MM بتوقيت مكة. "
         "channel اسم قناة صوتية، أو location مكان خارجي (واحد منهم لازم)",
         {"name": S, "start": S, "channel": S, "location": S, "description": S, "duration_minutes": I},
         ["name", "start"]),
    tool("list_events", "اعرض الفعاليات المجدولة", {}, []),
    tool("delete_event", "ألغِ فعالية (تحتاج تأكيد)", {"name": S}, ["name"]),
    tool("timeout_member", "اكتم عضو (تايم أوت) لعدد دقائق، 0 يفك الكتم. user: منشن أو ايدي أو اسم",
         {"user": S, "minutes": I, "reason": S}, ["user", "minutes"]),
    tool("pin_message", "ثبّت رسالة: بالايدي، أو الرسالة اللي رد عليها المالك، أو آخر رسالة في القناة",
         {"message_id": S, "channel": S}, []),
    tool("send_embed", "أرسل رسالة منسقة (إعلان) بعنوان ووصف ولون", {"title": S, "description": S, "channel": S, "color_hex": S}, ["title"]),
    tool("create_poll", "أنشئ تصويت. options خيارات مفصولة بفاصلة (٢ إلى ١٠)",
         {"question": S, "options": S, "channel": S, "hours": I, "multiple": {"type": "boolean"}}, ["question", "options"]),
    tool("add_reaction", "حط ريأكشن على رسالة (بالايدي أو المردود عليها أو آخر رسالة)", {"emoji": S, "message_id": S, "channel": S}, ["emoji"]),
    tool("edit_message", "عدّل رسالة من رسائل البوت نفسه", {"content": S, "message_id": S, "channel": S}, ["content"]),
    tool("delete_message", "احذف رسالة واحدة (تحتاج تأكيد)", {"message_id": S, "channel": S}, []),
    tool("create_thread", "أنشئ ثريد في قناة نصية، ومن رسالة إذا انعطى message_id", {"name": S, "channel": S, "message_id": S}, ["name"]),
    tool("archive_thread", "أرشف ثريد وقفله اختياري", {"name": S, "lock": {"type": "boolean"}}, ["name"]),
    tool("set_channel_topic", "غيّر وصف قناة", {"channel": S, "topic": S}, ["channel", "topic"]),
    tool("move_member", "انقل عضو لروم صوتي، بدون channel يفصله من الصوتي", {"user": S, "channel": S}, ["user"]),
    tool("voice_mute", "اكتم أو فك كتم عضو في الصوتي", {"user": S, "muted": {"type": "boolean"}}, ["user"]),
    tool("member_info", "معلومات عضو: تاريخ الدخول والرتب والكتم", {"user": S}, ["user"]),
    tool("server_info", "معلومات عامة عن السيرفر", {}, []),
    tool("list_roles", "اعرض الرتب وعدد أعضاء كل رتبة", {}, []),
    tool("set_nickname", "غيّر نك نيم عضو (فاضي يرجعه للأصلي). ما ينفع مع مالك السيرفر",
         {"user": S, "nickname": S}, ["user"]),
    tool("kick_member", "اطرد عضو (يحتاج تأكيد)", {"user": S, "reason": S}, ["user"]),
    tool("ban_member", "احظر عضو بالمنشن أو الايدي (يحتاج تأكيد)", {"user": S, "reason": S}, ["user"]),
    tool("unban_member", "فك الحظر عن شخص بالاسم أو الايدي", {"user": S}, ["user"]),
    tool("set_channel_permission", "عدّل صلاحية رتبة أو عضو في قناة. target: اسم رتبة أو everyone أو عضو. "
         "permission من: view_channel, send_messages, read_message_history, add_reactions, attach_files, "
         "embed_links, connect, speak. value: allow أو deny أو reset",
         {"channel": S, "target": S, "permission": S, "value": S}, ["channel", "target", "permission", "value"]),
    tool("create_invite", "أنشئ رابط دعوة. max_uses 0 = بلا حد", {"channel": S, "max_uses": I, "max_age_hours": I}, []),
    tool("edit_role", "عدّل رتبة: اسم أو لون أو hoist أو mentionable",
         {"role": S, "new_name": S, "color_hex": S, "hoist": {"type": "boolean"}, "mentionable": {"type": "boolean"}}, ["role"]),
    tool("unpin_message", "شيل تثبيت رسالة (نفس طريقة تحديد pin_message)", {"message_id": S, "channel": S}, []),
]

SYSTEM = ("أنت مساعد ذكي وإداري لسيرفر دسكورد، تتكلم بالعامية السعودية وردودك مختصرة وودودة. "
          "تجاوب على الأسئلة العامة (علوم، سوالف) وتدير السيرفر بالأدوات المتاحة عند الطلب، وتنفذ أوامر المالك فقط. "
          "ما عندك إنترنت، فلا تخترع طقس أو أخبار. "
          "أسماء القنوات فيها إيموجي وفواصل، والبحث يتم بالاسم بدونها (مثلاً الدردشة). "
          "لا تخترع أدوات. إذا الطلب غامض اسأل. بعد التنفيذ لخص اللي تم بجملة قصيرة. "
          "إذا رفض دسكورد الأداة بسبب الصلاحيات قل بالضبط وش الناقص. "
          "إذا ما فيه أداة للطلب قل كذا بصراحة.")


async def ask_llm(messages):
    hdr = {"Authorization": f"Bearer {GROQ_KEY}"}
    body = {"model": MODEL, "messages": messages, "tools": TOOLS, "temperature": 0.3}
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())) as s:
        async with s.post(GROQ_URL, headers=hdr, json=body) as r:
            data = await r.json()
    if "choices" not in data:
        raise RuntimeError(str(data)[:300])
    return data["choices"][0]["message"]


def strip_mention(c):
    return c.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()


async def get_history(msg, n=10):
    """آخر n رسائل من الحوار بينك وبين البوت في نفس القناة.
    رسائل باقي الأعضاء ما تدخل أبداً (حماية من حقن الأوامر)."""
    out = []
    async for h in msg.channel.history(limit=60, before=msg):
        if h.author.id == OWNER_ID and bot.user in h.mentions:
            out.append({"role": "user", "content": strip_mention(h.content)})
        elif h.author == bot.user and h.reference and h.content:
            out.append({"role": "assistant", "content": h.content})
        if len(out) >= n:
            break
    return out[::-1]


async def agent(msg: discord.Message, text: str, history=()):
    now = datetime.now(TZ)
    sysmsg = SYSTEM + f" الوقت الحالي بتوقيت مكة: {now:%Y-%m-%d %H:%M} ({now:%A}). استخدمه لحساب أوقات الفعاليات."
    messages = [{"role": "system", "content": sysmsg}, *history, {"role": "user", "content": text}]
    for _ in range(6):
        reply = await ask_llm(messages)
        calls = reply.get("tool_calls")
        if not calls:
            return reply.get("content") or "تم"
        messages.append(reply)
        for c in calls:
            fn = TOOL_FUNCS.get(c["function"]["name"])
            try:
                args = json.loads(c["function"]["arguments"] or "{}")
                res = await fn(msg.guild, msg, **args) if fn else "أداة غير موجودة"
            except discord.Forbidden:
                res = ("فشل: دسكورد رفض. إما صلاحية ناقصة في رتبة البوت، أو رتبة البوت أقل من الهدف "
                       "في الترتيب، أو الهدف مالك السيرفر")
            except Exception as e:
                res = f"فشل: {e}"
            await log(msg.guild, f"🤖 المساعد: {c['function']['name']} {c['function']['arguments']} -> {res}")
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": str(res)})
    return "وقفت بعد ٦ خطوات، الطلب كبير، قسمه."


intents = discord.Intents.default()
intents.message_content = True
intents.members = True


class MyBot(commands.Bot):
    async def login(self, token):
        # محلل DNS حق النظام بدل aiodns، لأن aiodns أحياناً يفشل (Timeout while contacting DNS servers)
        self.http.connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver(), limit=0)
        await super().login(token)


bot = MyBot(command_prefix="!", intents=intents)
bot.quiet = False  # يصير True وقت !setup عشان ما يغرق السجل


# ---------- السجل ----------
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
        return  # ضغطات أزرار الاهتمامات العادية ما نسجلها
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
    """يبني الهيكل، وإذا كان موجود يحدثه (أسماء، إيموجي، صلاحيات)"""
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
    """يحذف أي قناة أو فئة مو في القائمة + الرتب القديمة، بعد ما تأكد"""
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
    # المساعد: يستجيب للمالك فقط وإذا منشن البوت. كلام باقي الأعضاء ما يوصل للـ AI أبداً.
    if msg.guild and msg.author.id == OWNER_ID and bot.user in msg.mentions and not msg.content.startswith("!"):
        text = msg.content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()
        if not text:
            return
        async with msg.channel.typing():
            try:
                out = await agent(msg, text, await get_history(msg))
            except Exception as e:
                out = f"صار خطأ: {e}"
        await msg.reply(out[:2000])


bot.owner_id = OWNER_ID
bot.run(TOKEN)
