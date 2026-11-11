"""Real provider → command → PNG → owner Telegram, with delivery receipts."""
from pathlib import Path
import json
import broker_ytd_dispatch as dispatch
from orderflow_tape import TapeReport, send_photo
from bot_io import send_message


def main():
    legacy = dispatch.legacy
    assert legacy.TELEGRAM_BOT_TOKEN and legacy.TELEGRAM_CHAT_ID, 'Telegram configuration missing'
    out = Path('.runtime/tape-verification')
    out.mkdir(parents=True, exist_ok=True)
    report = dispatch.command_result('/tape ELTY', str(legacy.TELEGRAM_CHAT_ID))
    (out/'report.txt').write_text(str(report), encoding='utf-8')
    print(report)
    assert isinstance(report, TapeReport), 'No valid provider report'
    assert report.dashboard, 'PNG dashboard missing'
    assert report.metrics['trades'], 'Running trades missing'
    (out/'ELTY-tape.png').write_bytes(report.dashboard)
    (out/'metrics.json').write_text(json.dumps(report.metrics, ensure_ascii=False, indent=2), encoding='utf-8')
    photo = send_photo(legacy.TELEGRAM_BOT_TOKEN, str(legacy.TELEGRAM_CHAT_ID), report.dashboard, 'ELTY')
    message = send_message(legacy.TELEGRAM_BOT_TOKEN, str(legacy.TELEGRAM_CHAT_ID), str(report))
    receipt = {'command':'/tape ELTY', 'photo_confirmed': bool(photo['result']['photo']),
               'photo_message_id': photo['result']['message_id'],
               'text_message_id': message['result']['message_id'],
               'trades':len(report.metrics['trades']), 'operations':report.metrics['requests']}
    (out/'receipt.json').write_text(json.dumps(receipt,indent=2))
    print('TAPE_SMOKE_OK', json.dumps(receipt))


if __name__ == '__main__':
    main()
