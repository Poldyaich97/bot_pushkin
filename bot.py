import os
import logging
import sqlite3
from datetime import datetime, time
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore
from dotenv import load_dotenv
from telegram import Update, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackContext,
    CallbackQueryHandler,
    ContextTypes,
)

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Константы
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
MAIN_ADMIN_ID = int(os.getenv('MAIN_ADMIN_ID'))
GROUP_ID = int(os.getenv('GROUP_ID'))
FORCE_REGISTRATION_CODE = "2512"
BOT_TIMEZONE = os.getenv('BOT_TIMEZONE', 'Europe/Moscow')

try:
    BOT_TZINFO = ZoneInfo(BOT_TIMEZONE)
except Exception as tz_error:  # noqa: F841
    BOT_TZINFO = datetime.now().astimezone().tzinfo
    logger.warning(
        "Unable to load timezone '%s', fallback to system tz %s",
        BOT_TIMEZONE,
        BOT_TZINFO,
    )

# Диапазоны квартир
HOUSE1_START = 1
HOUSE1_END = 252
HOUSE2_START = 253
HOUSE2_END = 403


def sanitize_markdown(text: str) -> str:
    """Удаление символов, конфликтующих с Markdown-разметкой."""
    if not text:
        return ""
    return (
        text.replace('[', '')
        .replace(']', '')
        .replace('(', '')
        .replace(')', '')
        .replace('_', '')
        .replace('*', '')
    )


def format_user_mention(user) -> str:
    """Формирование безопасного упоминания пользователя."""
    if user.username:
        safe_username = user.username.replace('_', '\\_')
        return f"@{safe_username}"
    display_name = sanitize_markdown(user.first_name or "пользователь")
    if not display_name:
        display_name = "пользователь"
    return f"[{display_name}](tg://user?id={user.id})"


def is_admin_user(user_id: int) -> bool:
    """Проверка наличия прав администратора."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
        return cursor.fetchone() is not None

def get_db_connection():
    """Создание подключения к базе данных"""
    return sqlite3.connect('apartments.db')

def is_valid_apartment(apartment_number: int) -> bool:
    """Проверка валидности номера квартиры"""
    return (HOUSE1_START <= apartment_number <= HOUSE1_END) or \
           (HOUSE2_START <= apartment_number <= HOUSE2_END)


def clear_pending_requests_from_db() -> int:
    """Удаляет все ожидающие запросы на подтверждение и возвращает их количество."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM approval_requests WHERE status = 'pending'")
        deleted = cursor.rowcount or 0
        conn.commit()
    return deleted


def get_admin_actions_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура с быстрыми действиями администратора."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧹 Очистить заявки", callback_data="admin_clear_requests")]
    ])

def create_db():
    """Создание базы данных"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # Таблица квартир и их жильцов
        cursor.execute('''CREATE TABLE IF NOT EXISTS apartments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            apartment_number INTEGER,
            user_id INTEGER,
            registration_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(apartment_number, user_id)
        )''')
        
        # Таблица администраторов
        cursor.execute('''CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            added_by INTEGER,
            added_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        # Таблица запросов на подтверждение
        cursor.execute('''CREATE TABLE IF NOT EXISTS approval_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            apartment_number INTEGER,
            requesting_user_id INTEGER,
            approver_user_id INTEGER,
            status TEXT DEFAULT 'pending',
            request_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        # Добавляем главного админа
        cursor.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (MAIN_ADMIN_ID,))
        conn.commit()
    logger.info("Database created successfully")

async def start(update: Update, context: CallbackContext) -> None:
    """Обработчик команды /start"""
    try:
        user_id = update.message.from_user.id
        chat_id = update.message.chat.id
        
        if chat_id != GROUP_ID:
            await update.message.reply_text(
                "Бот работает только в основной группе дома."
            )
            return

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT apartment_number FROM apartments WHERE user_id = ?", (user_id,))
            apartment = cursor.fetchone()

        if not apartment:
            await update.message.reply_text(
                "Пожалуйста, укажите номер своей квартиры с помощью команды:\n"
                "/setapartment [номер]\n\n"
                "Например: /setapartment 100\n\n"
                f"Дом 1: квартиры {HOUSE1_START}-{HOUSE1_END}\n"
                f"Дом 2: квартиры от {HOUSE2_START}"
            )
        else:
            await update.message.reply_text(
                f"Вы уже зарегистрированы в квартире {apartment[0]}."
            )
    except Exception as e:
        logger.error(f"Error in start command: {e}")
        await update.message.reply_text("Произошла ошибка при обработке команды.")

async def handle_message(update: Update, context: CallbackContext) -> None:
    """Обработчик всех сообщений"""
    if not update.message or not update.message.from_user:
        return

    user_id = update.message.from_user.id
    user_mention = format_user_mention(update.message.from_user)
    
    # Пропускаем сообщения от администраторов
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM admins WHERE user_id = ?", (user_id,))
        if cursor.fetchone():
            return

        # Проверяем регистрацию пользователя
        cursor.execute("SELECT apartment_number FROM apartments WHERE user_id = ?", (user_id,))
        apartment = cursor.fetchone()
        
        if not apartment:
            try:
                # Удаляем сообщение
                await update.message.delete()
                # Отправляем уведомление
                await context.bot.send_message(
                    chat_id=GROUP_ID,
                    text=f"⚠️ {user_mention}, пожалуйста, укажите номер своей квартиры с помощью команды:\n"
                         f"/setapartment [номер]\n\n"
                         f"Например: /setapartment 100",
                    parse_mode='Markdown'
                )
            except Exception as e:
                logger.error(f"Error handling unregistered user message: {e}")

async def request_apartment_access(update: Update, context: CallbackContext) -> None:
    """Запрос на привязку к квартире"""
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Укажите номер квартиры.")
        return

    apartment_number = int(context.args[0])
    requesting_user = update.message.from_user
    requesting_user_id = requesting_user.id
    
    # Безопасное форматирование имени пользователя
    if requesting_user.username:
        user_mention = f"@{requesting_user.username}"
    else:
        user_mention = sanitize_markdown(requesting_user.first_name or "пользователь")

    if not is_valid_apartment(apartment_number):
        await update.message.reply_text(
            "Неверный номер квартиры.\n"
            f"Дом 1: квартиры {HOUSE1_START}-{HOUSE1_END}\n"
            f"Дом 2: квартиры от {HOUSE2_START}"
        )
        return

    with get_db_connection() as conn:
        cursor = conn.cursor()
        # Проверяем, есть ли уже жильцы в этой квартире
        cursor.execute(
            "SELECT user_id FROM apartments WHERE apartment_number = ?",
            (apartment_number,)
        )
        existing_residents = cursor.fetchall()

        if existing_residents:
            # Создаем запрос на подтверждение
            existing_user_id = existing_residents[0][0]
            cursor.execute("""
                INSERT INTO approval_requests (apartment_number, requesting_user_id, approver_user_id)
                VALUES (?, ?, ?)
            """, (apartment_number, requesting_user_id, existing_user_id))
            conn.commit()
            
            try:
                existing_user = await context.bot.get_chat_member(GROUP_ID, existing_user_id)
                if existing_user.user.username:
                    existing_user_mention = f"@{existing_user.user.username}"
                else:
                    existing_user_mention = sanitize_markdown(existing_user.user.first_name or "житель")
                
                notification_text = (
                    f"{user_mention} запросил доступ к квартире {apartment_number}.\n"
                    f"{existing_user_mention}, для подтверждения используйте команду:\n"
                    f"/approve {requesting_user_id}\n"
                    f"Для отказа:\n"
                    f"/reject {requesting_user_id}"
                )
                
                await context.bot.send_message(
                    chat_id=GROUP_ID,
                    text=notification_text
                )
            except Exception as e:
                logger.error(f"Error getting existing resident info: {e}")
                await update.message.reply_text("Произошла ошибка при обработке запроса.")
        else:
            cursor.execute("""
                INSERT INTO apartments (apartment_number, user_id)
                VALUES (?, ?)
            """, (apartment_number, requesting_user_id))
            conn.commit()
            
            success_message = f"Пользователь {user_mention} успешно привязан к квартире {apartment_number}"
            
            await context.bot.send_message(
                chat_id=GROUP_ID,
                text=success_message
            )

async def approve_request(update: Update, context: CallbackContext) -> None:
    """Подтверждение запроса на привязку к квартире"""
    try:
        if not context.args:
            await update.message.reply_text("Укажите ID пользователя.")
            return

        approver_id = update.message.from_user.id
        requesting_user_id = int(context.args[0])

        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # Сначала проверяем существование запроса и получаем номер квартиры
            cursor.execute("""
                SELECT apartment_number 
                FROM approval_requests 
                WHERE requesting_user_id = ? 
                AND approver_user_id = ? 
                AND status = 'pending'
            """, (requesting_user_id, approver_id))
            request = cursor.fetchone()

            if not request:
                await update.message.reply_text("Запрос на подтверждение не найден или уже обработан.")
                return

            apartment_number = request[0]

            # Проверяем, не зарегистрирован ли уже пользователь в другой квартире
            cursor.execute("""
                SELECT apartment_number 
                FROM apartments 
                WHERE user_id = ?
            """, (requesting_user_id,))
            existing_apartment = cursor.fetchone()

            if existing_apartment:
                await update.message.reply_text(
                    f"Пользователь уже зарегистрирован в квартире {existing_apartment[0]}. "
                    "Сначала нужно удалить старую привязку."
                )
                return

            # Добавляем нового жильца
            try:
                cursor.execute("""
                    INSERT INTO apartments (apartment_number, user_id)
                    VALUES (?, ?)
                """, (apartment_number, requesting_user_id))
            except sqlite3.IntegrityError:
                await update.message.reply_text("Ошибка: пользователь уже привязан к этой квартире.")
                return

            # Обновляем статус запроса
            cursor.execute("""
                UPDATE approval_requests 
                SET status = 'approved' 
                WHERE requesting_user_id = ? 
                AND approver_user_id = ? 
                AND status = 'pending'
            """, (requesting_user_id, approver_id))
            
            conn.commit()

            # Отправляем уведомление
            try:
                requesting_user = await context.bot.get_chat_member(GROUP_ID, requesting_user_id)
                if requesting_user.user.username:
                    user_mention = f"@{requesting_user.user.username}"
                else:
                    user_mention = sanitize_markdown(requesting_user.user.first_name or "пользователь")
                
                success_message = f"✅ Пользователь {user_mention} получил доступ к квартире {apartment_number}"
                
                await context.bot.send_message(
                    chat_id=GROUP_ID,
                    text=success_message
                )
            except Exception as e:
                logger.error(f"Error sending approval notification: {e}")
                await update.message.reply_text(
                    "Пользователь добавлен, но возникла ошибка при отправке уведомления."
                )

    except Exception as e:
        logger.error(f"Error in approve_request: {e}")
        await update.message.reply_text(
            "Произошла ошибка при обработке запроса. "
            "Пожалуйста, попробуйте позже или обратитесь к администратору."
        )

async def reject_request(update: Update, context: CallbackContext) -> None:
    """Отклонение запроса на привязку к квартире"""
    if not context.args:
        await update.message.reply_text("Укажите ID пользователя.")
        return

    approver_id = update.message.from_user.id
    requesting_user_id = int(context.args[0])

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT apartment_number 
            FROM approval_requests 
            WHERE requesting_user_id = ? AND approver_user_id = ? AND status = 'pending'
        """, (requesting_user_id, approver_id))
        request = cursor.fetchone()
        
        if request:
            apartment_number = request[0]
            cursor.execute("""
                UPDATE approval_requests 
                SET status = 'rejected' 
                WHERE requesting_user_id = ? AND approver_user_id = ?
            """, (requesting_user_id, approver_id))
            conn.commit()

            try:
                requesting_user = await context.bot.get_chat_member(GROUP_ID, requesting_user_id)
                if requesting_user.user.username:
                    user_mention = f"@{requesting_user.user.username}"
                else:
                    user_mention = sanitize_markdown(requesting_user.user.first_name or "пользователь")
                
                reject_message = f"❌ Запрос от {user_mention} на доступ к квартире {apartment_number} отклонен"
                
                await context.bot.send_message(
                    chat_id=GROUP_ID,
                    text=reject_message
                )
            except Exception as e:
                logger.error(f"Error in reject_request: {e}")
                await update.message.reply_text("Произошла ошибка при отклонении запроса.")
        else:
            await update.message.reply_text("Запрос на подтверждение не найден или уже обработан.")

async def force_registration(update: Update, context: CallbackContext) -> None:
    """Очистка базы и запрос регистрации у всех участников"""
    user_id = update.message.from_user.id
    if user_id != MAIN_ADMIN_ID:
        await update.message.reply_text("Эта команда доступна только главному администратору.")
        return

    if not context.args or context.args[0] != FORCE_REGISTRATION_CODE:
        await update.message.reply_text(
            "Для запуска перерегистрации укажите секретный код: /forceregistration 2512"
        )
        return

    # Очищаем базу данных
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM apartments")
        cursor.execute("DELETE FROM approval_requests")
        conn.commit()

    await update.message.reply_text("База данных очищена. Начинаю процесс перерегистрации...")

    try:
        # Отправляем общее сообщение
        initial_message = (
            "🔄 Запущен процесс перерегистрации!\n\n"
            "Всем участникам чата необходимо заново указать номер своей квартиры.\n"
            "До указания номера квартиры сообщения будут удаляться.\n\n"
            "Используйте команду /setapartment [номер]\n"
            "Например: /setapartment 100\n\n"
            f"Дом 1: квартиры {HOUSE1_START}-{HOUSE1_END}\n"
            f"Дом 2: квартиры от {HOUSE2_START}"
        )
        
        await context.bot.send_message(
            chat_id=GROUP_ID,
            text=initial_message,
            parse_mode='Markdown'
        )

        await update.message.reply_text(
            "Процесс перерегистрации запущен!\n"
            "Все участники должны заново указать номер квартиры.\n"
            "При отправке сообщения в чат, участники без регистрации "
            "получат уведомление о необходимости указать номер квартиры."
        )

    except Exception as e:
        logger.error(f"Error during force registration: {e}")
        await update.message.reply_text("Произошла ошибка при запуске перерегистрации.")

async def delete_apartment(update: Update, context: CallbackContext) -> None:
    """Удаление привязки к квартире"""
    user_id = update.message.from_user.id
    user = update.message.from_user
    user_mention = format_user_mention(user)

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT apartment_number FROM apartments WHERE user_id = ?", (user_id,))
        apartment = cursor.fetchone()

        if apartment:
            cursor.execute("DELETE FROM apartments WHERE user_id = ?", (user_id,))
            conn.commit()
            
            await context.bot.send_message(
                chat_id=GROUP_ID,
                text=f"🗑 {user_mention} удалил привязку к квартире {apartment[0]}",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text("У вас нет привязанной квартиры.")


async def admin_unlink(update: Update, context: CallbackContext) -> None:
    """Удаление привязки пользователя администратором."""
    actor_id = update.message.from_user.id
    if not is_admin_user(actor_id):
        await update.message.reply_text("Эта команда доступна только администраторам.")
        return

    if not context.args:
        await update.message.reply_text(
            "Использование: /adminunlink [user_id] [номер_квартиры]\n"
            "Если номер квартиры не указан, будут удалены все привязки пользователя."
        )
        return

    try:
        target_user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Первым параметром укажите числовой ID пользователя.")
        return

    apartment_number = None
    if len(context.args) > 1:
        if not context.args[1].isdigit():
            await update.message.reply_text("Номер квартиры должен быть числом.")
            return
        apartment_number = int(context.args[1])

    with get_db_connection() as conn:
        cursor = conn.cursor()
        if apartment_number is not None:
            cursor.execute(
                """
                SELECT apartment_number
                FROM apartments
                WHERE user_id = ? AND apartment_number = ?
                """,
                (target_user_id, apartment_number)
            )
        else:
            cursor.execute(
                """
                SELECT apartment_number
                FROM apartments
                WHERE user_id = ?
                """,
                (target_user_id,)
            )
        apartments = cursor.fetchall()

        if not apartments:
            await update.message.reply_text("Для указанного пользователя привязки не найдены.")
            return

        if apartment_number is not None:
            cursor.execute(
                "DELETE FROM apartments WHERE user_id = ? AND apartment_number = ?",
                (target_user_id, apartment_number)
            )
        else:
            cursor.execute(
                "DELETE FROM apartments WHERE user_id = ?",
                (target_user_id,)
            )
        conn.commit()

    removed_apartments = ", ".join(str(item[0]) for item in apartments)

    try:
        chat_member = await context.bot.get_chat_member(GROUP_ID, target_user_id)
        target_mention = format_user_mention(chat_member.user)
    except Exception as error:
        logger.warning(f"Failed to load chat member {target_user_id} for admin unlink: {error}")
        target_mention = f"ID: {target_user_id}"

    confirmation_text = (
        f"Привязка пользователя {target_mention} к квартире(ам) {removed_apartments} удалена."
    )

    await update.message.reply_text(confirmation_text, parse_mode='Markdown')

    try:
        await context.bot.send_message(
            chat_id=GROUP_ID,
            text=f"🗑 Администратор снял привязку {target_mention} с квартиры(квартир) {removed_apartments}",
            parse_mode='Markdown'
        )
    except Exception as error:
        logger.warning(f"Failed to notify group about admin unlink: {error}")


async def admin_delete_apartment(update: Update, context: CallbackContext) -> None:
    """Удаление записи о квартире по номеру."""
    actor_id = update.message.from_user.id
    if not is_admin_user(actor_id):
        await update.message.reply_text("Эта команда доступна только администраторам.")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            "Использование: /admindelete [номер_квартиры]\n"
            "Команда полностью освобождает квартиру для самостоятельной регистрации."
        )
        return

    apartment_number = int(context.args[0])
    if not is_valid_apartment(apartment_number):
        await update.message.reply_text(
            "Неверный номер квартиры.\n"
            f"Дом 1: квартиры {HOUSE1_START}-{HOUSE1_END}\n"
            f"Дом 2: квартиры от {HOUSE2_START}"
        )
        return

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT user_id FROM apartments WHERE apartment_number = ?",
            (apartment_number,)
        )
        residents = [row[0] for row in cursor.fetchall()]

        if not residents:
            await update.message.reply_text(
                f"Для квартиры {apartment_number} нет записей. Она уже свободна."
            )
            return

        cursor.execute(
            "DELETE FROM apartments WHERE apartment_number = ?",
            (apartment_number,)
        )
        conn.commit()

    resident_mentions = []
    for resident_id in residents:
        try:
            member = await context.bot.get_chat_member(GROUP_ID, resident_id)
            resident_mentions.append(format_user_mention(member.user))
        except Exception as error:
            logger.warning(f"Failed to load resident {resident_id} for admindelete: {error}")
            resident_mentions.append(f"ID: {resident_id}")

    removed_info = ", ".join(resident_mentions)
    await update.message.reply_text(
        f"Запись о квартире {apartment_number} удалена. Удалены жильцы: {removed_info}",
        parse_mode='Markdown'
    )

    try:
        await context.bot.send_message(
            chat_id=GROUP_ID,
            text=(
                f"🧹 Администратор освободил квартиру {apartment_number}. "
                "Теперь любой житель может привязать её через /setapartment."
            )
        )
    except Exception as error:
        logger.warning(f"Failed to notify group about apartment delete: {error}")


async def clear_approval_requests(update: Update, context: CallbackContext) -> None:
    """Очистка всех ожидающих запросов на подтверждение квартир."""
    actor_id = update.message.from_user.id
    if not is_admin_user(actor_id):
        await update.message.reply_text("Эта команда доступна только администраторам.")
        return

    deleted = clear_pending_requests_from_db()
    if deleted == 0:
        await update.message.reply_text("Нет заявок в ожидании. Очищать нечего.")
    else:
        await update.message.reply_text(f"🧹 Удалено запросов: {deleted}. Очередь очищена.")


async def apartment_stats(update: Update, context: CallbackContext) -> None:
    """Вывод количества занятых и свободных квартир."""
    actor_id = update.message.from_user.id
    if not is_admin_user(actor_id):
        await update.message.reply_text("Эта команда доступна только администраторам.")
        return

    total_house1 = HOUSE1_END - HOUSE1_START + 1
    total_house2 = HOUSE2_END - HOUSE2_START + 1
    total_all = total_house1 + total_house2

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT apartment_number FROM apartments")
        occupied_numbers = [row[0] for row in cursor.fetchall()]

    occupied_house1 = sum(1 for number in occupied_numbers if HOUSE1_START <= number <= HOUSE1_END)
    occupied_house2 = sum(1 for number in occupied_numbers if HOUSE2_START <= number <= HOUSE2_END)
    occupied_all = occupied_house1 + occupied_house2
    other_occupied = len(occupied_numbers) - occupied_all

    free_house1 = max(total_house1 - occupied_house1, 0)
    free_house2 = max(total_house2 - occupied_house2, 0)
    free_all = max(total_all - occupied_all, 0)

    lines = [
        "📊 Статистика квартир:",
        f"Дом 1: занято {occupied_house1}/{total_house1}, свободно {free_house1}",
        f"Дом 2: занято {occupied_house2}/{total_house2}, свободно {free_house2}",
        f"Всего: занято {occupied_all}/{total_all}, свободно {free_all}",
    ]

    if other_occupied > 0:
        lines.append(f"⚠️ Есть {other_occupied} записей вне указанных диапазонов квартир.")

    await update.message.reply_text("\n".join(lines))


async def handle_admin_callback(update: Update, context: CallbackContext) -> None:
    """Обработка нажатий админских кнопок."""
    query = update.callback_query
    if not query or not query.data:
        return

    data = query.data
    actor_id = query.from_user.id

    if data == "admin_clear_requests":
        if not is_admin_user(actor_id):
            await query.answer("Недостаточно прав.", show_alert=True)
            return

        deleted = clear_pending_requests_from_db()
        await query.answer("Очередь очищена" if deleted else "Нет заявок")

        if deleted == 0:
            await query.message.reply_text("Нет заявок в ожидании. Очередь уже пуста.")
        else:
            await query.message.reply_text(f"🧹 Удалено запросов: {deleted}. Очередь очищена.")

async def send_morning_greeting(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ежедневное утреннее сообщение для всех жителей."""
    try:
        await context.bot.send_message(
            chat_id=GROUP_ID,
            text="Доброе утро, соседи.\nВсем хорошего дня"
        )
    except Exception as error:
        logger.error(f"Failed to send morning greeting: {error}")


async def send_evening_greeting(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ежедневное вечернее сообщение для всех жителей."""
    try:
        await context.bot.send_message(
            chat_id=GROUP_ID,
            text="Доброй ночи, соседи."
        )
    except Exception as error:
        logger.error(f"Failed to send evening greeting: {error}")


async def admin_assign(update: Update, context: CallbackContext) -> None:
    """Переназначение квартиры на указанного пользователя."""
    actor_id = update.message.from_user.id
    if not is_admin_user(actor_id):
        await update.message.reply_text("Эта команда доступна только администраторам.")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Использование: /adminassign [номер_квартиры] [user_id]\n"
            "Перед назначением текущие привязки квартиры и пользователя будут удалены."
        )
        return

    try:
        apartment_number = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Номер квартиры должен быть числом.")
        return

    try:
        target_user_id = int(context.args[1])
    except ValueError:
        await update.message.reply_text("ID пользователя должен быть числом.")
        return

    if not is_valid_apartment(apartment_number):
        await update.message.reply_text(
            "Неверный номер квартиры.\n"
            f"Дом 1: квартиры {HOUSE1_START}-{HOUSE1_END}\n"
            f"Дом 2: квартиры от {HOUSE2_START}"
        )
        return

    with get_db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute(
            "SELECT user_id FROM apartments WHERE apartment_number = ?",
            (apartment_number,)
        )
        previous_residents = [row[0] for row in cursor.fetchall()]

        cursor.execute(
            "SELECT apartment_number FROM apartments WHERE user_id = ?",
            (target_user_id,)
        )
        previous_apartments = [row[0] for row in cursor.fetchall()]

        cursor.execute(
            "DELETE FROM apartments WHERE apartment_number = ?",
            (apartment_number,)
        )
        cursor.execute(
            "DELETE FROM apartments WHERE user_id = ?",
            (target_user_id,)
        )
        cursor.execute(
            """
            INSERT INTO apartments (apartment_number, user_id)
            VALUES (?, ?)
            """,
            (apartment_number, target_user_id)
        )
        cursor.execute(
            """
            UPDATE approval_requests
            SET status = 'approved', approver_user_id = ?
            WHERE apartment_number = ?
              AND requesting_user_id = ?
              AND status = 'pending'
            """,
            (actor_id, apartment_number, target_user_id)
        )
        conn.commit()

    try:
        new_resident = await context.bot.get_chat_member(GROUP_ID, target_user_id)
        target_mention = format_user_mention(new_resident.user)
    except Exception as error:
        logger.warning(f"Failed to load chat member {target_user_id} for admin assign: {error}")
        target_mention = f"ID: {target_user_id}"

    removed_from_apartment = ", ".join(str(user_id) for user_id in previous_residents) if previous_residents else "нет"
    removed_from_user = ", ".join(str(number) for number in previous_apartments) if previous_apartments else "нет"

    response_lines = [
        f"Пользователь {target_mention} назначен на квартиру {apartment_number}.",
        f"С квартиры удалены прежние жильцы: {removed_from_apartment}.",
        f"У пользователя удалены прежние квартиры: {removed_from_user}."
    ]

    await update.message.reply_text("\n".join(response_lines), parse_mode='Markdown')

    try:
        await context.bot.send_message(
            chat_id=GROUP_ID,
            text=f"🏠 Квартира {apartment_number} закреплена за {target_mention} администратором.",
            parse_mode='Markdown'
        )
    except Exception as error:
        logger.warning(f"Failed to notify group about admin assign: {error}")

async def view_apartments(update: Update, context: CallbackContext) -> None:
    """Просмотр списка квартир и их жильцов"""
    user_id = update.message.from_user.id
    
    # Проверяем, является ли пользователь администратором
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM admins WHERE user_id = ?", (user_id,))
        if not cursor.fetchone():
            await update.message.reply_text("Эта команда доступна только администраторам.")
            return

        # Получаем список всех квартир и жильцов
        cursor.execute("""
            SELECT apartment_number, user_id 
            FROM apartments 
            ORDER BY apartment_number
        """)
        apartments = cursor.fetchall()

    if not apartments:
        await update.message.reply_text("Список квартир пуст.")
        return

    # Формируем сообщения по домам
    house1_msg = "🏢 Дом 1:\n"
    house2_msg = "🏢 Дом 2:\n"
    
    try:
        for apartment in apartments:
            apartment_number = apartment[0]
            user_id = apartment[1]
            
            try:
                user = await context.bot.get_chat_member(GROUP_ID, user_id)
                # Безопасное форматирование имени пользователя
                if user.user.username:
                    user_info = f"@{user.user.username}"
                else:
                    user_info = sanitize_markdown(user.user.first_name or "пользователь")
            except Exception as e:
                logger.error(f"Error getting user info for {user_id}: {e}")
                user_info = f"ID: {user_id}"

            apartment_line = f"Квартира {apartment_number}: {user_info}\n"
            
            # Распределяем по домам
            if apartment_number <= HOUSE1_END:
                house1_msg += apartment_line
            else:
                house2_msg += apartment_line

            # Отправляем сообщение, если оно становится слишком длинным
            if len(house1_msg) > 3000:
                await update.message.reply_text(house1_msg)
                house1_msg = "🏢 Дом 1 (продолжение):\n"
            elif len(house2_msg) > 3000:
                await update.message.reply_text(house2_msg)
                house2_msg = "🏢 Дом 2 (продолжение):\n"

        # Отправляем оставшиеся сообщения
        if house1_msg != "🏢 Дом 1:\n":
            await update.message.reply_text(house1_msg)
        if house2_msg != "🏢 Дом 2:\n":
            await update.message.reply_text(house2_msg)

    except Exception as e:
        logger.error(f"Error in view_apartments: {e}")
        await update.message.reply_text("Произошла ошибка при формировании списка квартир.")

async def add_admin(update: Update, context: CallbackContext) -> None:
    """Добавление нового администратора"""
    if update.message.from_user.id != MAIN_ADMIN_ID:
        await update.message.reply_text("Эта команда доступна только главному администратору.")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Укажите ID пользователя.")
        return

    new_admin_id = int(context.args[0])

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO admins (user_id, added_by) VALUES (?, ?)",
                      (new_admin_id, MAIN_ADMIN_ID))
        conn.commit()

    try:
        user = await context.bot.get_chat_member(GROUP_ID, new_admin_id)
        user_mention = format_user_mention(user.user)
        await update.message.reply_text(
            f"✅ {user_mention} добавлен как администратор",
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Error adding admin: {e}")
        await update.message.reply_text("Ошибка при добавлении администратора.")

async def remove_admin(update: Update, context: CallbackContext) -> None:
    """Удаление администратора"""
    if update.message.from_user.id != MAIN_ADMIN_ID:
        await update.message.reply_text("Эта команда доступна только главному администратору.")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Укажите ID пользователя.")
        return

    admin_id = int(context.args[0])

    if admin_id == MAIN_ADMIN_ID:
        await update.message.reply_text("Невозможно удалить главного администратора.")
        return

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM admins WHERE user_id = ? AND user_id != ?",
                      (admin_id, MAIN_ADMIN_ID))
        conn.commit()

    try:
        user = await context.bot.get_chat_member(GROUP_ID, admin_id)
        user_mention = format_user_mention(user.user)
        await update.message.reply_text(
            f"❌ {user_mention} удален из администраторов",
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Error removing admin: {e}")
        await update.message.reply_text("Ошибка при удалении администратора.")


async def list_admins(update: Update, context: CallbackContext) -> None:
    """Вывод списка администраторов."""
    requester_id = update.message.from_user.id
    if not is_admin_user(requester_id):
        await update.message.reply_text("Эта команда доступна только администраторам.")
        return

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT user_id, added_by, added_date
            FROM admins
            ORDER BY added_date
            """
        )
        admins = cursor.fetchall()

    if not admins:
        await update.message.reply_text("Список администраторов пуст.")
        return

    lines = ["👑 Текущие администраторы:"]
    for admin_id, added_by, added_date in admins:
        try:
            member = await context.bot.get_chat_member(GROUP_ID, admin_id)
            admin_mention = format_user_mention(member.user)
        except Exception as error:
            logger.warning(f"Failed to load admin {admin_id}: {error}")
            admin_mention = f"ID: {admin_id}"

        suffix = " (главный администратор)" if admin_id == MAIN_ADMIN_ID else ""
        added_info = f", добавлен {added_date}" if added_date else ""
        if added_by:
            added_info += f", добавил {added_by}"
        lines.append(f"- {admin_mention}{suffix}{added_info}")

    await update.message.reply_text("\n".join(lines), parse_mode='Markdown')


async def admin_help(update: Update, context: CallbackContext) -> None:
    """Подсказка по админским командам."""
    if not is_admin_user(update.message.from_user.id):
        await update.message.reply_text("Эта команда доступна только администраторам.")
        return

    admin_commands = (
        "👑 Команды администратора:\n"
        "/viewapartments - Просмотр списка квартир\n"
        "/forceregistration - Запуск перерегистрации (главный админ)\n"
        "/adminassign [квартира] [ID] - Назначить владельца квартиры\n"
        "/adminunlink [ID] [квартира] - Удалить привязку пользователя\n"
        "/admindelete [квартира] - Освободить квартиру\n"
        "/clearrequests - Очистить зависшие заявки\n"
        "/apartmentstats - Показать занятые/свободные квартиры\n"
        "/listadmins - Показать текущих администраторов\n"
        "/addadmin [ID] - Добавить администратора (главный админ)\n"
        "/removeadmin [ID] - Удалить администратора (главный админ)\n"
    )
    await update.message.reply_text(admin_commands)
    await update.message.reply_text(
        "Быстрые действия администратора:",
        reply_markup=get_admin_actions_keyboard()
    )

async def help_command(update: Update, context: CallbackContext) -> None:
    """Показ списка доступных команд"""
    user_id = update.message.from_user.id

    is_admin = is_admin_user(user_id)

    basic_commands = (
        "📋 Доступные команды:\n\n"
        "/start - Начало работы с ботом\n"
        "/help - Показать это сообщение\n"
        "/setapartment [номер] - Привязать квартиру\n"
        "/deleteapartment - Удалить привязку к квартире\n"
    )

    admin_commands = (
        "\n👑 Команды администратора:\n"
        "/viewapartments - Просмотр списка квартир\n"
        "/forceregistration - Запуск перерегистрации (главный админ)\n"
        "/adminassign [квартира] [ID] - Назначить владельца квартиры\n"
        "/adminunlink [ID] [квартира] - Удалить привязку пользователя\n"
        "/admindelete [квартира] - Освободить квартиру\n"
        "/clearrequests - Очистить зависшие заявки\n"
        "/apartmentstats - Показать занятые/свободные квартиры\n"
        "/listadmins - Показать текущих администраторов\n"
        "/addadmin [ID] - Добавить администратора (главный админ)\n"
        "/removeadmin [ID] - Удалить администратора (главный админ)\n"
        "/adminhelp - Подсказка по админским командам\n"
    )

    message = basic_commands + (admin_commands if is_admin else "")
    await update.message.reply_text(message)

async def check_all_members(update: Update, context: CallbackContext) -> None:
    """Проверка всех участников группы на наличие регистрации"""
    if update.message.from_user.id != MAIN_ADMIN_ID:
        await update.message.reply_text("Эта команда доступна только главному администратору.")
        return

    try:
        # Получаем список администраторов
        admins = await context.bot.get_chat_administrators(GROUP_ID)
        admin_ids = [admin.user.id for admin in admins]
        
        unregistered_count = 0
        processed_count = 0
        
        async for member in context.bot.get_chat_members(GROUP_ID):
            if member.user.is_bot or member.status not in ['member', 'administrator', 'creator']:
                continue

            user_id = member.user.id
            processed_count += 1
            
            if user_id not in admin_ids:
                with get_db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT apartment_number FROM apartments WHERE user_id = ?", (user_id,))
                    apartment = cursor.fetchone()
                    
                    if not apartment:
                        unregistered_count += 1
                        user_mention = format_user_mention(member.user)
                        await context.bot.send_message(
                            chat_id=GROUP_ID,
                            text=f"⚠️ {user_mention}, пожалуйста, укажите номер своей квартиры с помощью команды:\n"
                                 f"/setapartment [номер]",
                            parse_mode='Markdown'
                        )

        await update.message.reply_text(
            f"Проверка завершена!\n"
            f"Обработано участников: {processed_count}\n"
            f"Не зарегистрировано: {unregistered_count}"
        )

    except Exception as e:
        logger.error(f"Error checking members: {e}")
        await update.message.reply_text("Произошла ошибка при проверке участников.")

def main() -> None:
    """Запуск бота"""
    # Создание базы данных
    create_db()

    # Инициализация бота
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Добавление обработчиков команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("setapartment", request_apartment_access))
    application.add_handler(CommandHandler("deleteapartment", delete_apartment))
    application.add_handler(CommandHandler("viewapartments", view_apartments))
    application.add_handler(CommandHandler("addadmin", add_admin))
    application.add_handler(CommandHandler("removeadmin", remove_admin))
    application.add_handler(CommandHandler("listadmins", list_admins))
    application.add_handler(CommandHandler("adminhelp", admin_help))
    application.add_handler(CommandHandler("adminassign", admin_assign))
    application.add_handler(CommandHandler("adminunlink", admin_unlink))
    application.add_handler(CommandHandler("admindelete", admin_delete_apartment))
    application.add_handler(CommandHandler("clearrequests", clear_approval_requests))
    application.add_handler(CommandHandler("apartmentstats", apartment_stats))
    application.add_handler(CommandHandler("forceregistration", force_registration))
    application.add_handler(CommandHandler("approve", approve_request))
    application.add_handler(CommandHandler("reject", reject_request))
    application.add_handler(CommandHandler("checkall", check_all_members))
    
    # Обработчик текстовых сообщений
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_admin_callback, pattern="^admin_"))

    # Планировщик рассылок
    if application.job_queue:
        morning_time = time(hour=7, minute=0, tzinfo=BOT_TZINFO)
        evening_time = time(hour=22, minute=0, tzinfo=BOT_TZINFO)
        application.job_queue.run_daily(send_morning_greeting, morning_time, name="morning_greeting")
        application.job_queue.run_daily(send_evening_greeting, evening_time, name="evening_greeting")
    
    # Запуск бота
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
