import asyncio
import os
import sqlite3
from datetime import date, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "exercise_bot.db")
if not BOT_TOKEN:
    raise RuntimeError("Не найден BOT_TOKEN. Создайте .env по примеру .env.example")

TIME_SLOTS = ["Утро", "День", "Рабочий перерыв", "После еды", "Вечер", "Перед сном"]
WEEKDAYS = [("Пн", 0), ("Вт", 1), ("Ср", 2), ("Чт", 3), ("Пт", 4), ("Сб", 5), ("Вс", 6)]
NUM_TO_DAY = {n: label for label, n in WEEKDAYS}


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.executescript("""
        PRAGMA foreign_keys = ON;
        CREATE TABLE IF NOT EXISTS exercises (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            author TEXT,
            url TEXT NOT NULL,
            time_slot TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS courses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            exercise_id INTEGER NOT NULL,
            start_date TEXT NOT NULL,
            schedule_days TEXT NOT NULL,
            goal_type TEXT NOT NULL CHECK(goal_type IN ('days','completions')),
            goal_value INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused','completed')),
            completed_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(exercise_id) REFERENCES exercises(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS course_marks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            course_id INTEGER NOT NULL,
            mark_date TEXT NOT NULL,
            mark_type TEXT NOT NULL CHECK(mark_type IN ('done','skipped')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(course_id, mark_date),
            FOREIGN KEY(course_id) REFERENCES courses(id) ON DELETE CASCADE
        );
        """)


def create_exercise(user_id, title, author, url, time_slot):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO exercises(user_id,title,author,url,time_slot) VALUES(?,?,?,?,?)",
            (user_id, title.strip(), author.strip(), url.strip(), time_slot),
        )
        return cur.lastrowid


def get_exercise(user_id, exercise_id):
    with db() as conn:
        return conn.execute("SELECT * FROM exercises WHERE id=? AND user_id=?", (exercise_id, user_id)).fetchone()


def list_exercises(user_id):
    with db() as conn:
        return conn.execute("""
            SELECT e.*,
                EXISTS(SELECT 1 FROM courses c WHERE c.exercise_id=e.id AND c.user_id=e.user_id AND c.status='active') AS has_active_course
            FROM exercises e WHERE e.user_id=? ORDER BY e.created_at DESC, e.id DESC
        """, (user_id,)).fetchall()


def parse_days(value):
    return [int(x) for x in value.split(",") if x != ""] if value else []


def create_course(user_id, exercise_id, schedule_days, goal_type, goal_value):
    days = ",".join(str(x) for x in sorted(set(schedule_days)))
    with db() as conn:
        cur = conn.execute("""
            INSERT INTO courses(user_id,exercise_id,start_date,schedule_days,goal_type,goal_value,status)
            VALUES(?,?,?,?,?,?,'active')
        """, (user_id, exercise_id, date.today().isoformat(), days, goal_type, goal_value))
        return cur.lastrowid


def get_course(user_id, course_id):
    with db() as conn:
        return conn.execute("""
            SELECT c.*, e.title, e.author, e.url, e.time_slot
            FROM courses c JOIN exercises e ON e.id=c.exercise_id
            WHERE c.id=? AND c.user_id=?
        """, (course_id, user_id)).fetchone()


def list_courses(user_id, status):
    with db() as conn:
        return conn.execute("""
            SELECT c.*, e.title, e.author, e.url, e.time_slot,
              (SELECT COUNT(*) FROM course_marks m WHERE m.course_id=c.id AND m.mark_type='done') AS done_count
            FROM courses c JOIN exercises e ON e.id=c.exercise_id
            WHERE c.user_id=? AND c.status=? ORDER BY c.created_at DESC, c.id DESC
        """, (user_id, status)).fetchall()


def done_count(course_id):
    with db() as conn:
        return conn.execute("SELECT COUNT(*) n FROM course_marks WHERE course_id=? AND mark_type='done'", (course_id,)).fetchone()["n"]


def mark_for_today(course_id):
    with db() as conn:
        return conn.execute("SELECT * FROM course_marks WHERE course_id=? AND mark_date=?", (course_id, date.today().isoformat())).fetchone()


def course_progress(c):
    if c["goal_type"] == "days":
        current = (date.today() - date.fromisoformat(c["start_date"])).days + 1
        current = max(1, min(current, c["goal_value"]))
        return f"День {current} из {c['goal_value']}"
    return f"Выполнено {done_count(c['id'])} из {c['goal_value']}"


def auto_complete(user_id):
    today = date.today()
    for c in list_courses(user_id, "active"):
        complete = False
        if c["goal_type"] == "days":
            last_day = date.fromisoformat(c["start_date"]) + timedelta(days=c["goal_value"] - 1)
            complete = today > last_day
        else:
            complete = done_count(c["id"]) >= c["goal_value"]
        if complete:
            with db() as conn:
                conn.execute("UPDATE courses SET status='completed', completed_at=? WHERE id=?", (today.isoformat(), c["id"]))


def today_courses(user_id):
    auto_complete(user_id)
    today = date.today()
    out = []
    for c in list_courses(user_id, "active"):
        start = date.fromisoformat(c["start_date"])
        if today < start or today.weekday() not in parse_days(c["schedule_days"]):
            continue
        if c["goal_type"] == "days":
            last_day = start + timedelta(days=c["goal_value"] - 1)
            if today > last_day:
                continue
        elif done_count(c["id"]) >= c["goal_value"]:
            continue
        if mark_for_today(c["id"]):
            continue
        out.append(c)
    order = {name: i for i, name in enumerate(TIME_SLOTS)}
    out.sort(key=lambda c: (order.get(c["time_slot"], 999), c["title"]))
    return out


def save_mark(user_id, course_id, mark_type):
    with db() as conn:
        conn.execute("""
            INSERT INTO course_marks(user_id,course_id,mark_date,mark_type) VALUES(?,?,?,?)
            ON CONFLICT(course_id,mark_date) DO UPDATE SET mark_type=excluded.mark_type
        """, (user_id, course_id, date.today().isoformat(), mark_type))
    c = get_course(user_id, course_id)
    if c and c["goal_type"] == "completions" and done_count(course_id) >= c["goal_value"]:
        with db() as conn:
            conn.execute("UPDATE courses SET status='completed', completed_at=? WHERE id=?", (date.today().isoformat(), course_id))


def set_status(user_id, course_id, status):
    with db() as conn:
        if status == "completed":
            conn.execute("UPDATE courses SET status=?,completed_at=? WHERE id=? AND user_id=?", (status, date.today().isoformat(), course_id, user_id))
        else:
            conn.execute("UPDATE courses SET status=?,completed_at=NULL WHERE id=? AND user_id=?", (status, course_id, user_id))


def delete_exercise(user_id, exercise_id):
    with db() as conn:
        conn.execute("DELETE FROM exercises WHERE id=? AND user_id=?", (exercise_id, user_id))


def schedule_text(value):
    nums = parse_days(value)
    return "ежедневно" if nums == list(range(7)) else ", ".join(NUM_TO_DAY[n] for n in nums)


def author_text(author):
    return author.strip() if author and author.strip() else "Автор не указан"


main_kb = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="Сегодня"), KeyboardButton(text="Мои курсы")],
    [KeyboardButton(text="Библиотека"), KeyboardButton(text="Добавить комплекс")],
    [KeyboardButton(text="Настройки")],
], resize_keyboard=True)


def today_kb(c):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Открыть видео", url=c["url"])],
        [InlineKeyboardButton(text="✓ Сделала", callback_data=f"done:{c['id']}"), InlineKeyboardButton(text="Пропустить сегодня", callback_data=f"skip:{c['id']}")],
    ])


def course_kb(c):
    rows = [[InlineKeyboardButton(text="▶️ Видео", url=c["url"])]]
    if c["status"] == "active":
        rows.append([InlineKeyboardButton(text="⏸ Пауза", callback_data=f"pause:{c['id']}"), InlineKeyboardButton(text="✓ Завершить", callback_data=f"finish:{c['id']}")])
    elif c["status"] == "paused":
        rows.append([InlineKeyboardButton(text="▶ Возобновить", callback_data=f"resume:{c['id']}"), InlineKeyboardButton(text="✓ Завершить", callback_data=f"finish:{c['id']}")])
    else:
        rows.append([InlineKeyboardButton(text="↻ Начать снова", callback_data=f"restart:{c['id']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def library_kb(ex):
    rows = [[InlineKeyboardButton(text="▶️ Видео", url=ex["url"])]]
    if not ex["has_active_course"]:
        rows.append([InlineKeyboardButton(text="Начать курс", callback_data=f"startcourse:{ex['id']}")])
    rows.append([InlineKeyboardButton(text="Удалить из библиотеки", callback_data=f"delete:{ex['id']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def slot_kb():
    b = InlineKeyboardBuilder()
    for slot in TIME_SLOTS:
        b.button(text=slot, callback_data=f"slot:{slot}")
    b.adjust(2)
    return b.as_markup()


def days_kb(selected, prefix):
    b = InlineKeyboardBuilder()
    for label, num in WEEKDAYS:
        b.button(text=("✓ " if num in selected else "") + label, callback_data=f"{prefix}day:{num}")
    b.adjust(4, 3)
    b.row(InlineKeyboardButton(text="Каждый день", callback_data=f"{prefix}all"), InlineKeyboardButton(text="Готово", callback_data=f"{prefix}done"))
    return b.as_markup()


def goal_kb(prefix):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="По календарю", callback_data=f"{prefix}goal:days")],
        [InlineKeyboardButton(text="По количеству выполнений", callback_data=f"{prefix}goal:completions")],
    ])


class AddExercise(StatesGroup):
    url = State(); title = State(); author = State(); slot = State(); days = State(); after_save = State()

class CourseSetup(StatesGroup):
    days = State(); goal_type = State(); goal_value = State()

class RestartSetup(StatesGroup):
    days = State(); goal_type = State(); goal_value = State()


bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer("Привет. Я помогу выбирать комплексы на сегодня и проходить их курсами.", reply_markup=main_kb)


@dp.message(F.text == "Сегодня")
async def show_today(message: Message):
    items = today_courses(message.from_user.id)
    if not items:
        await message.answer("На сегодня больше ничего не запланировано ✓", reply_markup=main_kb)
        return
    last_slot = None
    for c in items:
        if c["time_slot"] != last_slot:
            await message.answer(f"<b>{c['time_slot']}</b>")
            last_slot = c["time_slot"]
        await message.answer(f"<b>{c['title']}</b>\n{author_text(c['author'])}\n📆 {schedule_text(c['schedule_days'])}\n📍 {course_progress(c)}", reply_markup=today_kb(c))


@dp.callback_query(F.data.startswith("done:"))
async def done(call: CallbackQuery):
    cid = int(call.data.split(":")[1]); c = get_course(call.from_user.id, cid)
    if not c: return await call.answer("Курс не найден", show_alert=True)
    save_mark(call.from_user.id, cid, "done"); c = get_course(call.from_user.id, cid)
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer("✓ Сделано. Курс завершён." if c["status"] == "completed" else f"✓ Сделано. {course_progress(c)}")
    await call.answer()


@dp.callback_query(F.data.startswith("skip:"))
async def skip(call: CallbackQuery):
    cid = int(call.data.split(":")[1])
    save_mark(call.from_user.id, cid, "skipped")
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer("Сегодня пропускаем. Курс продолжается.")
    await call.answer()


@dp.message(F.text == "Мои курсы")
async def my_courses(message: Message):
    auto_complete(message.from_user.id); found = False
    for title, status in [("Активные", "active"), ("На паузе", "paused"), ("Завершённые", "completed")]:
        items = list_courses(message.from_user.id, status)
        if not items: continue
        found = True; await message.answer(f"<b>{title}</b>")
        for c in items:
            await message.answer(f"<b>{c['title']}</b>\n{author_text(c['author'])}\n🕒 {c['time_slot']}\n📆 {schedule_text(c['schedule_days'])}\n📍 {course_progress(c)}", reply_markup=course_kb(c))
    if not found: await message.answer("Курсов пока нет.")


@dp.callback_query(F.data.startswith("pause:"))
async def pause(call: CallbackQuery): set_status(call.from_user.id, int(call.data.split(":")[1]), "paused"); await call.message.answer("Курс поставлен на паузу."); await call.answer()
@dp.callback_query(F.data.startswith("resume:"))
async def resume(call: CallbackQuery): set_status(call.from_user.id, int(call.data.split(":")[1]), "active"); await call.message.answer("Курс снова активен."); await call.answer()
@dp.callback_query(F.data.startswith("finish:"))
async def finish(call: CallbackQuery): set_status(call.from_user.id, int(call.data.split(":")[1]), "completed"); await call.message.answer("Курс завершён и сохранён в истории."); await call.answer()


@dp.message(F.text == "Библиотека")
async def library(message: Message):
    items = list_exercises(message.from_user.id)
    if not items: return await message.answer("Библиотека пока пустая.")
    for ex in items:
        active = "\n🟢 Есть активный курс" if ex["has_active_course"] else ""
        await message.answer(f"<b>{ex['title']}</b>\n{author_text(ex['author'])}\n🕒 {ex['time_slot']}{active}", reply_markup=library_kb(ex))


@dp.callback_query(F.data.startswith("delete:"))
async def delete_ask(call: CallbackQuery):
    eid = int(call.data.split(":")[1]); ex = get_exercise(call.from_user.id, eid)
    if not ex: return await call.answer("Комплекс не найден", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Да, удалить", callback_data=f"deleteyes:{eid}"), InlineKeyboardButton(text="Отмена", callback_data="noop")]])
    await call.message.answer(f"Удалить «{ex['title']}» и всю историю его курсов?", reply_markup=kb); await call.answer()

@dp.callback_query(F.data.startswith("deleteyes:"))
async def delete_yes(call: CallbackQuery): delete_exercise(call.from_user.id, int(call.data.split(":")[1])); await call.message.answer("Удалено из библиотеки."); await call.answer()
@dp.callback_query(F.data == "noop")
async def noop(call: CallbackQuery): await call.answer("Отменено")


@dp.message(F.text == "Добавить комплекс")
async def add_start(message: Message, state: FSMContext):
    await state.clear(); await state.set_state(AddExercise.url); await message.answer("Пришли ссылку на видео.")

@dp.message(AddExercise.url)
async def add_url(message: Message, state: FSMContext):
    url = (message.text or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")): return await message.answer("Нужна ссылка, начинающаяся с http:// или https://")
    await state.update_data(url=url); await state.set_state(AddExercise.title); await message.answer("Как называется комплекс?")

@dp.message(AddExercise.title)
async def add_title(message: Message, state: FSMContext):
    title=(message.text or "").strip()
    if not title: return await message.answer("Название не должно быть пустым.")
    await state.update_data(title=title); await state.set_state(AddExercise.author); await message.answer("Автор? Если не хочешь указывать — отправь «-».")

@dp.message(AddExercise.author)
async def add_author(message: Message, state: FSMContext):
    author=(message.text or "").strip(); author="" if author=="-" else author
    await state.update_data(author=author); await state.set_state(AddExercise.slot); await message.answer("Когда делать?", reply_markup=slot_kb())

@dp.callback_query(AddExercise.slot, F.data.startswith("slot:"))
async def add_slot(call: CallbackQuery, state: FSMContext):
    await state.update_data(time_slot=call.data.split(":",1)[1], selected_days=[]); await state.set_state(AddExercise.days)
    await call.message.answer("В какие дни недели?", reply_markup=days_kb(set(), "add_")); await call.answer()

@dp.callback_query(AddExercise.days, F.data.startswith("add_day:"))
async def add_day(call: CallbackQuery, state: FSMContext):
    n=int(call.data.split(":")[1]); data=await state.get_data(); selected=set(data.get("selected_days",[])); selected.remove(n) if n in selected else selected.add(n)
    await state.update_data(selected_days=sorted(selected)); await call.message.edit_reply_markup(reply_markup=days_kb(selected,"add_")); await call.answer()

@dp.callback_query(AddExercise.days, F.data == "add_all")
async def add_all(call: CallbackQuery, state: FSMContext):
    selected=set(range(7)); await state.update_data(selected_days=sorted(selected)); await call.message.edit_reply_markup(reply_markup=days_kb(selected,"add_")); await call.answer()

@dp.callback_query(AddExercise.days, F.data == "add_done")
async def add_days_done(call: CallbackQuery, state: FSMContext):
    data=await state.get_data(); selected=data.get("selected_days",[])
    if not selected: return await call.answer("Выбери хотя бы один день", show_alert=True)
    eid=create_exercise(call.from_user.id,data["title"],data.get("author",""),data["url"],data["time_slot"])
    await state.update_data(exercise_id=eid); await state.set_state(AddExercise.after_save)
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Начать курс",callback_data="addcourse"),InlineKeyboardButton(text="Только сохранить",callback_data="saveonly")]])
    await call.message.answer("Комплекс сохранён. Начать курс сейчас?",reply_markup=kb); await call.answer()

@dp.callback_query(AddExercise.after_save, F.data == "saveonly")
async def save_only(call: CallbackQuery, state: FSMContext): await state.clear(); await call.message.answer("Сохранено ✓", reply_markup=main_kb); await call.answer()

@dp.callback_query(AddExercise.after_save, F.data == "addcourse")
async def add_course(call: CallbackQuery, state: FSMContext):
    data=await state.get_data(); await state.update_data(course_exercise_id=data["exercise_id"],course_days=data["selected_days"]); await state.set_state(CourseSetup.goal_type)
    await call.message.answer("Как считать курс?",reply_markup=goal_kb("course_")); await call.answer()


@dp.callback_query(F.data.startswith("startcourse:"))
async def start_course(call: CallbackQuery, state: FSMContext):
    eid=int(call.data.split(":")[1]); await state.clear(); await state.set_state(CourseSetup.days); await state.update_data(course_exercise_id=eid,course_days=[])
    await call.message.answer("В какие дни выполнять этот курс?",reply_markup=days_kb(set(),"course_")); await call.answer()

@dp.callback_query(CourseSetup.days, F.data.startswith("course_day:"))
async def course_day(call: CallbackQuery, state: FSMContext):
    n=int(call.data.split(":")[1]); data=await state.get_data(); selected=set(data.get("course_days",[])); selected.remove(n) if n in selected else selected.add(n)
    await state.update_data(course_days=sorted(selected)); await call.message.edit_reply_markup(reply_markup=days_kb(selected,"course_")); await call.answer()

@dp.callback_query(CourseSetup.days, F.data == "course_all")
async def course_all(call: CallbackQuery, state: FSMContext):
    selected=set(range(7)); await state.update_data(course_days=sorted(selected)); await call.message.edit_reply_markup(reply_markup=days_kb(selected,"course_")); await call.answer()

@dp.callback_query(CourseSetup.days, F.data == "course_done")
async def course_days_done(call: CallbackQuery, state: FSMContext):
    data=await state.get_data()
    if not data.get("course_days"): return await call.answer("Выбери хотя бы один день",show_alert=True)
    await state.set_state(CourseSetup.goal_type); await call.message.answer("Как считать курс?",reply_markup=goal_kb("course_")); await call.answer()

@dp.callback_query(CourseSetup.goal_type, F.data.startswith("course_goal:"))
async def course_goal(call: CallbackQuery, state: FSMContext):
    gt=call.data.split(":")[1]; await state.update_data(course_goal_type=gt); await state.set_state(CourseSetup.goal_value)
    await call.message.answer("Сколько календарных дней?" if gt=="days" else "Сколько выполнений?"); await call.answer()

@dp.message(CourseSetup.goal_value)
async def course_value(message: Message, state: FSMContext):
    try:
        value=int((message.text or "").strip()); assert value>0
    except: return await message.answer("Пришли положительное целое число.")
    data=await state.get_data(); cid=create_course(message.from_user.id,data["course_exercise_id"],data["course_days"],data["course_goal_type"],value); c=get_course(message.from_user.id,cid); await state.clear()
    await message.answer(f"Курс начат ✓\n\n<b>{c['title']}</b>\n📆 {schedule_text(c['schedule_days'])}\n📍 {course_progress(c)}",reply_markup=main_kb)


@dp.callback_query(F.data.startswith("restart:"))
async def restart(call: CallbackQuery, state: FSMContext):
    old=get_course(call.from_user.id,int(call.data.split(":")[1]))
    if not old: return await call.answer("Курс не найден",show_alert=True)
    days=parse_days(old["schedule_days"]); await state.clear(); await state.set_state(RestartSetup.days); await state.update_data(re_exercise_id=old["exercise_id"],re_days=days)
    await call.message.answer("Выбери дни нового курса:",reply_markup=days_kb(set(days),"re_")); await call.answer()

@dp.callback_query(RestartSetup.days, F.data.startswith("re_day:"))
async def re_day(call: CallbackQuery, state: FSMContext):
    n=int(call.data.split(":")[1]); data=await state.get_data(); selected=set(data.get("re_days",[])); selected.remove(n) if n in selected else selected.add(n)
    await state.update_data(re_days=sorted(selected)); await call.message.edit_reply_markup(reply_markup=days_kb(selected,"re_")); await call.answer()

@dp.callback_query(RestartSetup.days, F.data == "re_all")
async def re_all(call: CallbackQuery, state: FSMContext):
    selected=set(range(7)); await state.update_data(re_days=sorted(selected)); await call.message.edit_reply_markup(reply_markup=days_kb(selected,"re_")); await call.answer()

@dp.callback_query(RestartSetup.days, F.data == "re_done")
async def re_done(call: CallbackQuery, state: FSMContext):
    data=await state.get_data()
    if not data.get("re_days"): return await call.answer("Выбери хотя бы один день",show_alert=True)
    await state.set_state(RestartSetup.goal_type); await call.message.answer("Как считать новый курс?",reply_markup=goal_kb("re_")); await call.answer()

@dp.callback_query(RestartSetup.goal_type, F.data.startswith("re_goal:"))
async def re_goal(call: CallbackQuery, state: FSMContext):
    gt=call.data.split(":")[1]; await state.update_data(re_goal_type=gt); await state.set_state(RestartSetup.goal_value)
    await call.message.answer("Сколько календарных дней?" if gt=="days" else "Сколько выполнений?"); await call.answer()

@dp.message(RestartSetup.goal_value)
async def re_value(message: Message, state: FSMContext):
    try:
        value=int((message.text or "").strip()); assert value>0
    except: return await message.answer("Пришли положительное целое число.")
    data=await state.get_data(); cid=create_course(message.from_user.id,data["re_exercise_id"],data["re_days"],data["re_goal_type"],value); c=get_course(message.from_user.id,cid); await state.clear()
    await message.answer(f"Новый курс начат ✓\n\n<b>{c['title']}</b>\n📆 {schedule_text(c['schedule_days'])}\n📍 {course_progress(c)}",reply_markup=main_kb)


@dp.message(F.text == "Настройки")
async def settings(message: Message):
    await message.answer("Первая версия готова. Напоминания пока не включены — сначала проверяем основную логику курсов.")

@dp.message()
async def fallback(message: Message): await message.answer("Выбери действие кнопкой меню.",reply_markup=main_kb)


async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
