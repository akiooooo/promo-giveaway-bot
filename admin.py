import argparse
import csv
import json
import random
import sys
from datetime import date, datetime
from pathlib import Path

from promo_bot.config import Settings
from promo_bot.database import Database
from promo_bot.max_api import MaxClient


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def parse_day(value: str) -> date:
    return date.fromisoformat(value)


def import_partners(db: Database, path: str) -> None:
    added = 0
    with open(path, encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            db.add_partner(row)
            added += 1
    print(f"Добавлено партнёров: {added}")


def import_promos(db: Database, partner: str, path: str) -> None:
    with open(path, encoding="utf-8-sig") as file:
        lines = [line.strip() for line in file if line.strip()]
    if lines and lines[0].lower() == "code":
        lines = lines[1:]
    count = db.add_promo_codes(partner, lines)
    print(f"Добавлено промокодов: {count}")


def export_draw(db: Database, draw_id: int, output: str) -> None:
    draw = db.get_draw(draw_id)
    if not draw:
        raise SystemExit("Розыгрыш не найден")
    folder = Path(output) / f"draw_{draw_id}"
    folder.mkdir(parents=True, exist_ok=True)
    fields = ["user_id", "username", "first_name", "last_name", "chances", "role", "position"]
    for filename, roles in (
        ("participants.csv", None),
        ("winners.csv", {"winner", "promoted"}),
        ("reserves.csv", {"reserve"}),
    ):
        rows = draw["entries"] if roles is None else [row for row in draw["entries"] if row["role"] in roles]
        with (folder / filename).open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    summary = {key: value for key, value in draw.items() if key != "entries"}
    (folder / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(folder.resolve())


def send_notifications(db: Database, settings: Settings) -> None:
    client = MaxClient(settings.token, settings.api_url)
    sent = 0
    failed = 0
    for item in db.get_queued_notifications():
        buttons = None
        draw_id = item["payload"].get("draw_id")
        if draw_id:
            buttons = [[{"type": "callback", "text": "Получить приз", "payload": f"claim:{draw_id}"}]]
        try:
            client.send_message(item["user_id"], item["text"], buttons)
            db.mark_notification(item["id"], True, datetime.now(settings.timezone))
            sent += 1
        except Exception as error:
            db.mark_notification(item["id"], False, datetime.now(settings.timezone), str(error))
            failed += 1
    print(f"Отправлено: {sent}, ошибок: {failed}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Управление промо-ботом")
    sub = parser.add_subparsers(dest="command", required=True)

    partners = sub.add_parser("import-partners", help="загрузить партнёров из CSV")
    partners.add_argument("file")

    promos = sub.add_parser("import-promos", help="загрузить промокоды")
    promos.add_argument("partner")
    promos.add_argument("file")

    stats = sub.add_parser("stats", help="показать статистику")
    stats.add_argument("--day", type=parse_day, default=date.today())

    daily = sub.add_parser("draw-daily", help="провести дневной розыгрыш")
    daily.add_argument("--day", type=parse_day, default=date.today())
    daily.add_argument("--seed", type=int)

    final = sub.add_parser("draw-final", help="провести финальный розыгрыш")
    final.add_argument("--day", type=parse_day, default=date.today())
    final.add_argument("--seed", type=int)

    sub.add_parser("draws", help="список розыгрышей")

    approve = sub.add_parser("approve", help="подтвердить результаты")
    approve.add_argument("draw_id", type=int)

    export = sub.add_parser("export", help="сформировать выгрузку")
    export.add_argument("draw_id", type=int)
    export.add_argument("--output", default="exports")

    sub.add_parser("send-notifications", help="отправить очередь уведомлений")
    sub.add_parser("expire-claims", help="передать просроченные призы резерву")

    push = sub.add_parser("push", help="поставить уведомление в очередь")
    push.add_argument("text")
    push.add_argument("--user-id", type=int)

    upload = sub.add_parser("upload-banner", help="загрузить баннер в MAX")
    upload.add_argument("file")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = Settings.from_env(require_token=args.command in {"send-notifications", "upload-banner"})
    db = Database(settings.db_path)
    now = datetime.now(settings.timezone)

    if args.command == "import-partners":
        import_partners(db, args.file)
    elif args.command == "import-promos":
        import_promos(db, args.partner, args.file)
    elif args.command == "stats":
        print(json.dumps(db.get_stats(args.day), ensure_ascii=False, indent=2))
    elif args.command == "draw-daily":
        draw_id = db.create_daily_draw(args.day, now, random.Random(args.seed))
        print(f"Черновик дневного розыгрыша: {draw_id}")
    elif args.command == "draw-final":
        draw_id = db.create_final_draw(
            args.day, now, settings.final_winners, settings.final_reserves,
            settings.max_chances, random.Random(args.seed),
        )
        print(f"Черновик финального розыгрыша: {draw_id}")
    elif args.command == "draws":
        print(json.dumps(db.list_draws(), ensure_ascii=False, indent=2))
    elif args.command == "approve":
        count = db.approve_draw(args.draw_id, now, settings.grand_prize)
        print(f"Результаты подтверждены, уведомлений в очереди: {count}")
    elif args.command == "export":
        export_draw(db, args.draw_id, args.output)
    elif args.command == "send-notifications":
        send_notifications(db, settings)
    elif args.command == "expire-claims":
        count = db.expire_claims(now, settings.grand_prize)
        print(f"Победителей из резерва: {count}")
    elif args.command == "push":
        count = db.queue_push(args.text, now, args.user_id)
        print(f"Уведомлений в очереди: {count}")
    elif args.command == "upload-banner":
        token = MaxClient(settings.token, settings.api_url).upload_image(args.file)
        print(token)


if __name__ == "__main__":
    main()
