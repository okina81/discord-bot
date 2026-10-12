import re
import datetime
from config import JST

_CLOCK_KANJI = r'(\d{1,2})時(?:(\d{1,2})分)?(?:から)?'
_CLOCK_COLON = r'(\d{1,2}):(\d{2})(?:から)?'


def parse_duration(text):
    match = re.fullmatch(r'(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?', text.strip())
    if not match or not any(match.groups()):
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    total = hours * 3600 + minutes * 60 + seconds
    return total if total > 0 else None


def format_duration(seconds):
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h: parts.append(f"{h}時間")
    if m: parts.append(f"{m}分")
    if s: parts.append(f"{s}秒")
    return "".join(parts)


def format_clock(hour, minute):
    """15時、15時30分 のように時刻を表す(0分なら分を省く)。"""
    return f"{hour}時" + (f"{minute}分" if minute else "")


def _next_clock_time(hour, minute):
    """次にその時刻になるまでの秒数・表示用の文字列・日時を返す。範囲外ならNone。"""
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    now = datetime.datetime.now(JST)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    seconds = int((target - now).total_seconds())
    return seconds, format_clock(hour, minute), target


def parse_clock_time(text):
    text = text.strip()
    m = re.fullmatch(_CLOCK_KANJI, text) or re.fullmatch(_CLOCK_COLON, text)
    if not m:
        return None
    result = _next_clock_time(int(m.group(1)), int(m.group(2) or 0))
    if result is None:
        return None
    seconds, time_str, _ = result
    return seconds, time_str


def extract_clock_time(text):
    m = re.search(_CLOCK_KANJI, text)
    if not m:
        return None
    return _next_clock_time(int(m.group(1)), int(m.group(2) or 0))
