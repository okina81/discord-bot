import re
import datetime
from config import JST


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


def parse_clock_time(text):
    text = text.strip()
    m = re.fullmatch(r'(\d{1,2})時(?:(\d{1,2})分)?(?:から)?', text)
    if not m:
        m2 = re.fullmatch(r'(\d{1,2}):(\d{2})(?:から)?', text)
        if not m2:
            return None
        hour, minute = int(m2.group(1)), int(m2.group(2))
    else:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    now = datetime.datetime.now(JST)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    seconds = int((target - now).total_seconds())
    time_str = f"{hour}時" + (f"{minute}分" if minute else "")
    return seconds, time_str


def extract_clock_time(text):
    m = re.search(r'(\d{1,2})時(?:(\d{1,2})分)?(?:から)?', text)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    now = datetime.datetime.now(JST)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    seconds = int((target - now).total_seconds())
    time_str = f"{hour}時" + (f"{minute}分" if minute else "")
    return seconds, time_str, target
