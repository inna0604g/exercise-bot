import asyncio
import os
import re
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
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db():
    with db() as conn:
        conn.executescript("""
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
            repeatable INTEGER NOT NULL DEFAULT 0 CHECK(repeatable IN (0,1)),
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
            FOREIGN KEY(course_id) REFERENCES courses(id) ON DELETE CASCADE
        );
        """)

        # Migration from the first version: add the per-course repeat flag.
        course_columns = {row["name"] for row in conn.execute("PRAGMA table_info(courses)")}
        if "repeatable" not in course_columns:
            conn.execute(
                "ALTER TABLE courses ADD COLUMN repeatable INTEGER NOT NULL DEFAULT 0 "
                "CHECK(repeatable IN (0,1))"
            )

        # Migration from the first version: it allowed only one mark per course/day.
        # The new version must allow several 'done' marks on the same date.
        marks_sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='course_marks'"
        ).fetchone()
        marks_sql = (marks_sql_row["sql"] or "") if marks_sql_row else ""
        normalized = re.sub(r"\s+", "", marks_sql.lower())
        if "unique(course_id,mark_date)" in normalized:
            conn.execute("DROP TABLE IF EXISTS course_marks_v2")
            conn.execute("""
                CREATE TABLE course_marks_v2 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    course_id INTEGER NOT NULL,
                    mark_date TEXT NOT NULL,
                    mark_type TEXT NOT NULL CHECK(mark_type IN ('done','skipped')),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(course_id) REFERENCES courses(id) ON DELETE CASCADE
                )
            """)
            conn.execute("""
                INSERT INTO course_marks_v2(id,user_id,course_id,mark_date,mark_type,created_at)
                SELECT id,user_id,course_id,mark_date,mark_type,created_at
                FROM course_marks
            """)
            conn.execute("DROP TABLE course_marks")
            conn.execute("ALTER TABLE course_marks_v2 RENAME TO course_marks")

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_course_marks_user_course_date "
            "ON course_marks(user_id, course_id, mark_date)"
        )


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


def create_course(user_id, exercise_id, schedule_days, goal_type, goal_value, repeatable=False):
    # A course may only be created for an exercise owned by this Telegram user.
    if not get_exercise(user_id, exercise_id):
        return None
    days = ",".join(str(x) for x in sorted(set(schedule_days)))
    with db() as conn:
        cur = conn.execute("""
            INSERT INTO courses(
                user_id,exercise_id,start_date,schedule_days,goal_type,goal_value,repeatable,status
            )
            VALUES(?,?,?,?,?,?,?,'active')
        """, (
            user_id, exercise_id, date.today().isoformat(), days,
            goal_type, goal_value, 1 if repeatable else 0,
        ))
        return cur.lastrowid


def get_course(user_id, course_id):
    with db() as conn:
        return conn.execute("""
            SELECT c.*, e.title, e.author, e.url, e.time_slot
            FROM courses c JOIN exercises e ON e.id=c.exercise_id AND e.user_id=c.user_id
            WHERE c.id=? AND c.user_id=?
        """, (course_id, user_id)).fetchone()


def list_courses(user_id, status):
    with db() as conn:
        return conn.execute("""
            SELECT c.*, e.title, e.author, e.url, e.time_slot,
              (SELECT COUNT(*) FROM course_marks m WHERE m.course_id=c.id AND m.mark_type='done') AS done_count
            FROM courses c JOIN exercises e ON e.id=c.exercise_id AND e.user_id=c.user_id
            WHERE c.user_id=? AND c.status=? ORDER BY c.created_at DESC, c.id DESC
        """, (user_id, status)).fetchall()


def done_count(user_id, course_id):
    with db() as conn:
        return conn.execute(
            "SELECT COUNT(*) n FROM course_marks "
            "WHERE user_id=? AND course_id=? AND mark_type='done'",
            (user_id, course_id),
        ).fetchone()["n"]


def done_count_today(user_id, course_id):
    with db() as conn:
        return conn.execute(
            "SELECT COUNT(*) n FROM course_marks "
            "WHERE user_id=? AND course_id=? AND mark_date=? AND mark_type='done'",
            (user_id, course_id, date.today().isoformat()),
        ).fetchone()["n"]


def skipped_today(user_id, course_id):
    with db() as conn:
        return conn.execute(
            "SELECT 1 FROM course_marks "
            "WHERE user_id=? AND course_id=? AND mark_date=? AND mark_type='skipped' LIMIT 1",
            (user_id, course_id, date.today().isoformat()),
        ).fetchone() is not None


def course_progress(user_id, c):
    if c["goal_type"] == "days":
        current = (date.today() - date.fromisoformat(c["start_date"])).days + 1
        current = max(1, min(current, c["goal_value"]))
        return f"День {current} из {c['goal_value']}"
    return f"Выполнено {done_count(user_id, c['id'])} из {c['goal_value']}"


def auto_complete(user_id):
    today = date.today()
    for c in list_courses(user_id, "active"):
        complete = False
        if c["goal_type"] == "days":
            last_day = date.fromisoformat(c["start_date"]) + timedelta(days=c["goal_value"] - 1)
            complete = today > last_day
        else:
            complete = done_count(user_id, c["id"]) >= c["goal_value"]
        if complete:
            with db() as conn:
                conn.execute(
                    "UPDATE courses SET status='completed', completed_at=? WHERE id=? AND user_id=?",
                    (today.isoformat(), c["id"], user_id),
                )


def scheduled_today(c, today):
    start = date.fromisoformat(c["start_date"])
    if today < start or today.weekday() not in parse_days(c["schedule_days"]):
        return False
    if c["goal_type"] == "days":
        last_day = start + timedelta(days=c["goal_value"] - 1)
        if today > last_day:
            return False
    return True


def today_sections(user_id):
    """Return (pending, repeatable_done) for the Today screen."""
    auto_complete(user_id)
    today = date.today()
    pending = []
    repeatable_done = []

    for c in list_courses(user_id, "active"):
        if not scheduled_today(c, today):
            continue
        if c["goal_type"] == "completions" and done_count(user_id, c["id"]) >= c["goal_value"]:
            continue
        if skipped_today(user_id, c["id"]):
            continue

        n_today = done_count_today(user_id, c["id"])
        if n_today == 0:
            pending.append(c)
        elif c["repeatable"]:
            repeatable_done.append(c)

    order = {name: i for i, name in enumerate(TIME_SLOTS)}
    key = lambda c: (order.get(c["time_slot"], 999), c["title"])
    pending.sort(key=key)
    repeatable_done.sort(key=key)
    return pending, repeatable_done


def save_done(user_id, course_id):
    # Several done marks may be stored on the same date for repeatable courses.
    c = get_course(user_id, course_id)
    if not c or c["status"] != "active":
        return False

    today = date.today()
    if not scheduled_today(c, today) or skipped_today(user_id, course_id):
        return False

    n_today = done_count_today(user_id, course_id)
    if n_today > 0 and not c["repeatable"]:
        return False

    with db() as conn:
        conn.execute(
            "INSERT INTO course_marks(user_id,course_id,mark_date,mark_type) VALUES(?,?,?,'done')",
            (user_id, course_id, today.isoformat()),
        )

    if c["goal_type"] == "completions" and done_count(user_id, course_id) >= c["goal_value"]:
        with db() as conn:
            conn.execute(
                "UPDATE courses SET status='completed', completed_at=? "
                "WHERE id=? AND user_id=?",
                (today.isoformat(), course_id, user_id),
            )
    return True


def save_skip(user_id, course_id):
    c = get_course(user_id, course_id)
    if not c or c["status"] != "active":
        return False
    today = date.today()
    if not scheduled_today(c, today):
        return False
    # Once a complex has been done today, the task is already closed;
    # 'skip' is only for an uncompleted task.
    if done_count_today(user_id, course_id) > 0 or skipped_today(user_id, course_id):
        return False
    with db() as conn:
        conn.execute(
            "INSERT INTO course_marks(user_id,course_id,mark_date,mark_type) VALUES(?,?,?,'skipped')",
            (user_id, course_id, today.isoformat()),
        )
    return True


def set_status(user_id, course_id, status):
    if status not in {"active", "paused", "completed"}:
        return False
    with db() as conn:
        if status == "completed":
            cur = conn.execute(
                "UPDATE courses SET status=?,completed_at=? WHERE id=? AND user_id=?",
                (status, date.today().isoformat(), course_id, user_id),
            )
        else:
            cur = conn.execute(
                "UPDATE courses SET status=?,completed_at=NULL WHERE id=? AND user_id=?",
                (status, course_id, user_id),
            )
        return cur.rowcount == 1


def delete_exercise(user_id, exercise_id):
    with db() as conn:
        cur = conn.execute("DELETE FROM exercises WHERE id=? AND user_id=?", (exercise_id, user_id))
        return cur.rowcount == 1


def schedule_text(value):
    nums = parse_days(value)
    return "ежедневно" if nums == list(range(7)) else ", ".join(NUM_TO_DAY[n] for n in nums)


def author_text(author):
    return author.strip() if author and author.strip() else "Автор не указан"


def repeatability_text(c):
    return "🔁 Можно несколько раз в день" if c["repeatable"] else "1️⃣ Один раз в день"


main_kb = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="Сегодня"), KeyboardButton(text="Мои курсы")],
    [KeyboardButton(text="Библиотека"), KeyboardButton(text="Добавить комплекс")],
    [KeyboardButton(text="Настройки")],
], resize_keyboard=True)


def today_kb(c):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Открыть видео", url=c["url"])],
        [
            InlineKeyboardButton(text="✓ Сделала", callback_data=f"done:{c['id']}"),
            InlineKeyboardButton(text="Пропустить сегодня", callback_data=f"skip:{c['id']}"),
        ],
    ])


def repeat_kb(c):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Открыть видео", url=c["url"])],
        [InlineKeyboardButton(text="✓ Сделала ещё раз", callback_data=f"repeatdone:{c['id']}")],
    ])


def course_kb(c):
    rows = [[InlineKeyboardButton(text="▶️ Видео", url=c["url"])]]
    if c["status"] == "active":
        toggle_label = "Только 1 раз в день" if c["repeatable"] else "Разрешить повторы"
        rows.append([InlineKeyboardButton(text=toggle_label, callback_data=f"toggle_repeat:{c['id']}")])
        rows.append([
            InlineKeyboardButton(text="⏸ Пауза", callback_data=f"pause:{c['id']}"),
            InlineKeyboardButton(text="✓ Завершить", callback_data=f"finish:{c['id']}"),
        ])
    elif c["status"] == "paused":
        toggle_label = "Только 1 раз в день" if c["repeatable"] else "Разрешить повторы"
        rows.append([InlineKeyboardButton(text=toggle_label, callback_data=f"toggle_repeat:{c['id']}")])
        rows.append([
            InlineKeyboardButton(text="▶ Возобновить", callback_data=f"resume:{c['id']}"),
            InlineKeyboardButton(text="✓ Завершить", callback_data=f"finish:{c['id']}"),
        ])
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


def repeat_choice_kb(prefix):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Да", callback_data=f"{prefix}repeat:yes"),
        InlineKeyboardButton(text="Нет", callback_data=f"{prefix}repeat:no"),
    ]])


class AddExercise(StatesGroup):
    url = State(); title = State(); author = State(); slot = State(); days = State(); after_save = State()

class CourseSetup(StatesGroup):
    days = State(); goal_type = State(); goal_value = State(); repeatable = State()

class RestartSetup(StatesGroup):
    days = State(); goal_type = State(); goal_value = State(); repeatable = State()


bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer("Привет. Я помогу выбирать комплексы на сегодня и проходить их курсами.", reply_markup=main_kb)


@dp.message(F.text == "Сегодня")
async def show_today(message: Message):
    pending, repeatable_done = today_sections(message.from_user.id)

    if not pending and not repeatable_done:
        await message.answer("На сегодня больше ничего не запланировано ✓", reply_markup=main_kb)
        return

    if pending:
        await message.answer(f"<b>Осталось сегодня: {len(pending)}</b>")
        last_slot = None
        for c in pending:
            if c["time_slot"] != last_slot:
                await message.answer(f"<b>{c['time_slot']}</b>")
                last_slot = c["time_slot"]
            await message.answer(
                f"<b>{c['title']}</b>\n"
                f"{author_text(c['author'])}\n"
                f"📆 {schedule_text(c['schedule_days'])}\n"
                f"📍 {course_progress(message.from_user.id, c)}",
                reply_markup=today_kb(c),
            )
    else:
        await message.answer("<b>Все обязательные комплексы на сегодня выполнены ✓</b>")

    if repeatable_done:
        await message.answer("<b>Можно повторить</b>")
        for c in repeatable_done:
            n_today = done_count_today(message.from_user.id, c["id"])
            await message.answer(
                f"<b>{c['title']}</b>\n"
                f"{author_text(c['author'])}\n"
                f"✓ Сегодня: {n_today} раз\n"
                f"📍 {course_progress(message.from_user.id, c)}",
                reply_markup=repeat_kb(c),
            )


@dp.callback_query(F.data.startswith("done:"))
async def done(call: CallbackQuery):
    cid = int(call.data.split(":")[1])
    c = get_course(call.from_user.id, cid)
    if not c:
        return await call.answer("Курс не найден", show_alert=True)
    if not save_done(call.from_user.id, cid):
        return await call.answer("Сегодня это выполнение уже учтено", show_alert=True)

    c = get_course(call.from_user.id, cid)
    await call.message.edit_reply_markup(reply_markup=None)
    if c["status"] == "completed":
        await call.message.answer("✓ Сделано. Курс завершён.")
    elif c["repeatable"]:
        await call.message.answer(
            f"✓ Сделано. Задача на сегодня закрыта. "
            f"При желании комплекс можно повторить. Сегодня: {done_count_today(call.from_user.id, cid)} раз."
        )
    else:
        await call.message.answer(f"✓ Сделано. {course_progress(call.from_user.id, c)}")
    await call.answer()


@dp.callback_query(F.data.startswith("repeatdone:"))
async def repeat_done(call: CallbackQuery):
    cid = int(call.data.split(":")[1])
    c = get_course(call.from_user.id, cid)
    if not c or not c["repeatable"]:
        return await call.answer("Повторы для этого курса не включены", show_alert=True)
    if not save_done(call.from_user.id, cid):
        return await call.answer("Не удалось записать выполнение", show_alert=True)

    c = get_course(call.from_user.id, cid)
    if c["status"] == "completed":
        await call.message.edit_reply_markup(reply_markup=None)
        await call.message.answer("✓ Выполнение учтено. Курс завершён.")
    else:
        n_today = done_count_today(call.from_user.id, cid)
        await call.message.edit_text(
            f"<b>{c['title']}</b>\n"
            f"{author_text(c['author'])}\n"
            f"✓ Сегодня: {n_today} раз\n"
            f"📍 {course_progress(call.from_user.id, c)}",
            reply_markup=repeat_kb(c),
        )
    await call.answer("Выполнение учтено ✓")


@dp.callback_query(F.data.startswith("skip:"))
async def skip(call: CallbackQuery):
    cid = int(call.data.split(":")[1])
    if not save_skip(call.from_user.id, cid):
        return await call.answer("Пропуск сейчас не применим", show_alert=True)
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
            await message.answer(
                f"<b>{c['title']}</b>\n"
                f"{author_text(c['author'])}\n"
                f"🕒 {c['time_slot']}\n"
                f"📆 {schedule_text(c['schedule_days'])}\n"
                f"{repeatability_text(c)}\n"
                f"📍 {course_progress(message.from_user.id, c)}",
                reply_markup=course_kb(c),
            )
    if not found: await message.answer("Курсов пока нет.")


@dp.callback_query(F.data.startswith("toggle_repeat:"))
async def toggle_repeat(call: CallbackQuery):
    cid = int(call.data.split(":")[1])
    c = get_course(call.from_user.id, cid)
    if not c or c["status"] not in {"active", "paused"}:
        return await call.answer("Курс не найден", show_alert=True)
    new_value = 0 if c["repeatable"] else 1
    with db() as conn:
        conn.execute(
            "UPDATE courses SET repeatable=? WHERE id=? AND user_id=?",
            (new_value, cid, call.from_user.id),
        )
    await call.answer("Повторы включены" if new_value else "Теперь только 1 раз в день")
    await call.message.answer(
        "Настройка курса изменена: "
        + ("можно выполнять несколько раз в день." if new_value else "одно выполнение в день.")
    )


@dp.callback_query(F.data.startswith("pause:"))
async def pause(call: CallbackQuery):
    cid = int(call.data.split(":")[1])
    if not set_status(call.from_user.id, cid, "paused"):
        return await call.answer("Курс не найден", show_alert=True)
    await call.message.answer("Курс поставлен на паузу.")
    await call.answer()

@dp.callback_query(F.data.startswith("resume:"))
async def resume(call: CallbackQuery):
    cid = int(call.data.split(":")[1])
    if not set_status(call.from_user.id, cid, "active"):
        return await call.answer("Курс не найден", show_alert=True)
    await call.message.answer("Курс снова активен.")
    await call.answer()

@dp.callback_query(F.data.startswith("finish:"))
async def finish(call: CallbackQuery):
    cid = int(call.data.split(":")[1])
    if not set_status(call.from_user.id, cid, "completed"):
        return await call.answer("Курс не найден", show_alert=True)
    await call.message.answer("Курс завершён и сохранён в истории.")
    await call.answer()


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
async def delete_yes(call: CallbackQuery):
    eid = int(call.data.split(":")[1])
    if not delete_exercise(call.from_user.id, eid):
        return await call.answer("Комплекс не найден", show_alert=True)
    await call.message.answer("Удалено из библиотеки.")
    await call.answer()
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
    eid = int(call.data.split(":")[1])
    if not get_exercise(call.from_user.id, eid):
        return await call.answer("Комплекс не найден", show_alert=True)
    await state.clear()
    await state.set_state(CourseSetup.days)
    await state.update_data(course_exercise_id=eid, course_days=[])
    await call.message.answer("В какие дни выполнять этот курс?", reply_markup=days_kb(set(), "course_"))
    await call.answer()

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
        value = int((message.text or "").strip())
        assert value > 0
    except Exception:
        return await message.answer("Пришли положительное целое число.")
    await state.update_data(course_goal_value=value)
    await state.set_state(CourseSetup.repeatable)
    await message.answer(
        "Можно выполнять этот комплекс несколько раз в один день?",
        reply_markup=repeat_choice_kb("course_"),
    )


@dp.callback_query(CourseSetup.repeatable, F.data.startswith("course_repeat:"))
async def course_repeat(call: CallbackQuery, state: FSMContext):
    repeatable = call.data.endswith(":yes")
    data = await state.get_data()
    cid = create_course(
        call.from_user.id,
        data["course_exercise_id"],
        data["course_days"],
        data["course_goal_type"],
        data["course_goal_value"],
        repeatable,
    )
    if not cid:
        await state.clear()
        return await call.message.answer(
            "Не удалось начать курс: комплекс не найден.", reply_markup=main_kb
        )
    c = get_course(call.from_user.id, cid)
    await state.clear()
    await call.message.answer(
        f"Курс начат ✓\n\n"
        f"<b>{c['title']}</b>\n"
        f"📆 {schedule_text(c['schedule_days'])}\n"
        f"{repeatability_text(c)}\n"
        f"📍 {course_progress(call.from_user.id, c)}",
        reply_markup=main_kb,
    )
    await call.answer()


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
        value = int((message.text or "").strip())
        assert value > 0
    except Exception:
        return await message.answer("Пришли положительное целое число.")
    await state.update_data(re_goal_value=value)
    await state.set_state(RestartSetup.repeatable)
    await message.answer(
        "Можно выполнять этот комплекс несколько раз в один день?",
        reply_markup=repeat_choice_kb("re_"),
    )


@dp.callback_query(RestartSetup.repeatable, F.data.startswith("re_repeat:"))
async def re_repeat(call: CallbackQuery, state: FSMContext):
    repeatable = call.data.endswith(":yes")
    data = await state.get_data()
    cid = create_course(
        call.from_user.id,
        data["re_exercise_id"],
        data["re_days"],
        data["re_goal_type"],
        data["re_goal_value"],
        repeatable,
    )
    if not cid:
        await state.clear()
        return await call.message.answer(
            "Не удалось начать новый курс: комплекс не найден.", reply_markup=main_kb
        )
    c = get_course(call.from_user.id, cid)
    await state.clear()
    await call.message.answer(
        f"Новый курс начат ✓\n\n"
        f"<b>{c['title']}</b>\n"
        f"📆 {schedule_text(c['schedule_days'])}\n"
        f"{repeatability_text(c)}\n"
        f"📍 {course_progress(call.from_user.id, c)}",
        reply_markup=main_kb,
    )
    await call.answer()


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
