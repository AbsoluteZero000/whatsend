import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

VARIABLES = {'send_number', 'counter', 'total_sends', 'today', 'date', 'time'}
PATTERN = re.compile(r'\{([a-z_]+)\}')


def render_message(message, number, total, timezone_name, now=None):
    local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(timezone_name or 'UTC'))
    values = {'send_number': str(number), 'counter': str(number), 'total_sends': str(total) if total is not None else '∞',
              'today': local.strftime('%Y-%m-%d'), 'date': local.strftime('%Y-%m-%d'), 'time': local.strftime('%H:%M')}
    # Only replace the supported variables; ordinary braces remain literal text.
    return PATTERN.sub(lambda match: values.get(match[1], match[0]), message)
