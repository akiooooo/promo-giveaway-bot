import logging
import time
from datetime import datetime

from promo_bot.config import Settings
from promo_bot.database import Database
from promo_bot.max_api import MaxApiError, MaxClient


logger = logging.getLogger(__name__)


def callback_button(text: str, payload: str) -> dict:
    return {"type": "callback", "text": text, "payload": payload}


def link_button(text: str, url: str) -> dict:
    return {"type": "link", "text": text, "url": url}


class PromoBot:
    def __init__(self, settings: Settings, db: Database, client: MaxClient):
        self.settings = settings
        self.db = db
        self.client = client

    def now(self) -> datetime:
        return datetime.now(self.settings.timezone)

    @staticmethod
    def menu() -> list[list[dict]]:
        return [
            [callback_button("Получить промокод", "menu:promo")],
            [callback_button("Участвовать в розыгрыше", "menu:participate")],
            [callback_button("Как участвовать", "menu:how"), callback_button("Мои шансы", "menu:chances")],
            [callback_button("Правила акции", "menu:rules")],
        ]

    def start(self, user: dict) -> None:
        self.db.upsert_user(user, self.now())
        self.client.send_message(
            int(user["user_id"]),
            "Добро пожаловать в Бот розыгрышей",
            self.menu(),
            image_token=self.settings.banner_token,
        )

    def handle_update(self, update: dict) -> None:
        event_type = update.get("update_type")
        if event_type == "bot_started":
            self.start(update["user"])
            return
        if event_type == "message_created":
            self.handle_message(update["message"])
            return
        if event_type == "message_callback":
            self.handle_callback(update["callback"])

    def handle_message(self, message: dict) -> None:
        user = message.get("sender") or {}
        if "user_id" not in user:
            return
        user_id = int(user["user_id"])
        self.db.upsert_user(user, self.now())
        text = ((message.get("body") or {}).get("text") or "").strip()
        state = self.db.get_user_state(user_id)
        if state and state["state"] == "claim_name":
            parts = text.split()
            if len(parts) < 2:
                self.client.send_message(user_id, "Напиши имя и фамилию через пробел")
                return
            first_name, last_name = parts[0], " ".join(parts[1:])
            draw_id = int(state["payload"]["draw_id"])
            try:
                self.db.save_claim_name(draw_id, user_id, first_name, last_name, self.now())
            except ValueError as error:
                self.client.send_message(user_id, str(error))
                return
            buttons = [[
                callback_button("Согласен", f"consent:{draw_id}:yes"),
                callback_button("Не согласен", f"consent:{draw_id}:no"),
            ]]
            self.client.send_message(
                user_id,
                "Разрешаешь передать имя и фамилию партнёру или билетному оператору для выдачи приза?",
                buttons,
            )
            return
        if text.lower() in {"/start", "начать", "меню"}:
            self.start(user)
        else:
            self.client.send_message(user_id, "Выбери действие", self.menu())

    def handle_callback(self, callback: dict) -> None:
        user = callback["user"]
        user_id = int(user["user_id"])
        callback_id = callback["callback_id"]
        payload = callback.get("payload", "")
        self.db.upsert_user(user, self.now())
        try:
            text, buttons = self.callback_response(user_id, payload)
        except MaxApiError:
            logger.exception("MAX API error while handling callback")
            text, buttons = "Не получилось проверить подписку. Попробуй ещё раз через минуту", self.menu()
        except ValueError as error:
            text, buttons = str(error), self.menu()
        self.client.answer_callback(callback_id, text, buttons)

    def callback_response(self, user_id: int, payload: str) -> tuple[str, list[list[dict]] | None]:
        now = self.now()
        partner = self.db.get_partner_for_day(now.date())
        if payload == "menu:promo":
            if not partner:
                return "На сегодня партнёрский день не настроен", self.menu()
            result = self.db.request_promo(user_id, partner["id"], now)
            if result.status == "exhausted":
                text = (
                    f"Промокоды от {partner['name']} уже закончились — их разобрали быстрее всех.\n\n"
                    "Но ты всё ещё можешь подписаться на партнёра и принять участие в розыгрыше призов дня."
                )
                return text, [[callback_button("Участвовать в розыгрыше", "menu:participate")]]
            text = (
                f"Твой фестивальный бонус от {partner['name']} готов!\n\n"
                f"Промокод: {result.code}\n\n"
                f"Используй его до {partner['promo_expires_on']} на {partner['promo_place']}.\n\n"
                "Теперь можешь принять участие в розыгрыше призов дня."
            )
            return text, [
                [{"type": "clipboard", "text": "Скопировать промокод", "payload": result.code}],
                [callback_button("Участвовать в розыгрыше", "menu:participate")],
            ]
        if payload == "menu:participate":
            if not partner:
                return "На сегодня партнёрский день не настроен", self.menu()
            if self.db.is_participant(user_id, partner["id"]):
                return "Ты уже участвуешь в сегодняшнем розыгрыше. Результаты сообщим после подведения итогов", self.menu()
            return (
                f"Чтобы попасть в розыгрыш призов от {partner['name']}, подпишись на аккаунт партнёра.",
                [
                    [link_button(f"Подписаться на {partner['name']}", partner["channel_url"])],
                    [callback_button("Проверить подписку", "check:subscription")],
                ],
            )
        if payload == "check:subscription":
            if not partner:
                return "На сегодня партнёрский день не настроен", self.menu()
            if not self.client.is_channel_member(partner["channel_id"], user_id):
                return (
                    f"Пока не вижу подписку. Подпишись на аккаунт {partner['name']}, вернись в бот и попробуй ещё раз.",
                    [
                        [link_button("Подписаться", partner["channel_url"])],
                        [callback_button("Проверить ещё раз", "check:subscription")],
                    ],
                )
            self.db.register_participation(user_id, partner["id"], now)
            chances = self.db.get_user_progress(user_id, self.settings.max_chances)["chances"]
            return (
                f"Ты в лайнапе!\n\nЗаявка на розыгрыш призов от {partner['name']} зарегистрирована.\n\n"
                f"Накоплено шансов: {chances} из {self.settings.max_chances}.\n\n"
                "Возвращайся завтра — на сцену выйдет новый партнёр.",
                self.menu(),
            )
        if payload == "menu:chances":
            progress = self.db.get_user_progress(user_id, self.settings.max_chances)
            days = ", ".join(progress["days"]) or "пока нет"
            partners = ", ".join(progress["partners"]) or "пока нет"
            text = (
                "Твой фестивальный пропуск\n\n"
                f"Накоплено шансов: {progress['chances']} из {self.settings.max_chances}\n"
                f"Пройденные дни: {days}\n"
                f"Партнёры: {partners}\n"
                f"Осталось дней: {progress['remaining']}\n"
                f"Главный приз: {self.settings.grand_prize}"
            )
            return text, self.menu()
        if payload == "menu:how":
            return (
                "1. Получи промокод или зарегистрируйся в розыгрыше дня\n"
                "2. Для розыгрыша подпишись на канал партнёра\n"
                "3. Нажми «Проверить подписку»\n"
                "4. За каждый активный день начисляется один шанс",
                self.menu(),
            )
        if payload == "menu:rules":
            return (
                "Один активный день даёт один шанс на главный приз. Промокод и участие в один день всё равно дают один шанс. "
                f"Максимум — {self.settings.max_chances}. Победители выбираются случайно, результаты подтверждает администратор.",
                self.menu(),
            )
        if payload.startswith("claim:"):
            draw_id = int(payload.split(":", 1)[1])
            self.db.set_user_state(user_id, "claim_name", {"draw_id": draw_id}, now)
            return "Напиши имя и фамилию через пробел", None
        if payload.startswith("consent:"):
            _, draw_id, answer = payload.split(":", 2)
            self.db.submit_consent(int(draw_id), user_id, answer == "yes", now)
            if answer == "yes":
                return "Данные сохранены. Администратор свяжется с тобой по поводу приза", self.menu()
            return "Без согласия передать данные приз не получится. Заявка закрыта", self.menu()
        return "Кнопка устарела. Открой меню ещё раз", self.menu()

    def run(self) -> None:
        marker = None
        logger.info("bot started")
        while True:
            try:
                response = self.client.get_updates(marker)
                for update in response.get("updates", []):
                    self.handle_update(update)
                marker = response.get("marker", marker)
            except KeyboardInterrupt:
                return
            except Exception:
                logger.exception("polling error")
                time.sleep(3)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    bot = PromoBot(settings, Database(settings.db_path), MaxClient(settings.token, settings.api_url))
    bot.run()


if __name__ == "__main__":
    main()
